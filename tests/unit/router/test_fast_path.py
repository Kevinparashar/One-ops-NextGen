"""Fast-path dispatcher — Phase F1.

Verifies the generalised dispatcher refuses every malformed call with a
typed `FastPathError` (no silent pass-through), accepts valid input, and is
fully registry-driven (no per-UC branches).
"""
from __future__ import annotations

import pytest

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
    ToolRef,
)
from oneops.registry.service import RegistryService
from oneops.registry.store import FileBackend
from oneops.router.fast_path import (
    FastPathDispatcher,
    FastPathError,
    FastPathRequest,
)


def _make_fast_path_agent(*, agent_id="uc01_summarization",
                          tool_id="get_ticket_details",
                          fields=None,
                          fast_path_enabled=True,
                          status=RecordStatus.ACTIVE) -> AgentRecord:
    if fields is None:
        fields = (
            FastPathInputField(name="ticket_id", type="str", required=True,
                               description="Canonical work-record id."),
            FastPathInputField(name="service_id", type="str", required=True,
                               description="Service module."),
        )
    return AgentRecord(
        id=agent_id, version=1, status=status,
        owner="team-itsm",
        description="Fast-path summarization use case for tests.",
        intent_family="summary",
        routing_shape=RoutingShape.SINGLE,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF,
            signal=ConditionSignal.INTENT_IN,
            values=("summary",)),
        tool_refs=(ToolRef(tool_id=tool_id),),
        abac_tags=AbacTags(tier=ExecutionTier.READ),
        determinism_level=DeterminismLevel.LOW,
        fast_path=FastPathSpec(
            enabled=fast_path_enabled,
            primary_tool_id=tool_id,
            input_fields=fields,
        ),
    )


def _make_chat_only_agent(agent_id="uc99_chatonly") -> AgentRecord:
    return AgentRecord(
        id=agent_id, version=1, status=RecordStatus.ACTIVE,
        owner="team-itsm",
        description="Chat-only use case (no fast-path declared).",
        intent_family="chat",
        routing_shape=RoutingShape.SINGLE,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF,
            signal=ConditionSignal.INTENT_IN,
            values=("chat",)),
        abac_tags=AbacTags(tier=ExecutionTier.READ),
        determinism_level=DeterminismLevel.LOW,
    )


def _service_with(*agents: AgentRecord, tmp_path) -> RegistryService:
    backend = FileBackend(tmp_path)
    for a in agents:
        backend.write("agents", a.id, {
            "id": a.id, "versions": {"1": a.model_dump(mode="json")},
            "active_version": 1,
        })
    return RegistryService(backend)


# ── happy path ───────────────────────────────────────────────────────────


def test_dispatch_builds_a_single_step_plan_for_a_valid_call(tmp_path):
    service = _service_with(_make_fast_path_agent(), tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    out = disp.dispatch(FastPathRequest(
        uc_id="uc01_summarization",
        inputs={"ticket_id": "INC0048213", "service_id": "incident"}))
    assert len(out.plan.steps) == 1
    step = out.plan.steps[0]
    assert step.agent_id == "uc01_summarization"
    assert step.depends_on == ()
    assert dict(step.parameters) == {
        "ticket_id": "INC0048213", "service_id": "incident",
    }
    assert out.parameters == {
        "ticket_id": "INC0048213", "service_id": "incident",
    }


# ── eligibility / discovery ──────────────────────────────────────────────


def test_is_eligible_returns_true_for_fast_path_uc(tmp_path):
    service = _service_with(_make_fast_path_agent(), tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    assert disp.is_eligible("uc01_summarization") is True


def test_is_eligible_returns_false_for_chat_only_uc(tmp_path):
    service = _service_with(
        _make_fast_path_agent(),
        _make_chat_only_agent(),
        tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    assert disp.is_eligible("uc99_chatonly") is False


def test_is_eligible_returns_false_for_unknown_uc(tmp_path):
    service = _service_with(_make_fast_path_agent(), tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    assert disp.is_eligible("uc_does_not_exist") is False


def test_describe_returns_spec_for_eligible_uc(tmp_path):
    service = _service_with(_make_fast_path_agent(), tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    spec = disp.describe("uc01_summarization")
    assert spec is not None
    assert spec.primary_tool_id == "get_ticket_details"
    assert {f.name for f in spec.input_fields} == {"ticket_id", "service_id"}


def test_describe_returns_none_for_chat_only_uc(tmp_path):
    service = _service_with(
        _make_fast_path_agent(),
        _make_chat_only_agent(),
        tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    assert disp.describe("uc99_chatonly") is None


# ── refusal — every malformed call is a typed error ──────────────────────


def test_dispatch_refuses_blank_uc_id(tmp_path):
    service = _service_with(_make_fast_path_agent(), tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    with pytest.raises(FastPathError, match="uc_id is required"):
        disp.dispatch(FastPathRequest(uc_id="", inputs={}))


def test_dispatch_refuses_unknown_uc(tmp_path):
    service = _service_with(_make_fast_path_agent(), tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    with pytest.raises(FastPathError, match="unknown use case"):
        disp.dispatch(FastPathRequest(uc_id="uc_missing", inputs={}))


def test_dispatch_refuses_retired_uc(tmp_path):
    agent = _make_fast_path_agent(status=RecordStatus.RETIRED)
    service = _service_with(agent, tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    with pytest.raises(FastPathError, match="not active"):
        disp.dispatch(FastPathRequest(
            uc_id=agent.id,
            inputs={"ticket_id": "INC0048213", "service_id": "incident"}))


def test_dispatch_refuses_chat_only_uc(tmp_path):
    service = _service_with(
        _make_fast_path_agent(),
        _make_chat_only_agent(),
        tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    with pytest.raises(FastPathError, match="does not expose a fast-path"):
        disp.dispatch(FastPathRequest(uc_id="uc99_chatonly", inputs={}))


def test_dispatch_refuses_disabled_fast_path(tmp_path):
    # An eligible UC whose fast_path was disabled via registry rollback —
    # the dispatcher must refuse just like an unset one.
    service = _service_with(
        _make_fast_path_agent(fast_path_enabled=False),
        tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    with pytest.raises(FastPathError, match="does not expose a fast-path"):
        disp.dispatch(FastPathRequest(
            uc_id="uc01_summarization",
            inputs={"ticket_id": "INC0048213", "service_id": "incident"}))


def test_dispatch_refuses_missing_required_field(tmp_path):
    service = _service_with(_make_fast_path_agent(), tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    with pytest.raises(FastPathError, match="requires fields"):
        disp.dispatch(FastPathRequest(
            uc_id="uc01_summarization",
            inputs={"ticket_id": "INC0048213"}))         # service_id missing


def test_dispatch_treats_blank_string_as_missing(tmp_path):
    service = _service_with(_make_fast_path_agent(), tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    with pytest.raises(FastPathError, match="requires fields"):
        disp.dispatch(FastPathRequest(
            uc_id="uc01_summarization",
            inputs={"ticket_id": "INC0048213", "service_id": "   "}))


def test_dispatch_refuses_unknown_fields_no_silent_passthrough(tmp_path):
    service = _service_with(_make_fast_path_agent(), tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    with pytest.raises(FastPathError, match="unknown fast-path fields"):
        disp.dispatch(FastPathRequest(
            uc_id="uc01_summarization",
            inputs={"ticket_id": "INC0048213", "service_id": "incident",
                    "secret_field": "haha"}))


# ── optional fields — explicit, not silent ───────────────────────────────


def test_optional_field_is_accepted_when_omitted(tmp_path):
    fields = (
        FastPathInputField(name="ticket_id", type="str", required=True,
                           description="Required ticket id."),
        FastPathInputField(name="locale", type="str", required=False,
                           description="Optional caller locale."),
    )
    service = _service_with(
        _make_fast_path_agent(fields=fields), tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    out = disp.dispatch(FastPathRequest(
        uc_id="uc01_summarization", inputs={"ticket_id": "INC0048213"}))
    assert dict(out.plan.steps[0].parameters) == {"ticket_id": "INC0048213"}
    assert "locale" not in out.parameters


def test_optional_field_is_threaded_when_supplied(tmp_path):
    fields = (
        FastPathInputField(name="ticket_id", type="str", required=True,
                           description="Required ticket id."),
        FastPathInputField(name="locale", type="str", required=False,
                           description="Optional caller locale."),
    )
    service = _service_with(
        _make_fast_path_agent(fields=fields), tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    out = disp.dispatch(FastPathRequest(
        uc_id="uc01_summarization",
        inputs={"ticket_id": "INC0048213", "locale": "fr"}))
    assert out.parameters["locale"] == "fr"


# ── type coercion — strict gate, not permissive parser ───────────────────


def test_int_field_is_coerced_from_string(tmp_path):
    fields = (
        FastPathInputField(name="limit", type="int", required=True,
                           description="Result limit."),
    )
    service = _service_with(
        _make_fast_path_agent(fields=fields), tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    out = disp.dispatch(FastPathRequest(
        uc_id="uc01_summarization", inputs={"limit": "25"}))
    assert out.parameters["limit"] == 25                 # int, not str


def test_int_field_rejects_non_numeric(tmp_path):
    fields = (
        FastPathInputField(name="limit", type="int", required=True,
                           description="Result limit."),
    )
    service = _service_with(
        _make_fast_path_agent(fields=fields), tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    with pytest.raises(FastPathError, match="expects an int"):
        disp.dispatch(FastPathRequest(
            uc_id="uc01_summarization", inputs={"limit": "abc"}))


def test_bool_field_accepts_common_truthy_strings(tmp_path):
    fields = (
        FastPathInputField(name="include_drafts", type="bool", required=True,
                           description="Include drafts?"),
    )
    service = _service_with(
        _make_fast_path_agent(fields=fields), tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    for v in ("true", "1", "yes"):
        out = disp.dispatch(FastPathRequest(
            uc_id="uc01_summarization", inputs={"include_drafts": v}))
        assert out.parameters["include_drafts"] is True


def test_bool_field_rejects_nonsense(tmp_path):
    fields = (
        FastPathInputField(name="include_drafts", type="bool", required=True,
                           description="Include drafts?"),
    )
    service = _service_with(
        _make_fast_path_agent(fields=fields), tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    with pytest.raises(FastPathError, match="expects a bool"):
        disp.dispatch(FastPathRequest(
            uc_id="uc01_summarization", inputs={"include_drafts": "maybe"}))


# ── cross-UC isolation — dispatcher serves every UC the same way ────────


def test_dispatcher_serves_multiple_ucs_with_no_per_uc_branch(tmp_path):
    a = _make_fast_path_agent(agent_id="uc01_summarization",
                               tool_id="get_ticket_details")
    b = _make_fast_path_agent(
        agent_id="uc09_sentiment", tool_id="run_sentiment",
        fields=(
            FastPathInputField(name="ticket_id", type="str", required=True,
                               description="Canonical work-record id."),
        ))
    service = _service_with(a, b, tmp_path=tmp_path)
    disp = FastPathDispatcher(service)
    # Each UC keeps its own schema; the dispatcher swaps which agent the
    # plan targets based purely on the registry record.
    out_a = disp.dispatch(FastPathRequest(
        uc_id="uc01_summarization",
        inputs={"ticket_id": "INC0048213", "service_id": "incident"}))
    out_b = disp.dispatch(FastPathRequest(
        uc_id="uc09_sentiment",
        inputs={"ticket_id": "INC0048213"}))
    assert out_a.plan.steps[0].agent_id == "uc01_summarization"
    assert out_b.plan.steps[0].agent_id == "uc09_sentiment"
    assert "service_id" not in out_b.parameters


# ── registry-level validation surfaces here too (defence in depth) ──────


def test_fast_path_primary_tool_id_must_be_in_tool_refs():
    # Build the smallest invalid AgentRecord: declares fast_path.primary_tool_id
    # = "missing_tool" but tool_refs only carries "get_ticket_details".
    bad_field = FastPathInputField(
        name="ticket_id", type="str", required=True,
        description="Canonical work-record id.")
    with pytest.raises(ValueError, match="primary_tool_id"):
        AgentRecord(
            id="uc01_summarization", version=1, status=RecordStatus.ACTIVE,
            owner="team-itsm",
            description="Fast-path summarization.",
            intent_family="summary",
            routing_shape=RoutingShape.SINGLE,
            activation_condition=ActivationCondition(
                operator=ConditionOperator.LEAF,
                signal=ConditionSignal.INTENT_IN,
                values=("summary",)),
            tool_refs=(ToolRef(tool_id="get_ticket_details"),),
            abac_tags=AbacTags(tier=ExecutionTier.READ),
            determinism_level=DeterminismLevel.LOW,
            fast_path=FastPathSpec(
                enabled=True,
                primary_tool_id="missing_tool",          # not in tool_refs
                input_fields=(bad_field,),
            ),
        )


def test_fast_path_input_fields_must_be_unique():
    f1 = FastPathInputField(name="ticket_id", type="str", required=True,
                            description="First.")
    f2 = FastPathInputField(name="ticket_id", type="str", required=False,
                            description="Second.")
    with pytest.raises(ValueError, match="duplicate"):
        AgentRecord(
            id="uc01_summarization", version=1, status=RecordStatus.ACTIVE,
            owner="team-itsm",
            description="Fast-path summarization.",
            intent_family="summary",
            routing_shape=RoutingShape.SINGLE,
            activation_condition=ActivationCondition(
                operator=ConditionOperator.LEAF,
                signal=ConditionSignal.INTENT_IN,
                values=("summary",)),
            tool_refs=(ToolRef(tool_id="get_ticket_details"),),
            abac_tags=AbacTags(tier=ExecutionTier.READ),
            determinism_level=DeterminismLevel.LOW,
            fast_path=FastPathSpec(
                enabled=True,
                primary_tool_id="get_ticket_details",
                input_fields=(f1, f2),
            ),
        )
