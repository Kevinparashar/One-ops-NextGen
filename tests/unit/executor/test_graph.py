"""Executor graph tests — the LangGraph runtime, end to end.

The system under test is the compiled `StateGraph`: the wave loop, `Send`
fan-out, hooks, the interrupt approval gate, aggregation, the boundary path,
and conversational memory. The router is a stub (`_StubRouter`) so each test
controls the plan precisely — the real router has its own 55-test suite.
"""
from __future__ import annotations

from langgraph.types import Command

from oneops.executor.graph import build_executor_graph, run_turn
from oneops.executor.hooks import HookError, default_hook_registry
from oneops.executor.step_runner import make_result
from oneops.registry.models import (
    AbacTags,
    ActivationCondition,
    AgentRecord,
    ConditionOperator,
    ConditionSignal,
    DeterminismLevel,
    ExecutionTier,
    Hooks,
    RoutingShape,
)
from oneops.registry.service import RegistryService
from oneops.registry.store import FileBackend
from oneops.router.plan import PlanStep, RoutePlan, RouteResult
from oneops.session import InMemoryEventLog, InMemoryHotWindow, SessionEventStore

# ── builders ─────────────────────────────────────────────────────────────


def _agent(agent_id, *, tier=ExecutionTier.READ, before_hooks=()):
    return AgentRecord(
        id=agent_id, version=1, owner="team-test",
        description="A test agent that does work.", intent_family="testing",
        routing_shape=RoutingShape.SINGLE,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF, signal=ConditionSignal.INTENT_IN,
            values=("summary",)),
        abac_tags=AbacTags(tier=tier), determinism_level=DeterminismLevel.LOW,
        hooks=Hooks(before_invocation=before_hooks))


def _registry(tmp_path, agents):
    svc = RegistryService(FileBackend(tmp_path))
    for a in agents:
        svc.agents.create(a)
        svc.agents.activate(a.id, 1)
    return svc


def _plan(*steps):
    """steps: (step_id, agent_id, depends_on-tuple)."""
    return RoutePlan(steps=tuple(
        PlanStep(step_id=sid, agent_id=aid, depends_on=tuple(dep))
        for sid, aid, dep in steps))


class _StubRouter:
    """Test double of the P5 Router — returns a preset RouteResult."""

    def __init__(self, result):
        self._result = result

    async def route(self, query_text, *, principal, signals,
                    conversation_history=None, request_ctx=None):
        return self._result


def _envelope(**over):
    base = dict(request_id="r-1", tenant_id="t-a", session_id="s-1",
                user_id="u-1", role="service_desk_agent", message="summarize it")
    base.update(over)
    return base


# ── routed: single / parallel / dependent ────────────────────────────────


async def test_single_step_executes(tmp_path):
    reg = _registry(tmp_path, [_agent("uc_a")])
    router = _StubRouter(RouteResult.routed(_plan(("step_1", "uc_a", ())), ["d"]))
    graph = build_executor_graph(router, reg)
    out = await run_turn(graph, _envelope())
    assert out["final_status"] == "executed"
    results = out["step_results"]
    assert len(results) == 1
    assert results[0]["status"] == "success"
    assert results[0]["agent_id"] == "uc_a"


async def test_parallel_steps_all_execute(tmp_path):
    reg = _registry(tmp_path, [_agent("uc_a"), _agent("uc_b")])
    router = _StubRouter(RouteResult.routed(
        _plan(("step_1", "uc_a", ()), ("step_2", "uc_b", ())), ["d"]))
    graph = build_executor_graph(router, reg)
    out = await run_turn(graph, _envelope())
    assert out["final_status"] == "executed"
    assert {r["agent_id"] for r in out["step_results"]} == {"uc_a", "uc_b"}


async def test_dependent_steps_both_run(tmp_path):
    reg = _registry(tmp_path, [_agent("uc_a"), _agent("uc_b")])
    # step_2 depends on step_1 — the wave loop runs step_1, then step_2.
    router = _StubRouter(RouteResult.routed(
        _plan(("step_1", "uc_a", ()), ("step_2", "uc_b", ("step_1",))), ["d"]))
    graph = build_executor_graph(router, reg)
    out = await run_turn(graph, _envelope())
    assert out["final_status"] == "executed"
    assert {r["agent_id"] for r in out["step_results"]} == {"uc_a", "uc_b"}


# ── non-routed → boundary ────────────────────────────────────────────────


async def test_no_confident_match_routes_to_boundary(tmp_path):
    reg = _registry(tmp_path, [_agent("uc_a")])
    router = _StubRouter(RouteResult.no_match("nothing matched", ["d"]))
    graph = build_executor_graph(router, reg)
    out = await run_turn(graph, _envelope())
    assert out["final_status"] == "clarification"
    assert "not sure how to help" in out["final_response"]
    assert not out.get("step_results")


async def test_policy_denied_routes_to_boundary(tmp_path):
    reg = _registry(tmp_path, [_agent("uc_a")])
    router = _StubRouter(RouteResult.policy_denied("denied by policy", ["d"]))
    graph = build_executor_graph(router, reg)
    out = await run_turn(graph, _envelope())
    assert out["final_status"] == "denied"
    assert "permission" in out["final_response"]


# ── failure / partial ────────────────────────────────────────────────────


async def test_failing_step_executor_yields_failed(tmp_path):
    reg = _registry(tmp_path, [_agent("uc_a")])
    router = _StubRouter(RouteResult.routed(_plan(("step_1", "uc_a", ())), ["d"]))

    class _Failing:
        async def run(self, step, request):
            return make_result(step, status="failed", error="handler boom")

    graph = build_executor_graph(router, reg, step_executor=_Failing())
    out = await run_turn(graph, _envelope())
    assert out["final_status"] == "failed"
    assert out["step_results"][0]["error"] == "handler boom"


async def test_partial_when_a_subquery_is_unrouted(tmp_path):
    reg = _registry(tmp_path, [_agent("uc_a")])
    router = _StubRouter(RouteResult.routed(
        _plan(("step_1", "uc_a", ())), ["d"], unrouted=["launch the rocket"]))
    graph = build_executor_graph(router, reg)
    out = await run_turn(graph, _envelope())
    assert out["final_status"] == "partial"
    assert "launch the rocket" in out["final_response"]


# ── hooks ────────────────────────────────────────────────────────────────


async def test_before_hook_abort_fails_the_step(tmp_path):
    reg = _registry(tmp_path, [_agent("uc_a", before_hooks=("hook_boom",))])
    router = _StubRouter(RouteResult.routed(_plan(("step_1", "uc_a", ())), ["d"]))

    hooks = default_hook_registry()

    async def boom(ctx):
        raise HookError("deliberate gate failure")

    hooks.register("hook_boom", boom)
    graph = build_executor_graph(router, reg, hooks=hooks)
    out = await run_turn(graph, _envelope())
    assert out["final_status"] == "failed"
    assert "before-hook aborted" in out["step_results"][0]["error"]


async def test_unregistered_hook_fails_the_step_loudly(tmp_path):
    reg = _registry(tmp_path, [_agent("uc_a", before_hooks=("hook_ghost",))])
    router = _StubRouter(RouteResult.routed(_plan(("step_1", "uc_a", ())), ["d"]))
    graph = build_executor_graph(router, reg)          # default registry — no hook_ghost
    out = await run_turn(graph, _envelope())
    assert out["final_status"] == "failed"
    assert "not registered" in out["step_results"][0]["error"]


# ── action approval via interrupt() ──────────────────────────────────────


async def test_action_step_interrupts_then_resumes_approved(tmp_path):
    reg = _registry(tmp_path, [
        _agent("uc_close", tier=ExecutionTier.ACTION,
               before_hooks=("hook_state_validate",))])
    router = _StubRouter(RouteResult.routed(_plan(("step_1", "uc_close", ())), ["d"]))
    graph = build_executor_graph(router, reg)
    config = {"configurable": {"thread_id": "s-int-1"}, "recursion_limit": 60}

    paused = await run_turn(graph, _envelope(session_id="s-int-1"), config=config)
    assert "__interrupt__" in paused                   # graph paused for approval

    done = await graph.ainvoke(Command(resume={"approved": True}), config=config)
    assert done["final_status"] == "executed"
    assert done["step_results"][0]["status"] == "success"


async def test_action_step_resume_denied_is_not_executed(tmp_path):
    reg = _registry(tmp_path, [
        _agent("uc_close", tier=ExecutionTier.ACTION,
               before_hooks=("hook_state_validate",))])
    router = _StubRouter(RouteResult.routed(_plan(("step_1", "uc_close", ())), ["d"]))
    graph = build_executor_graph(router, reg)
    config = {"configurable": {"thread_id": "s-int-2"}, "recursion_limit": 60}

    await run_turn(graph, _envelope(session_id="s-int-2"), config=config)
    done = await graph.ainvoke(Command(resume={"approved": False}), config=config)
    assert done["step_results"][0]["status"] == "denied"
    assert done["final_status"] == "failed"            # nothing succeeded


# ── conversational memory ────────────────────────────────────────────────


def _session_store():
    return SessionEventStore(InMemoryEventLog(), InMemoryHotWindow())


async def test_conversation_memory_accumulates_across_turns(tmp_path):
    reg = _registry(tmp_path, [_agent("uc_a")])
    router = _StubRouter(RouteResult.routed(_plan(("step_1", "uc_a", ())), ["d"]))
    store = _session_store()
    graph = build_executor_graph(router, reg, session_store=store)

    # Turn 1 — history is empty on entry.
    t1 = await run_turn(graph, _envelope(session_id="s-mem", message="summarize INC1"))
    assert t1["conversation_history"] == []

    # Turn 2 — load_session sees turn 1's user + assistant events.
    t2 = await run_turn(graph, _envelope(session_id="s-mem", message="and the next one"))
    history = t2["conversation_history"]
    assert len(history) == 2
    assert history[0] == {"role": "user", "content": "summarize INC1"}
    assert history[1]["role"] == "assistant"


async def test_stateless_when_no_session_store(tmp_path):
    reg = _registry(tmp_path, [_agent("uc_a")])
    router = _StubRouter(RouteResult.routed(_plan(("step_1", "uc_a", ())), ["d"]))
    graph = build_executor_graph(router, reg)          # no session_store
    out = await run_turn(graph, _envelope())
    assert out["final_status"] == "executed"
    assert out["conversation_history"] == []


# ── entity-clarification loop closure ─────────────────────────────────────


async def test_all_malformed_entity_refs_short_circuit_to_clarification(tmp_path):
    """Every record ID in the message is malformed — the route node answers
    with a correction request and never consults the router."""
    reg = _registry(tmp_path, [_agent("uc_a")])
    # The router *would* route, but it must never be reached for this turn.
    router = _StubRouter(RouteResult.routed(_plan(("step_1", "uc_a", ())), ["d"]))
    graph = build_executor_graph(router, reg)
    out = await run_turn(graph, _envelope(message="please summarize INCX0048"))
    assert out["route_outcome"] == "entity_clarification"
    assert out["final_status"] == "clarification"
    assert "INCX0048" in out["final_response"]
    assert "incident" in out["final_response"]
    assert "INC0001234" in out["final_response"]


async def test_malformed_ref_alongside_a_valid_id_rides_along_as_a_note(tmp_path):
    """A valid ID is acted on; a malformed one in the same message is appended
    as a correction note — not silently dropped (thumb rule #11)."""
    reg = _registry(tmp_path, [_agent("uc_a")])
    router = _StubRouter(RouteResult.routed(_plan(("step_1", "uc_a", ())), ["d"]))
    graph = build_executor_graph(router, reg)
    out = await run_turn(graph, _envelope(
        message="summarize INC0048213 and INCX0048"))
    assert out["final_status"] == "executed"
    # The valid ID was acted on — friendly response is non-empty and
    # carries the per-step text from `friendly_step_response`. The
    # specific text depends on the (stubbed) handler output; what we
    # guarantee is that the response is populated, NOT a debug line.
    assert out["final_response"]                      # success-side present
    assert not out["final_response"].startswith("- ")  # legacy debug shape gone
    assert "INCX0048" in out["final_response"]        # the bad one is surfaced


async def test_clean_message_has_no_clarification_note(tmp_path):
    """A message with only valid IDs gets no spurious correction note."""
    reg = _registry(tmp_path, [_agent("uc_a")])
    router = _StubRouter(RouteResult.routed(_plan(("step_1", "uc_a", ())), ["d"]))
    graph = build_executor_graph(router, reg)
    out = await run_turn(graph, _envelope(message="summarize INC0048213"))
    assert out["final_status"] == "executed"
    assert "look" not in out["final_response"]      # no "looks like ... ID"


# ── fast-path direct-plan entry (Phase F1) ───────────────────────────────


class _ExplodingRouter:
    """Router that fails if called. The fast-path entry MUST skip routing —
    this stub proves the router is not consulted on the fast-path code path."""

    async def route(self, *args, **kwargs):
        raise AssertionError(
            "router.route was called on the fast-path; expected to be skipped")


async def test_fast_path_skips_router_and_executes_directly(tmp_path):
    """When the ingress (e.g. /fast/uc_a) supplies `plan` + route_outcome=
    "routed" on the input state, the executor runs the plan without touching
    the router. Every downstream stage (load_session, hooks, run_step,
    aggregate, persist) still runs."""
    reg = _registry(tmp_path, [_agent("uc_a")])
    graph = build_executor_graph(_ExplodingRouter(), reg)   # router would explode
    envelope = _envelope(message="(fast-path)")
    envelope["plan"] = [
        {"step_id": "step_1", "agent_id": "uc_a",
         "parameters": {"ticket_id": "INC0048213"}, "depends_on": []}
    ]
    envelope["entry_mode"] = "fast_path"
    out = await run_turn(graph, envelope)
    assert out["final_status"] == "executed"
    assert len(out["step_results"]) == 1
    assert out["step_results"][0]["agent_id"] == "uc_a"


async def test_fast_path_runs_before_hooks(tmp_path):
    """The fast-path skips ONLY routing — before-hooks (e.g. authz_recheck)
    still run. Proven by declaring a hook that aborts and observing the turn
    fail (not bypass)."""
    reg = _registry(tmp_path, [_agent("uc_a", before_hooks=("always_deny",))])
    hooks = default_hook_registry()

    async def always_deny(_ctx):
        raise HookError("denied by test hook")

    hooks.register("always_deny", always_deny)
    graph = build_executor_graph(_ExplodingRouter(), reg, hooks=hooks)
    envelope = _envelope(message="(fast-path)")
    envelope["plan"] = [
        {"step_id": "step_1", "agent_id": "uc_a",
         "parameters": {}, "depends_on": []}
    ]
    envelope["entry_mode"] = "fast_path"
    out = await run_turn(graph, envelope)
    # The step failed at the hook; final_status reports it.
    assert out["step_results"][0]["status"] == "failed"
    assert "denied by test hook" in out["step_results"][0]["error"]


async def test_fast_path_then_chat_followup_continues_same_session(tmp_path):
    """Multi-turn invariant: a fast-path turn writes to the session store
    just like a chat turn. A subsequent chat message loads that history so
    references ("it") can resolve."""
    reg = _registry(tmp_path, [_agent("uc_a")])
    session_store = SessionEventStore(
        cold=InMemoryEventLog(), hot=InMemoryHotWindow())
    captured_history: list = []

    class _RecordingRouter:
        async def route(self, query_text, *, principal, signals,
                        conversation_history=None, request_ctx=None):
            captured_history.append(list(conversation_history or []))
            return RouteResult.routed(_plan(("step_1", "uc_a", ())), ["d"])

    graph = build_executor_graph(
        _RecordingRouter(), reg, session_store=session_store)

    # Turn 1: fast-path
    envelope_1 = _envelope(message="(fast-path: summarize INC0048213)")
    envelope_1["plan"] = [
        {"step_id": "step_1", "agent_id": "uc_a",
         "parameters": {"ticket_id": "INC0048213"}, "depends_on": []}
    ]
    envelope_1["route_outcome"] = "routed"
    out_1 = await run_turn(graph, envelope_1)
    assert out_1["final_status"] == "executed"

    # Turn 2: chat follow-up — router IS consulted, and it sees the prior
    # fast-path turn in history.
    envelope_2 = _envelope(message="root cause of it?")
    out_2 = await run_turn(graph, envelope_2)
    assert out_2["final_status"] == "executed"
    # The router saw at least the user+assistant pair from turn 1.
    assert any(t.content.startswith("(fast-path") or t.role == "assistant"
               for t in captured_history[-1])
