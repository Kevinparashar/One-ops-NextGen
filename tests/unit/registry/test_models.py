"""Contract tests for the registry record schemas.

These test the *schema*, not an implementation — a record that violates a
declared rule must be impossible to construct. Every validator in models.py
has a test that proves it rejects the bad case AND accepts the good case.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from oneops.registry.models import (
    MAX_DESCRIPTION_CHARS,
    AbacTags,
    ActivationCondition,
    AgentRecord,
    ConditionOperator,
    ConditionSignal,
    DeterminismLevel,
    ExclusionRef,
    ExecutionTier,
    Hooks,
    JourneySlot,
    JourneySpec,
    RoutingShape,
    SchemaRecord,
    ToolParameter,
    ToolRecord,
)


def _leaf(sig=ConditionSignal.INTENT_IN, *vals):
    return ActivationCondition(operator=ConditionOperator.LEAF, signal=sig,
                               values=tuple(vals) or ("summary",))


def _read_agent(**over):
    base = dict(
        id="uc01_summary", version=1, owner="team-itsm",
        description="Summarise an ITSM record.", intent_family="entity_summary",
        routing_shape=RoutingShape.SINGLE, activation_condition=_leaf(),
        abac_tags=AbacTags(tier=ExecutionTier.READ), determinism_level=DeterminismLevel.LOW,
    )
    base.update(over)
    return AgentRecord(**base)


# ── ActivationCondition ──────────────────────────────────────────────────


def test_leaf_condition_requires_a_signal():
    with pytest.raises(ValidationError, match="leaf condition requires"):
        ActivationCondition(operator=ConditionOperator.LEAF)


def test_leaf_membership_signal_requires_values():
    with pytest.raises(ValidationError, match="requires non-empty"):
        ActivationCondition(operator=ConditionOperator.LEAF,
                            signal=ConditionSignal.INTENT_IN, values=())


def test_boolean_signal_needs_no_values():
    cond = ActivationCondition(operator=ConditionOperator.LEAF,
                               signal=ConditionSignal.ENTITY_PRESENT)
    assert cond.signal is ConditionSignal.ENTITY_PRESENT


def test_group_condition_requires_clauses():
    with pytest.raises(ValidationError, match="requires `clauses`"):
        ActivationCondition(operator=ConditionOperator.ALL_OF, clauses=())


def test_group_condition_rejects_leaf_fields():
    with pytest.raises(ValidationError, match="must not carry leaf fields"):
        ActivationCondition(operator=ConditionOperator.ANY_OF,
                            signal=ConditionSignal.ENTITY_PRESENT,
                            clauses=(_leaf(),))


def test_nested_condition_tree_is_valid():
    tree = ActivationCondition(
        operator=ConditionOperator.ALL_OF,
        clauses=(_leaf(ConditionSignal.INTENT_IN, "summary"),
                 ActivationCondition(operator=ConditionOperator.ANY_OF,
                                     clauses=(_leaf(ConditionSignal.ENTITY_PRESENT),
                                              _leaf(ConditionSignal.FOCUS_REQUIRED)))))
    assert len(tree.clauses) == 2
    assert tree.clauses[1].operator is ConditionOperator.ANY_OF


# ── AgentRecord ──────────────────────────────────────────────────────────


def test_valid_read_agent_constructs():
    agent = _read_agent()
    assert agent.id == "uc01_summary"
    assert agent.abac_tags.tier is ExecutionTier.READ


def test_description_over_cap_is_rejected():
    with pytest.raises(ValidationError):
        _read_agent(description="x" * (MAX_DESCRIPTION_CHARS + 1))


def test_id_must_match_pattern():
    with pytest.raises(ValidationError):
        _read_agent(id="UC-Bad-ID")


def test_agent_cannot_depend_on_itself():
    with pytest.raises(ValidationError, match="cannot depend on itself"):
        _read_agent(depends_on=("uc01_summary",))


def test_agent_cannot_exclude_itself():
    with pytest.raises(ValidationError, match="cannot exclude itself"):
        _read_agent(excludes=(ExclusionRef(agent_id="uc01_summary", priority=1),))


def test_action_agent_without_before_hook_is_rejected():
    with pytest.raises(ValidationError, match="must declare a before_invocation hook"):
        _read_agent(abac_tags=AbacTags(tier=ExecutionTier.ACTION),
                    determinism_level=DeterminismLevel.HIGH)


def test_action_agent_with_before_hook_is_accepted():
    agent = _read_agent(
        id="uc_close_ticket",
        abac_tags=AbacTags(tier=ExecutionTier.ACTION),
        determinism_level=DeterminismLevel.HIGH,
        hooks=Hooks(before_invocation=("hook_authz_recheck",)))
    assert agent.abac_tags.tier is ExecutionTier.ACTION


def test_journey_shape_requires_journey_spec():
    with pytest.raises(ValidationError, match="requires a `journey` spec"):
        _read_agent(routing_shape=RoutingShape.JOURNEY)


def test_journey_agent_cannot_be_low_determinism():
    journey = JourneySpec(slots=(JourneySlot(slot_id="issue", prompt="What is the issue?"),))
    with pytest.raises(ValidationError, match="cannot be determinism_level=low"):
        _read_agent(routing_shape=RoutingShape.JOURNEY, journey=journey,
                    determinism_level=DeterminismLevel.LOW)


def test_valid_journey_agent_constructs():
    journey = JourneySpec(slots=(
        JourneySlot(slot_id="issue", prompt="What is the issue?"),
        JourneySlot(slot_id="severity", prompt="How severe is it?", required=False)))
    agent = _read_agent(id="uc06_create_ticket", routing_shape=RoutingShape.JOURNEY,
                        journey=journey, determinism_level=DeterminismLevel.MEDIUM)
    assert agent.journey is not None
    assert len(agent.journey.slots) == 2


def test_journey_rejects_duplicate_slot_ids():
    with pytest.raises(ValidationError, match="slot_ids must be unique"):
        JourneySpec(slots=(JourneySlot(slot_id="issue", prompt="a"),
                           JourneySlot(slot_id="issue", prompt="b")))


def test_journey_requires_at_least_one_slot():
    with pytest.raises(ValidationError, match="at least one slot"):
        JourneySpec(slots=())


def test_compound_action_cannot_be_single_shape():
    with pytest.raises(ValidationError, match="compound action cannot have routing_shape=single"):
        _read_agent(compound_of=("uc01_summary_inner",), routing_shape=RoutingShape.SINGLE)


def test_duplicate_exclusion_priority_is_a_cross_field_error_at_service_layer():
    # Two distinct excluded agents with the SAME priority is allowed at the
    # record layer (both agent_ids differ) — it is caught by the service-layer
    # integrity check, tested in test_service.py. Here we prove the record
    # itself accepts distinct agent_ids.
    agent = _read_agent(excludes=(
        ExclusionRef(agent_id="uc_a", priority=5),
        ExclusionRef(agent_id="uc_b", priority=9)))
    assert len(agent.excludes) == 2


# ── ToolRecord ───────────────────────────────────────────────────────────


def _tool(**over):
    base = dict(
        id="get_ticket", version=1, owner="team-itsm",
        description="Fetch a ticket.", activation_condition=_leaf(),
        handler_ref="oneops.tools:get_ticket", execution_type=ExecutionTier.READ,
    )
    base.update(over)
    return ToolRecord(**base)


def test_valid_tool_constructs():
    assert _tool().execution_type is ExecutionTier.READ


def test_non_idempotent_action_tool_is_rejected():
    with pytest.raises(ValidationError, match="must be idempotent"):
        _tool(id="close_ticket", execution_type=ExecutionTier.ACTION, idempotent=False)


def test_idempotent_action_tool_is_accepted():
    tool = _tool(id="close_ticket", execution_type=ExecutionTier.ACTION, idempotent=True)
    assert tool.idempotent is True


def test_tool_timeout_bounds_are_enforced():
    with pytest.raises(ValidationError):
        _tool(timeout_ms=50)            # below the 100ms floor
    with pytest.raises(ValidationError):
        _tool(timeout_ms=999_999)       # above the 600s ceiling


def test_tool_parameter_carries_classification():
    tool = _tool(parameters=(ToolParameter(
        name="ssn", type="str", required=True, description="caller SSN",
        data_classification="pii"),))
    assert tool.parameters[0].data_classification.value == "pii"


# ── SchemaRecord ─────────────────────────────────────────────────────────


def test_schema_record_constructs():
    rec = SchemaRecord(id="uc_envelope", version=1, owner="team-platform",
                       description="Request envelope.", format="protobuf",
                       location="proto/oneops/v1/uc.proto")
    assert rec.format == "protobuf"


def test_schema_record_rejects_unknown_format():
    with pytest.raises(ValidationError):
        SchemaRecord(id="uc_envelope", version=1, owner="x",
                     description="d", format="xml", location="p")


def test_schema_deprecates_must_be_older():
    with pytest.raises(ValidationError, match="must be older"):
        SchemaRecord(id="uc_envelope", version=2, owner="x", description="d",
                     format="json", location="p", deprecates_version=2)
