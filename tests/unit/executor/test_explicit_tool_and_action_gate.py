"""Phase 2b-i — generic executor extensions for multi-tool plans.

Two additive, backward-compatible behaviours that let a single agent run a
multi-step, multi-tool plan on the MAIN executor (UC-5 triage is the first
consumer; the mechanism is UC-agnostic):

  1. Explicit tool selection: a plan step may name `tool_id`; the step runner
     uses exactly that tool (when bound to the agent), instead of the
     parameter-shape `_pick_tool` heuristic — necessary when several tools on
     one agent share a required-param shape. Absent ⇒ `_pick_tool` (unchanged).

  2. Per-tool action gate: when a step names a `tool_id`, the approval
     `interrupt()` is gated on THAT TOOL's `execution_type`, not the agent
     tier. An action-tier agent may own read tools (propose) and action tools
     (apply); only the action tools require approval. No tool_id ⇒ agent tier
     (chat path unchanged — locked by the existing test_graph interrupt tests).
"""
from __future__ import annotations

from langgraph.types import Command

from oneops.executor.graph import build_executor_graph, run_turn
from oneops.executor.step_runner import HandlerStepExecutor
from oneops.registry.models import (
    AbacTags,
    ActivationCondition,
    AgentRecord,
    ConditionOperator,
    ConditionSignal,
    DeterminismLevel,
    ExecutionTier,
    Hooks,
    RecordStatus,
    RoutingShape,
    ToolParameter,
    ToolRecord,
    ToolRef,
)
from oneops.registry.service import RegistryService
from oneops.registry.store import FileBackend
from oneops.toolrunner.resolver import HandlerResolver

# ── builders ─────────────────────────────────────────────────────────────


def _tool(tool_id, handler_ref, *, execution_type="read", required=("ticket_id",)):
    return ToolRecord(
        id=tool_id, version=1, status=RecordStatus.ACTIVE, owner="team-test",
        description=f"Test tool {tool_id}.", handler_ref=handler_ref,
        execution_type=execution_type,
        parameters=tuple(
            ToolParameter(name=n, type="str", required=True, description=n)
            for n in required),
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF,
            signal=ConditionSignal.INTENT_IN, values=("summary",)),
    )


def _agent(agent_id, tool_ids, *, tier=ExecutionTier.READ, before_hooks=()):
    return AgentRecord(
        id=agent_id, version=1, status=RecordStatus.ACTIVE, owner="team-test",
        description="A test agent that owns several tools.",
        intent_family="testing", routing_shape=RoutingShape.SINGLE,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF,
            signal=ConditionSignal.INTENT_IN, values=("summary",)),
        tool_refs=tuple(ToolRef(tool_id=t) for t in tool_ids),
        abac_tags=AbacTags(tier=tier), determinism_level=DeterminismLevel.LOW,
        hooks=Hooks(before_invocation=before_hooks))


def _registry(tmp_path, *, agents=(), tools=()):
    svc = RegistryService(FileBackend(tmp_path))
    for t in tools:
        svc.tools.create(t); svc.tools.activate(t.id, 1)
    for a in agents:
        svc.agents.create(a); svc.agents.activate(a.id, 1)
    return svc


def _envelope(**over):
    base = dict(request_id="r-1", tenant_id="t-a", session_id="s-1",
                user_id="u-1", role="service_desk_agent", message="x")
    base.update(over)
    return base


# ════════════════════════════════════════════════════════════════════════
# 1. Explicit tool selection (step-runner level)
# ════════════════════════════════════════════════════════════════════════


async def test_explicit_tool_id_selects_named_tool_over_param_shape(tmp_path):
    """Two tools share the SAME required-param shape (ticket_id). The step
    names tool_id=second — the runner must call it, NOT the shape/primary pick."""
    calls = []

    async def first_handler(arguments, context):
        calls.append("first"); return {"who": "first"}

    async def second_handler(arguments, context):
        calls.append("second"); return {"who": "second"}

    resolver = HandlerResolver()
    resolver.register("test:first", first_handler)
    resolver.register("test:second", second_handler)
    t1 = _tool("first_tool", "test:first")
    t2 = _tool("second_tool", "test:second")     # same required shape
    agent = _agent("uc_multi", ["first_tool", "second_tool"])
    reg = _registry(tmp_path, agents=[agent], tools=[t1, t2])

    runner = HandlerStepExecutor(registry=reg, resolver=resolver)
    step = {"step_id": "s1", "agent_id": "uc_multi",
            "tool_id": "second_tool",
            "parameters": {"ticket_id": "INC1"}, "depends_on": []}
    result = await runner.run(step, _envelope())
    assert result["status"] == "success"
    assert result["output"] == {"who": "second"}
    assert result["tool_id"] == "second_tool"
    assert calls == ["second"]


async def test_explicit_tool_id_not_bound_to_agent_fails_loud(tmp_path):
    """A tool_id the agent does not own must fail loud — never silently fall
    through to a different tool."""
    resolver = HandlerResolver()
    resolver.register("test:only", lambda a, c: None)
    t = _tool("only_tool", "test:only")
    agent = _agent("uc_one", ["only_tool"])
    reg = _registry(tmp_path, agents=[agent], tools=[t])

    runner = HandlerStepExecutor(registry=reg, resolver=resolver)
    step = {"step_id": "s1", "agent_id": "uc_one",
            "tool_id": "not_owned", "parameters": {}, "depends_on": []}
    result = await runner.run(step, _envelope())
    assert result["status"] == "failed"
    assert "no invokable tool" in result["error"]


async def test_no_tool_id_uses_pick_tool_unchanged(tmp_path):
    """No tool_id ⇒ existing single-tool selection path (zero behaviour change)."""
    async def h(arguments, context):
        return {"ok": True}

    resolver = HandlerResolver()
    resolver.register("test:h", h)
    t = _tool("solo_tool", "test:h")
    agent = _agent("uc_solo", ["solo_tool"])
    reg = _registry(tmp_path, agents=[agent], tools=[t])

    runner = HandlerStepExecutor(registry=reg, resolver=resolver)
    step = {"step_id": "s1", "agent_id": "uc_solo",
            "parameters": {"ticket_id": "INC1"}, "depends_on": []}
    result = await runner.run(step, _envelope())
    assert result["status"] == "success"
    assert result["tool_id"] == "solo_tool"


# ════════════════════════════════════════════════════════════════════════
# 2. Per-tool action gate (graph level, via fast-path pre-built plan)
# ════════════════════════════════════════════════════════════════════════


def _fast_plan(*steps):
    """steps: dicts with step_id/agent_id/tool_id/depends_on."""
    return list(steps)


async def test_read_tool_under_action_agent_does_not_interrupt(tmp_path):
    """An action-tier agent running a READ tool (e.g. UC-5 propose) must NOT
    hit the approval interrupt — it runs straight to completion."""
    agent = _agent("uc_action", ["read_tool"], tier=ExecutionTier.ACTION,
                   before_hooks=("hook_state_validate",))
    read_tool = _tool("read_tool", "test:noop", execution_type="read")
    reg = _registry(tmp_path, agents=[agent], tools=[read_tool])
    graph = build_executor_graph(_StubRouterNone(), reg)   # echo executor
    config = {"configurable": {"thread_id": "s-rt"}, "recursion_limit": 60}

    env = _envelope(session_id="s-rt", entry_mode="fast_path",
                    plan=[{"step_id": "s1", "agent_id": "uc_action",
                           "tool_id": "read_tool", "parameters": {},
                           "depends_on": []}])
    out = await run_turn(graph, env, config=config)
    assert "__interrupt__" not in out                 # no approval needed
    assert out["final_status"] == "executed"
    assert out["step_results"][0]["status"] == "success"


async def test_action_tool_under_action_agent_interrupts(tmp_path):
    """An ACTION tool still gates on approval — the interrupt fires."""
    agent = _agent("uc_action", ["apply_tool"], tier=ExecutionTier.ACTION,
                   before_hooks=("hook_state_validate",))
    apply_tool = _tool("apply_tool", "test:noop", execution_type="action")
    reg = _registry(tmp_path, agents=[agent], tools=[apply_tool])
    graph = build_executor_graph(_StubRouterNone(), reg)
    config = {"configurable": {"thread_id": "s-at"}, "recursion_limit": 60}

    env = _envelope(session_id="s-at", entry_mode="fast_path",
                    plan=[{"step_id": "s1", "agent_id": "uc_action",
                           "tool_id": "apply_tool", "parameters": {},
                           "depends_on": []}])
    paused = await run_turn(graph, env, config=config)
    assert "__interrupt__" in paused                  # approval required
    done = await graph.ainvoke(Command(resume={"approved": True}), config=config)
    assert done["step_results"][0]["status"] == "success"


class _StubRouterNone:
    """Router never consulted on the fast-path; present to satisfy the builder."""

    async def route(self, *a, **k):  # pragma: no cover - never called
        raise AssertionError("router must not be called on the fast-path")
