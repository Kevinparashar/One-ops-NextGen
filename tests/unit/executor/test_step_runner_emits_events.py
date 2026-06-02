"""Enforces the live-activity contract (CONVENTIONS.md "Live activity stream").

The executor's step boundary MUST publish `tool_start` / `tool_done` events
for every step, so every UC on the standard path inherits the live "which
agent + tool is running" view with no per-UC code. If a refactor drops the
publish, these tests fail.
"""
from __future__ import annotations

import pytest

from oneops.executor.step_runner import HandlerStepExecutor
from oneops.observability import event_sink
from oneops.registry.models import (
    AbacTags,
    ActivationCondition,
    AgentRecord,
    ConditionOperator,
    ConditionSignal,
    DeterminismLevel,
    ExecutionTier,
    FastPathInputField,
    FastPathSpec,
    RecordStatus,
    RoutingShape,
    ToolParameter,
    ToolRecord,
    ToolRef,
)
from oneops.registry.service import RegistryService
from oneops.registry.store import FileBackend
from oneops.toolrunner.resolver import HandlerResolver


def _tool() -> ToolRecord:
    return ToolRecord(
        id="test_summarize", version=1, status=RecordStatus.ACTIVE,
        owner="team-test",
        # First sentence is the live "action" line; the rest is dropped.
        description="Synthesise a structured summary. Extra detail ignored.",
        handler_ref="test:h", execution_type="read",
        parameters=(ToolParameter(
            name="ticket_id", type="str", required=True,
            description="A ticket id."),),
        timeout_ms=30_000,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF,
            signal=ConditionSignal.INTENT_IN, values=("summary",)),
    )


def _agent() -> AgentRecord:
    return AgentRecord(
        id="uc_test", version=1, status=RecordStatus.ACTIVE, owner="team-test",
        description="Test UC.", intent_family="summary",
        routing_shape=RoutingShape.SINGLE,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF,
            signal=ConditionSignal.INTENT_IN, values=("summary",)),
        tool_refs=(ToolRef(tool_id="test_summarize"),),
        abac_tags=AbacTags(tier=ExecutionTier.READ),
        determinism_level=DeterminismLevel.LOW,
        fast_path=FastPathSpec(
            enabled=True, primary_tool_id="test_summarize",
            input_fields=(FastPathInputField(
                name="ticket_id", type="str", required=True,
                description="…"),),
        ),
    )


@pytest.fixture
def runner(tmp_path):
    svc = RegistryService(FileBackend(tmp_path))
    t, a = _tool(), _agent()
    svc.tools.create(t)
    svc.tools.activate(t.id, 1)
    svc.agents.create(a)
    svc.agents.activate(a.id, 1)
    resolver = HandlerResolver()

    async def _h(arguments, context):
        return {"outcome": "summarized", "summary": "ok"}

    resolver.register("test:h", _h)
    return HandlerStepExecutor(registry=svc, resolver=resolver)


def _step():
    return {"step_id": "step_1", "agent_id": "uc_test",
            "parameters": {"ticket_id": "INC0001001"}, "depends_on": []}


def _env(request_id: str):
    return {"request_id": request_id, "tenant_id": "T001", "session_id": "s1",
            "user_id": "u", "role": "service_desk_agent", "message": "x"}


async def test_step_boundary_publishes_tool_start_and_done(runner):
    q = event_sink.open_sink("req_evt")
    try:
        result = await runner.run(_step(), _env("req_evt"))
        events = []
        while not q.empty():
            events.append(q.get_nowait())
    finally:
        event_sink.close_sink("req_evt")

    types = [e["type"] for e in events]
    assert "tool_start" in types, types
    assert "tool_done" in types, types

    start = next(e for e in events if e["type"] == "tool_start")
    done = next(e for e in events if e["type"] == "tool_done")
    # Names the REAL agent + REAL tool the executor invoked.
    assert start["agent_id"] == "uc_test"
    assert start["tool_id"] == "test_summarize"
    # Action is the registry description's first sentence — never hardcoded.
    assert start["action"] == "Synthesise a structured summary"
    assert done["status"] == "success"
    assert isinstance(done["latency_ms"], int)

    # The step result also carries tool_id + latency_ms (response contract).
    assert result["tool_id"] == "test_summarize"
    assert isinstance(result["latency_ms"], int)


async def test_publish_is_noop_without_an_open_sink(runner):
    """Non-streaming turns (no sink) must run unaffected — publish no-ops."""
    result = await runner.run(_step(), _env("req_nosink"))
    assert result["status"] == "success"
    assert "req_nosink" not in event_sink._SINKS
