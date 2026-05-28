"""P11 — chaos drills: a dependency fails, the system degrades, never crashes.

Each drill injects one fault and asserts the turn still returns a terminal
result — a typed failure or a boundary response — and **never raises**. The
pluggable-backend design is what makes this testable in-process: a failing
transport / executor / tool is just a different implementation of a Protocol.
"""
from __future__ import annotations

import asyncio

import pytest

from oneops.executor.boundary import LlmBoundaryResponder
from oneops.executor.graph import build_executor_graph, run_turn
from oneops.executor.step_runner import make_result
from oneops.llm import EchoTransport, LlmGateway
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
from oneops.router.plan import PlanStep, RoutePlan, RouteResult
from oneops.toolrunner import HandlerResolver, ToolRunner, ToolStepExecutor

pytestmark = pytest.mark.timeout(120)


def _agent(agent_id, *, tool_ids=()):
    return AgentRecord(
        id=agent_id, version=1, owner="team-test", description="A test agent.",
        intent_family="testing", routing_shape=RoutingShape.SINGLE,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF, signal=ConditionSignal.INTENT_IN,
            values=("summary",)),
        abac_tags=AbacTags(tier=ExecutionTier.READ),
        determinism_level=DeterminismLevel.LOW,
        tool_refs=tuple(ToolRef(tool_id=t) for t in tool_ids))


def _tool(tool_id, handler_ref):
    return ToolRecord(
        id=tool_id, version=1, owner="team-test", description="A tool.",
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF, signal=ConditionSignal.INTENT_IN,
            values=("summary",)),
        handler_ref=handler_ref, execution_type=ExecutionTier.READ,
        timeout_ms=200, idempotent=True)


def _registry(tmp_path, *, agents, tools=()):
    svc = RegistryService(FileBackend(tmp_path))
    for t in tools:
        svc.tools.create(t)
        svc.tools.activate(t.id, 1)
    for a in agents:
        svc.agents.create(a)
        svc.agents.activate(a.id, 1)
    return svc


class _StubRouter:
    def __init__(self, result):
        self._result = result

    async def route(self, query_text, *, principal, signals,
                    conversation_history=None, request_ctx=None):
        return self._result


def _routed(agent_id="uc_a"):
    return RouteResult.routed(
        RoutePlan(steps=(PlanStep(step_id="step_1", agent_id=agent_id),)), ["d"])


def _envelope(**over):
    base = {"request_id": "r-1", "tenant_id": "t-a", "session_id": "s-1",
            "user_id": "u-1", "role": "service_desk_agent", "message": "go"}
    base.update(over)
    return base


# ── drill 1: a handler dependency is down ────────────────────────────────


async def test_failing_step_executor_degrades_not_crashes(tmp_path):
    class _DependencyDown:
        async def run(self, step, request):
            return make_result(step, status="failed",
                               error="chaos: downstream dependency unreachable")

    reg = _registry(tmp_path, agents=[_agent("uc_a")])
    graph = build_executor_graph(_StubRouter(_routed()), reg,
                                 step_executor=_DependencyDown())
    result = await run_turn(graph, _envelope())
    assert result["final_status"] == "failed"        # degraded — typed failure
    assert "final_response" in result                # the user still gets a reply


# ── drill 2: a tool exceeds its timeout ──────────────────────────────────


async def test_tool_timeout_degrades_not_crashes(tmp_path):
    resolver = HandlerResolver()

    async def hangs(args, ctx):
        await asyncio.sleep(5)                       # far past the 200ms budget
        return "never"

    resolver.register("chaos:hang", hangs)
    reg = _registry(tmp_path,
                    tools=[_tool("slow_tool", "chaos:hang")],
                    agents=[_agent("uc_a", tool_ids=["slow_tool"])])
    graph = build_executor_graph(
        _StubRouter(_routed()), reg,
        step_executor=ToolStepExecutor(reg, ToolRunner(resolver)))
    result = await run_turn(graph, _envelope())
    # The tool was killed at its timeout; the turn still terminated cleanly.
    assert result["final_status"] == "failed"
    assert result["step_results"][0]["status"] == "failed"


# ── drill 3: the LLM gateway is down ─────────────────────────────────────


async def test_llm_gateway_down_degrades_via_boundary_fallback(tmp_path):
    # The boundary path's LLM is unreachable — the LlmBoundaryResponder must
    # fall back to the deterministic reply, not crash the turn.
    dead_gateway = LlmGateway(EchoTransport(fail_times=999), max_retries=0)
    reg = _registry(tmp_path, agents=[_agent("uc_a")])
    graph = build_executor_graph(
        _StubRouter(RouteResult.no_match("nothing matched", ["d"])), reg,
        boundary=LlmBoundaryResponder(dead_gateway))
    result = await run_turn(graph, _envelope())
    assert result["final_status"] == "clarification"
    assert result["final_response"]                  # deterministic fallback reply


# ── drill 4: mixed failure under concurrency ─────────────────────────────


async def test_mixed_failure_under_concurrency_all_return(tmp_path):
    # Half the turns hit a failing executor, half succeed — fired together.
    class _FlakyExecutor:
        async def run(self, step, request):
            if request.get("request_id", "").endswith("-fail"):
                return make_result(step, status="failed", error="chaos")
            return make_result(step, status="success", output={"ok": True})

    reg = _registry(tmp_path, agents=[_agent("uc_a")])
    graph = build_executor_graph(_StubRouter(_routed()), reg,
                                 step_executor=_FlakyExecutor())
    envelopes = [
        _envelope(request_id=f"r-{i}-{'fail' if i % 2 else 'ok'}",
                  session_id=f"s-{i}")
        for i in range(30)
    ]
    results = await asyncio.gather(
        *(run_turn(graph, e) for e in envelopes), return_exceptions=True)

    # Nothing crashed — every turn returned a terminal status.
    assert not [r for r in results if isinstance(r, BaseException)]
    statuses = {r["final_status"] for r in results}
    assert statuses == {"executed", "failed"}        # both outcomes, all clean
