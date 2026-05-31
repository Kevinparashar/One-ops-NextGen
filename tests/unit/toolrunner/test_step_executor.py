"""ToolStepExecutor tests — running an agent's tools for a plan step."""
from __future__ import annotations

from oneops.registry.models import (
    AbacTags,
    ActivationCondition,
    AgentRecord,
    ConditionOperator,
    ConditionSignal,
    DeterminismLevel,
    ExecutionTier,
    RoutingShape,
    ToolRecord,
    ToolRef,
)
from oneops.registry.service import RegistryService
from oneops.registry.store import FileBackend
from oneops.toolrunner.idempotency import InMemoryIdempotencyStore
from oneops.toolrunner.resolver import HandlerResolver
from oneops.toolrunner.runner import ToolRunner
from oneops.toolrunner.step_executor import ToolStepExecutor


def _cond():
    return ActivationCondition(operator=ConditionOperator.LEAF,
                               signal=ConditionSignal.INTENT_IN, values=("x",))


def _tool(tool_id, handler_ref):
    return ToolRecord(
        id=tool_id, version=1, owner="team-test", description="A test tool.",
        activation_condition=_cond(), handler_ref=handler_ref,
        execution_type=ExecutionTier.READ, timeout_ms=30_000, idempotent=True)


def _agent(agent_id, tool_ids):
    return AgentRecord(
        id=agent_id, version=1, owner="team-test", description="A test agent.",
        intent_family="testing", routing_shape=RoutingShape.SINGLE,
        activation_condition=_cond(),
        abac_tags=AbacTags(tier=ExecutionTier.READ),
        determinism_level=DeterminismLevel.LOW,
        tool_refs=tuple(ToolRef(tool_id=t) for t in tool_ids))


def _registry(tmp_path, *, agents, tools):
    svc = RegistryService(FileBackend(tmp_path))
    for t in tools:
        svc.tools.create(t)
        svc.tools.activate(t.id, 1)
    for a in agents:
        svc.agents.create(a)
        svc.agents.activate(a.id, 1)
    return svc


def _step(agent_id, *, step_id="step_1", parameters=None):
    return {"step_id": step_id, "agent_id": agent_id,
            "parameters": parameters or {}, "depends_on": []}


def _request(**over):
    base = {"request_id": "r-1", "tenant_id": "t-a"}
    base.update(over)
    return base


# ── runs an agent's tools ────────────────────────────────────────────────


async def test_runs_all_of_an_agents_tools(tmp_path):
    resolver = HandlerResolver()

    async def get_details(args, ctx):
        return {"id": args.get("ticket_id"), "status": "open"}

    async def get_timeline(args, ctx):
        return ["created", "assigned"]

    resolver.register("h:details", get_details)
    resolver.register("h:timeline", get_timeline)
    reg = _registry(
        tmp_path,
        tools=[_tool("get_details", "h:details"), _tool("get_timeline", "h:timeline")],
        agents=[_agent("uc_summary", ["get_details", "get_timeline"])])
    executor = ToolStepExecutor(reg, ToolRunner(resolver))

    result = await executor.run(
        _step("uc_summary", parameters={"ticket_id": "INC1"}), _request())
    assert result["status"] == "success"
    tools = result["output"]["tools"]
    assert tools["get_details"] == {"id": "INC1", "status": "open"}
    assert tools["get_timeline"] == ["created", "assigned"]


# ── failure paths ────────────────────────────────────────────────────────


async def test_unknown_agent_fails_the_step(tmp_path):
    reg = _registry(tmp_path, tools=[], agents=[])
    executor = ToolStepExecutor(reg, ToolRunner(HandlerResolver()))
    result = await executor.run(_step("ghost_agent"), _request())
    assert result["status"] == "failed"
    assert "no active registry record" in result["error"]


async def test_tool_less_agent_succeeds_with_a_note(tmp_path):
    reg = _registry(tmp_path, tools=[], agents=[_agent("uc_chat", [])])
    executor = ToolStepExecutor(reg, ToolRunner(HandlerResolver()))
    result = await executor.run(_step("uc_chat"), _request())
    assert result["status"] == "success"
    assert "no tools" in result["output"]["note"]


async def test_a_failing_tool_fails_the_step(tmp_path):
    resolver = HandlerResolver()

    async def ok(args, ctx):
        return "fine"

    async def boom(args, ctx):
        raise RuntimeError("tool exploded")

    resolver.register("h:ok", ok)
    resolver.register("h:boom", boom)
    reg = _registry(
        tmp_path,
        tools=[_tool("t_ok", "h:ok"), _tool("t_boom", "h:boom")],
        agents=[_agent("uc_a", ["t_ok", "t_boom"])])
    executor = ToolStepExecutor(reg, ToolRunner(resolver))

    result = await executor.run(_step("uc_a"), _request())
    assert result["status"] == "failed"
    assert "t_boom" in result["error"]
    assert "exploded" in result["error"]


# ── idempotency threaded through the executor ────────────────────────────


async def test_replayed_request_does_not_rerun_the_tools(tmp_path):
    resolver = HandlerResolver()
    calls = {"n": 0}

    async def counting(args, ctx):
        calls["n"] += 1
        return calls["n"]

    resolver.register("h:count", counting)
    reg = _registry(tmp_path, tools=[_tool("t_count", "h:count")],
                    agents=[_agent("uc_a", ["t_count"])])
    runner = ToolRunner(resolver, idempotency_store=InMemoryIdempotencyStore())
    executor = ToolStepExecutor(reg, runner)

    # Same request envelope (same idempotency base) → re-delivery.
    req = _request(idempotency_key="idem-fixed")
    await executor.run(_step("uc_a"), req)
    await executor.run(_step("uc_a"), req)
    assert calls["n"] == 1                          # tool ran exactly once


# ── large output capping through the executor ────────────────────────────


async def test_large_tool_output_is_a_preview_in_the_step_result(tmp_path):
    resolver = HandlerResolver()

    async def big(args, ctx):
        return "q" * 10_000

    resolver.register("h:big", big)
    reg = _registry(tmp_path, tools=[_tool("t_big", "h:big")],
                    agents=[_agent("uc_a", ["t_big"])])
    executor = ToolStepExecutor(reg, ToolRunner(resolver))

    result = await executor.run(_step("uc_a"), _request())
    big_out = result["output"]["tools"]["t_big"]
    # JSON-safe preview dict — not the 10 KB blob, so checkpointed state stays lean.
    assert big_out["variable_ref"]
    assert big_out["size_bytes"] >= 10_000
    assert len(big_out["preview"]) < 400
