"""ToolRunner tests — timeout, fault containment, idempotency, output capping.

These cover the P7 exit criteria directly: a timeout kills the tool, a
re-invoke with the same idempotency key does not re-run the handler, and an
oversized output is moved to the variable store.
"""
from __future__ import annotations

import asyncio

from oneops.registry.models import (
    ActivationCondition,
    ConditionOperator,
    ConditionSignal,
    ExecutionTier,
    ToolRecord,
)
from oneops.toolrunner.context import ToolContext
from oneops.toolrunner.idempotency import InMemoryIdempotencyStore
from oneops.toolrunner.models import ToolStatus
from oneops.toolrunner.resolver import HandlerResolver
from oneops.toolrunner.runner import ToolRunner
from oneops.toolrunner.variables import InMemoryVariableStore


def _ctx(tenant_id: str = "t-test", request_id: str = "r-test") -> ToolContext:
    """Minimal ToolContext for runner-isolation tests."""
    return ToolContext.from_request(
        {"tenant_id": tenant_id, "request_id": request_id})


def _tool(tool_id="test_tool", *, handler_ref="reg:h", timeout_ms=30_000,
          execution_type=ExecutionTier.READ, idempotent=True):
    return ToolRecord(
        id=tool_id, version=1, owner="team-test", description="A test tool.",
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF, signal=ConditionSignal.INTENT_IN,
            values=("x",)),
        handler_ref=handler_ref, execution_type=execution_type,
        timeout_ms=timeout_ms, idempotent=idempotent)


# ── success ──────────────────────────────────────────────────────────────


async def test_runs_a_tool_and_returns_success():
    resolver = HandlerResolver()

    async def handler(args, ctx):
        return {"echo": args.get("q")}

    resolver.register("reg:h", handler)
    runner = ToolRunner(resolver)
    result = await runner.run(_tool(), {"q": "hello"}, context=_ctx())
    assert result.status is ToolStatus.SUCCESS
    assert result.output == {"echo": "hello"}


# ── timeout ──────────────────────────────────────────────────────────────


async def test_a_slow_tool_times_out():
    resolver = HandlerResolver()

    async def slow(args, ctx):
        await asyncio.sleep(5)               # far over the 100ms budget
        return "never"

    resolver.register("reg:slow", slow)
    runner = ToolRunner(resolver)
    result = await runner.run(
        _tool(handler_ref="reg:slow", timeout_ms=100), {}, context=_ctx())
    assert result.status is ToolStatus.TIMEOUT
    assert "100ms" in result.error


# ── fault containment ────────────────────────────────────────────────────


async def test_a_raising_handler_becomes_a_failed_result():
    resolver = HandlerResolver()

    async def boom(args, ctx):
        raise RuntimeError("handler exploded")

    resolver.register("reg:boom", boom)
    runner = ToolRunner(resolver)
    result = await runner.run(_tool(handler_ref="reg:boom"), {}, context=_ctx())
    assert result.status is ToolStatus.FAILED
    assert "handler exploded" in result.error      # contained, not propagated


# ── idempotency ──────────────────────────────────────────────────────────


async def test_repeated_idempotency_key_does_not_rerun_the_handler():
    resolver = HandlerResolver()
    calls = {"n": 0}

    async def counting(args, ctx):
        calls["n"] += 1
        return {"call": calls["n"]}

    resolver.register("reg:count", counting)
    runner = ToolRunner(resolver, idempotency_store=InMemoryIdempotencyStore())
    tool = _tool(handler_ref="reg:count", execution_type=ExecutionTier.ACTION)

    first = await runner.run(tool, {}, context=_ctx(), idempotency_key="idem-1")
    second = await runner.run(tool, {}, context=_ctx(), idempotency_key="idem-1")

    assert calls["n"] == 1                         # handler ran exactly once
    assert first.from_idempotency_cache is False
    assert second.from_idempotency_cache is True
    assert second.output == first.output           # the replayed result


async def test_different_idempotency_keys_both_run():
    resolver = HandlerResolver()
    calls = {"n": 0}

    async def counting(args, ctx):
        calls["n"] += 1
        return calls["n"]

    resolver.register("reg:count", counting)
    runner = ToolRunner(resolver, idempotency_store=InMemoryIdempotencyStore())
    tool = _tool(handler_ref="reg:count")
    await runner.run(tool, {}, context=_ctx(), idempotency_key="a")
    await runner.run(tool, {}, context=_ctx(), idempotency_key="b")
    assert calls["n"] == 2                         # distinct keys → both run


# ── output capping ───────────────────────────────────────────────────────


async def test_large_output_is_moved_to_the_variable_store():
    resolver = HandlerResolver()

    async def big(args, ctx):
        return "z" * 10_000                  # well over the 4 KiB threshold

    resolver.register("reg:big", big)
    var_store = InMemoryVariableStore()
    runner = ToolRunner(resolver, variable_store=var_store)
    result = await runner.run(_tool(handler_ref="reg:big"), {}, context=_ctx())

    assert result.status is ToolStatus.SUCCESS
    # The big blob did not come back inline — it is a VariableRef preview.
    assert getattr(result.output, "is_variable_ref", False) is True
    assert result.output.size_bytes >= 10_000
    assert var_store.has(result.output.name)       # full value retained, fetchable


async def test_small_output_stays_inline():
    resolver = HandlerResolver()

    async def small(args, ctx):
        return {"ok": True}

    resolver.register("reg:small", small)
    runner = ToolRunner(resolver, variable_store=InMemoryVariableStore())
    result = await runner.run(_tool(handler_ref="reg:small"), {}, context=_ctx())
    assert result.output == {"ok": True}           # not wrapped
