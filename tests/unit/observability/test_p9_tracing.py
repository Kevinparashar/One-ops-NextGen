"""P9 — end-to-end trace verification.

The exit criterion: one exemplar trace traverses the whole pipeline with no
broken span links. There is no OTel collector here (no docker), so the test
captures spans with an in-memory exporter and asserts the span *tree* is
connected — a root span exists and every other span's parent is also in the
trace (no orphans).
"""
from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from oneops.executor.graph import build_executor_graph, run_turn
from oneops.observability import (
    current_traceparent,
    extract_trace_context,
    inject_trace_headers,
    safe_hash_text,
)
from oneops.registry.models import (
    AbacTags,
    ActivationCondition,
    AgentRecord,
    ConditionOperator,
    ConditionSignal,
    DeterminismLevel,
    ExecutionTier,
    RoutingShape,
)
from oneops.registry.service import RegistryService
from oneops.registry.store import FileBackend
from oneops.router.plan import PlanStep, RoutePlan, RouteResult
from oneops.session import InMemoryEventLog, InMemoryHotWindow, SessionEventStore

# ── helpers ──────────────────────────────────────────────────────────────


def _agent(agent_id):
    return AgentRecord(
        id=agent_id, version=1, owner="team-test", description="A test agent.",
        intent_family="testing", routing_shape=RoutingShape.SINGLE,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF, signal=ConditionSignal.INTENT_IN,
            values=("summary",)),
        abac_tags=AbacTags(tier=ExecutionTier.READ),
        determinism_level=DeterminismLevel.LOW)


def _registry(tmp_path, agent_ids):
    svc = RegistryService(FileBackend(tmp_path))
    for aid in agent_ids:
        svc.agents.create(_agent(aid))
        svc.agents.activate(aid, 1)
    return svc


class _StubRouter:
    def __init__(self, result):
        self._result = result

    async def route(self, query_text, *, principal, signals,
                    conversation_history=None, request_ctx=None):
        return self._result


def _routed_two_steps():
    plan = RoutePlan(steps=(
        PlanStep(step_id="step_1", agent_id="uc_a"),
        PlanStep(step_id="step_2", agent_id="uc_b"),
    ))
    return RouteResult.routed(plan, ["d"])


# ── exemplar trace ───────────────────────────────────────────────────────


async def test_exemplar_trace_is_one_connected_tree(tmp_path):
    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    assert hasattr(provider, "add_span_processor"), "expected an SDK TracerProvider"
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    exporter.clear()

    reg = _registry(tmp_path, ["uc_a", "uc_b"])
    store = SessionEventStore(InMemoryEventLog(), InMemoryHotWindow())
    graph = build_executor_graph(_StubRouter(_routed_two_steps()), reg,
                                 session_store=store)
    await run_turn(graph, {
        "request_id": "r-trace", "tenant_id": "t-a", "session_id": "s-trace",
        "user_id": "u-1", "role": "service_desk_agent",
        "message": "summarize INC0048213"})

    spans = exporter.get_finished_spans()
    by_id = {s.context.span_id: s for s in spans}
    names = {s.name for s in spans}

    # The whole turn produced a trace with a single root and the key hops.
    roots = [s for s in spans if s.parent is None or s.parent.span_id not in by_id]
    assert len(roots) == 1, f"expected exactly one root span, got {len(roots)}"
    assert roots[0].name == "oneops.request"
    assert {"executor.route", "executor.run_step", "executor.load_session"} <= names

    # No broken links: every non-root span's parent is also in the trace.
    orphans = [s.name for s in spans
               if s is not roots[0] and s.parent.span_id not in by_id]
    assert not orphans, f"orphan span(s) with no parent in the trace: {orphans}"

    # The two fanned-out steps both produced a run_step span in this one trace.
    trace_ids = {s.context.trace_id for s in spans}
    assert len(trace_ids) == 1, "the whole turn must be a single trace"
    assert sum(1 for s in spans if s.name == "executor.run_step") == 2


async def test_run_step_spans_carry_latency(tmp_path):
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    exporter.clear()
    reg = _registry(tmp_path, ["uc_a"])
    graph = build_executor_graph(
        _StubRouter(RouteResult.routed(
            RoutePlan(steps=(PlanStep(step_id="step_1", agent_id="uc_a"),)), ["d"])),
        reg)
    await run_turn(graph, {"request_id": "r", "tenant_id": "t", "session_id": "s",
                           "user_id": "u", "role": "service_desk_agent",
                           "message": "go"})
    run_steps = [s for s in exporter.get_finished_spans()
                 if s.name == "executor.run_step"]
    assert run_steps
    assert "executor.latency_ms" in run_steps[0].attributes


# ── trace-context propagation ────────────────────────────────────────────


def test_inject_then_extract_round_trips_the_trace():
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("producer"):
        headers = inject_trace_headers({"existing": "header"})
        traceparent = current_traceparent()
    assert "traceparent" in headers
    assert headers["existing"] == "header"          # existing headers preserved
    assert traceparent.startswith("00-")            # W3C traceparent format

    # The receiver continues the trace from the headers.
    ctx = extract_trace_context(headers)
    span_ctx = trace.get_current_span(ctx).get_span_context()
    assert span_ctx.is_valid


def test_extract_tolerates_missing_headers():
    # No traceparent → an empty context, never a raise.
    extract_trace_context(None)
    extract_trace_context({})


# ── PII safety ───────────────────────────────────────────────────────────


async def test_root_span_does_not_carry_raw_message(tmp_path):
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    exporter.clear()
    reg = _registry(tmp_path, ["uc_a"])
    graph = build_executor_graph(
        _StubRouter(RouteResult.routed(
            RoutePlan(steps=(PlanStep(step_id="step_1", agent_id="uc_a"),)), ["d"])),
        reg)
    secret = "my SSN is 123-45-6789 — do not leak this"
    await run_turn(graph, {"request_id": "r", "tenant_id": "t", "session_id": "s",
                           "user_id": "u", "role": "service_desk_agent",
                           "message": secret})

    root = next(s for s in exporter.get_finished_spans() if s.name == "oneops.request")
    attrs = dict(root.attributes)
    # The raw message must not appear; only its hash + length.
    assert secret not in str(attrs)
    assert "123-45-6789" not in str(attrs)
    assert attrs["oneops.message_hash"] == safe_hash_text(secret)
    assert attrs["oneops.message_len"] == len(secret)
