"""Tests for the registry-record → ResourceDescriptor bridge."""
from __future__ import annotations

from oneops.authz.descriptors import from_agent_record, from_tool_record
from oneops.authz.models import DataClass, Tier
from oneops.registry.models import (
    AbacTags,
    ActivationCondition,
    AgentRecord,
    ConditionOperator,
    ConditionSignal,
    DataClassification,
    DeterminismLevel,
    ExecutionTier,
    RoutingShape,
    ToolRecord,
)


def _cond():
    return ActivationCondition(operator=ConditionOperator.LEAF,
                               signal=ConditionSignal.INTENT_IN, values=("summary",))


def test_from_agent_record_maps_abac_tags():
    agent = AgentRecord(
        id="uc01_summarization", version=1, owner="team-itsm",
        description="Summarise a record.", intent_family="entity_summary",
        routing_shape=RoutingShape.SINGLE, activation_condition=_cond(),
        determinism_level=DeterminismLevel.LOW,
        abac_tags=AbacTags(
            service=("incident",), tier=ExecutionTier.READ,
            audience=("viewer", "service_desk_agent"),
            data_classification=DataClassification.CONFIDENTIAL))

    desc = from_agent_record(agent, resource_tenant_id="tenant-a")
    assert desc.resource_id == "uc01_summarization"
    assert desc.resource_tenant_id == "tenant-a"
    assert desc.tier is Tier.READ
    assert desc.data_classification is DataClass.CONFIDENTIAL
    assert desc.audience == ("viewer", "service_desk_agent")
    assert desc.required_scopes == ()                # scopes are per-tool


def test_from_tool_record_maps_tier_and_scopes():
    tool = ToolRecord(
        id="close_ticket", version=1, owner="team-itsm",
        description="Close a ticket.", activation_condition=_cond(),
        handler_ref="oneops.tools:close", execution_type=ExecutionTier.ACTION,
        idempotent=True, requires_scopes=("write:ticket",))

    desc = from_tool_record(tool, resource_tenant_id="tenant-b")
    assert desc.resource_id == "close_ticket"
    assert desc.resource_tenant_id == "tenant-b"
    assert desc.tier is Tier.ACTION
    assert desc.required_scopes == ("write:ticket",)
    assert desc.audience == ()                       # audience gating is the agent's job
