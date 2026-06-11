"""S4 — the slot-filling gate wired into `HandlerStepExecutor.run()`.

Drives the REAL step executor (registry + tool with a required `ticket_id` +
handler). Proves the flag contract and every gate branch:
  * flag OFF            → gate is a no-op; handler runs as today (zero regression)
  * flag ON, missing    → elicits; resolved bindings are merged before dispatch
  * flag ON, first pass → the interrupt propagates (turn pauses, no failure)
  * flag ON, has focus  → skipped (never interrupt mid-conversation)
  * flag ON, already bound → skipped (no needless ask)
The elicitor itself is unit-tested in test_entity_elicitation_*; here it is
monkeypatched so the gate's wiring is what's under test.
"""
from __future__ import annotations

import pytest
from langgraph.errors import GraphInterrupt

from oneops.executor import entity_elicitation
from oneops.executor.step_runner import (
    HandlerStepExecutor,
    _elicitation_enabled,
    _missing_entity_slot,
)
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

pytestmark = pytest.mark.asyncio

_FLAG = "ONEOPS_ENTITY_ELICITATION_ENABLED"


def _tool():
    return ToolRecord(
        id="t_sum", version=1, status=RecordStatus.ACTIVE, owner="team-test",
        description="Test tool.", handler_ref="test:h", execution_type="read",
        parameters=(
            ToolParameter(name="ticket_id", type="str", required=True,
                          description="A work-record id."),
            ToolParameter(name="service_id", type="str", required=True,
                          description="Service module."),
        ),
        timeout_ms=30_000,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF,
            signal=ConditionSignal.INTENT_IN, values=("summary",)),
    )


def _agent():
    return AgentRecord(
        id="uc_sum", version=1, status=RecordStatus.ACTIVE, owner="team-test",
        description="Test UC.", intent_family="summary",
        routing_shape=RoutingShape.SINGLE,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF,
            signal=ConditionSignal.INTENT_IN, values=("summary",)),
        tool_refs=(ToolRef(tool_id="t_sum"),),
        abac_tags=AbacTags(tier=ExecutionTier.READ),
        determinism_level=DeterminismLevel.LOW,
        fast_path=FastPathSpec(
            enabled=True, primary_tool_id="t_sum",
            input_fields=(FastPathInputField(
                name="ticket_id", type="str", required=True, description="…"),),
        ),
    )


@pytest.fixture
def runner_and_calls(tmp_path):
    svc = RegistryService(FileBackend(tmp_path))
    tool, agent = _tool(), _agent()
    svc.tools.create(tool); svc.tools.activate(tool.id, 1)
    svc.agents.create(agent); svc.agents.activate(agent.id, 1)
    calls: list[dict] = []

    async def handler(arguments, context):     # noqa: ANN001
        calls.append(dict(arguments))
        return {"outcome": "ok", "ticket_id": arguments.get("ticket_id")}

    resolver = HandlerResolver()
    resolver.register("test:h", handler)
    return HandlerStepExecutor(registry=svc, resolver=resolver), calls


def _env(**over):
    base = {"request_id": "r", "tenant_id": "T001", "session_id": "s",
            "user_id": "U1", "role": "service_desk_agent",
            "message": "summarize my ticket"}
    base.update(over)
    return base


def _step(**params):
    return {"step_id": "s1", "agent_id": "uc_sum",
            "parameters": params, "depends_on": []}


# ── flag contract ────────────────────────────────────────────────────────


async def test_flag_off_never_elicits(runner_and_calls, monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "false")        # explicitly OFF (default is now ON)
    called = {"n": 0}

    async def spy(**_kw):
        called["n"] += 1
        return {"ticket_id": "INC0000001"}
    monkeypatch.setattr(entity_elicitation, "maybe_elicit_entity", spy)

    runner, calls = runner_and_calls
    await runner.run(_step(), _env())             # no ticket_id, flag OFF
    assert called["n"] == 0                        # gate skipped wholesale
    assert calls and calls[0].get("ticket_id") in (None, "")  # handler ran as today


async def test_flag_on_missing_slot_elicits_and_binds(
        runner_and_calls, monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "true")

    async def fake(**kw):
        assert kw["param_name"] == "ticket_id"
        assert kw["service_param"] == "service_id"
        return {"ticket_id": "INC0000002", "service_id": "incident"}
    monkeypatch.setattr(entity_elicitation, "maybe_elicit_entity", fake)

    runner, calls = runner_and_calls
    res = await runner.run(_step(), _env())
    assert res["status"] == "success"
    assert calls[0]["ticket_id"] == "INC0000002"   # resolved binding merged
    assert calls[0]["service_id"] == "incident"


async def test_flag_on_first_pass_interrupt_propagates(
        runner_and_calls, monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "true")

    async def pause(**_kw):
        raise GraphInterrupt(())                    # the question — turn pauses
    monkeypatch.setattr(entity_elicitation, "maybe_elicit_entity", pause)

    runner, _ = runner_and_calls
    with pytest.raises(GraphInterrupt):
        await runner.run(_step(), _env())


async def test_flag_on_with_focus_is_skipped(
        runner_and_calls, monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "true")
    called = {"n": 0}

    async def spy(**_kw):
        called["n"] += 1
        return None
    monkeypatch.setattr(entity_elicitation, "maybe_elicit_entity", spy)

    runner, calls = runner_and_calls
    # a focus subject is in scope → the existing focus path owns it, no ask
    await runner.run(_step(), _env(focus_entity_id="INC0000009",
                                   focus_service_id="incident"))
    assert called["n"] == 0 and calls            # handler still ran


async def test_flag_on_already_bound_is_skipped(
        runner_and_calls, monkeypatch) -> None:
    monkeypatch.setenv(_FLAG, "true")
    called = {"n": 0}

    async def spy(**_kw):
        called["n"] += 1
        return None
    monkeypatch.setattr(entity_elicitation, "maybe_elicit_entity", spy)

    runner, calls = runner_and_calls
    await runner.run(_step(ticket_id="INC0000007", service_id="incident"),
                     _env(message="summarize INC0000007"))
    assert called["n"] == 0                        # nothing missing → no ask
    assert calls[0]["ticket_id"] == "INC0000007"


# ── detection + flag units ────────────────────────────────────────────────


async def test_missing_entity_slot_detects_and_pairs_service() -> None:
    assert _missing_entity_slot(_tool(), {}) == ("ticket_id", "service_id")
    assert _missing_entity_slot(_tool(), {"ticket_id": "INC1"}) is None
    # empty string counts as unbound
    assert _missing_entity_slot(_tool(), {"ticket_id": ""}) == (
        "ticket_id", "service_id")


async def test_flag_reader(monkeypatch) -> None:
    # default graduated to ON (2026-06-11)
    monkeypatch.delenv(_FLAG, raising=False)
    assert _elicitation_enabled() is True
    monkeypatch.setenv(_FLAG, "false")
    assert _elicitation_enabled() is False
    monkeypatch.setenv(_FLAG, "true")
    assert _elicitation_enabled() is True
