"""Cross-record integrity tests for RegistryService.

Proves the integrity check catches every cross-record violation it claims to,
and that a consistent registry passes. Uses the real FileBackend on tmp dirs.
"""
from __future__ import annotations

import pytest

from oneops.errors import RegistryIntegrityError
from oneops.registry.models import (
    AbacTags,
    ActivationCondition,
    AgentRecord,
    ConditionOperator,
    ConditionSignal,
    DeterminismLevel,
    ExclusionRef,
    ExecutionTier,
    RoutingShape,
    ToolRecord,
    ToolRef,
)
from oneops.registry.service import RegistryService
from oneops.registry.store import FileBackend


def _cond():
    return ActivationCondition(operator=ConditionOperator.LEAF,
                               signal=ConditionSignal.INTENT_IN, values=("summary",))


def _agent(agent_id, *, tool_refs=(), depends_on=(), excludes=(), compound_of=(),
           shape=RoutingShape.SINGLE):
    return AgentRecord(
        id=agent_id, version=1, owner="team-itsm", description="An agent.",
        intent_family="entity_summary", routing_shape=shape,
        activation_condition=_cond(), abac_tags=AbacTags(tier=ExecutionTier.READ),
        determinism_level=DeterminismLevel.LOW, tool_refs=tuple(tool_refs),
        depends_on=tuple(depends_on), excludes=tuple(excludes),
        compound_of=tuple(compound_of))


def _tool(tool_id):
    return ToolRecord(
        id=tool_id, version=1, owner="team-itsm", description="A tool.",
        activation_condition=_cond(), handler_ref="oneops.tools:fn",
        execution_type=ExecutionTier.READ)


@pytest.fixture
def service(tmp_path):
    return RegistryService(FileBackend(tmp_path))


def _add_agent(service, agent):
    service.agents.create(agent)
    service.agents.activate(agent.id, 1)


def _add_tool(service, tool):
    service.tools.create(tool)
    service.tools.activate(tool.id, 1)


# ── clean registry ───────────────────────────────────────────────────────


def test_consistent_registry_passes_integrity(service):
    _add_tool(service, _tool("get_ticket"))
    _add_agent(service, _agent("uc01", tool_refs=(ToolRef(tool_id="get_ticket"),)))
    _add_agent(service, _agent("uc02", depends_on=("uc01",)))
    service.check_integrity()                       # must not raise


def test_empty_registry_passes_integrity(service):
    service.check_integrity()


# ── dangling references ──────────────────────────────────────────────────


def test_dangling_tool_ref_is_caught(service):
    _add_agent(service, _agent("uc01", tool_refs=(ToolRef(tool_id="ghost_tool"),)))
    with pytest.raises(RegistryIntegrityError, match="ghost_tool"):
        service.check_integrity()


def test_tool_ref_to_inactive_tool_is_caught(service):
    service.tools.create(_tool("draft_tool"))       # created but NOT activated
    _add_agent(service, _agent("uc01", tool_refs=(ToolRef(tool_id="draft_tool"),)))
    with pytest.raises(RegistryIntegrityError, match="no active version"):
        service.check_integrity()


def test_dangling_depends_on_is_caught(service):
    _add_agent(service, _agent("uc02", depends_on=("missing_agent",)))
    with pytest.raises(RegistryIntegrityError, match="missing_agent"):
        service.check_integrity()


def test_dangling_exclusion_is_caught(service):
    _add_agent(service, _agent(
        "uc01", excludes=(ExclusionRef(agent_id="ghost", priority=1),)))
    with pytest.raises(RegistryIntegrityError, match="ghost"):
        service.check_integrity()


def test_dangling_compound_member_is_caught(service):
    _add_agent(service, _agent(
        "uc_compound", compound_of=("missing_member",), shape=RoutingShape.DEPENDENT))
    with pytest.raises(RegistryIntegrityError, match="missing_member"):
        service.check_integrity()


# ── exclusion priority ───────────────────────────────────────────────────


def test_duplicate_exclusion_priority_is_caught(service):
    _add_agent(service, _agent("uc_a"))
    _add_agent(service, _agent("uc_b"))
    _add_agent(service, _agent("uc01", excludes=(
        ExclusionRef(agent_id="uc_a", priority=5),
        ExclusionRef(agent_id="uc_b", priority=5))))   # same priority — ambiguous
    with pytest.raises(RegistryIntegrityError, match="duplicate exclusion priorities"):
        service.check_integrity()


# ── dependency cycles ────────────────────────────────────────────────────


def test_direct_dependency_cycle_is_caught(service):
    # uc01 -> uc02 -> uc01
    service.agents.create(_agent("uc01", depends_on=("uc02",)))
    service.agents.activate("uc01", 1)
    service.agents.create(_agent("uc02", depends_on=("uc01",)))
    service.agents.activate("uc02", 1)
    with pytest.raises(RegistryIntegrityError, match="cycle"):
        service.check_integrity()


def test_three_node_dependency_cycle_is_caught(service):
    for a, dep in [("uc01", "uc02"), ("uc02", "uc03"), ("uc03", "uc01")]:
        service.agents.create(_agent(a, depends_on=(dep,)))
        service.agents.activate(a, 1)
    with pytest.raises(RegistryIntegrityError, match="cycle"):
        service.check_integrity()


def test_diamond_dependency_is_not_a_cycle(service):
    # uc_top -> {uc_l, uc_r} -> uc_base.  A DAG, not a cycle.
    _add_agent(service, _agent("uc_base"))
    _add_agent(service, _agent("uc_l", depends_on=("uc_base",)))
    _add_agent(service, _agent("uc_r", depends_on=("uc_base",)))
    _add_agent(service, _agent("uc_top", depends_on=("uc_l", "uc_r")))
    service.check_integrity()                       # must not raise


# ── error reports every violation ────────────────────────────────────────


def test_integrity_error_lists_all_violations(service):
    _add_agent(service, _agent("uc01", tool_refs=(ToolRef(tool_id="ghost_a"),),
                               depends_on=("ghost_b",)))
    with pytest.raises(RegistryIntegrityError) as exc:
        service.check_integrity()
    msg = str(exc.value)
    assert "ghost_a" in msg
    assert "ghost_b" in msg


# ── resolve_agent_tools ──────────────────────────────────────────────────


def test_resolve_agent_tools_returns_active_tool_records(service):
    _add_tool(service, _tool("get_ticket"))
    _add_tool(service, _tool("get_timeline"))
    _add_agent(service, _agent("uc01", tool_refs=(
        ToolRef(tool_id="get_ticket"), ToolRef(tool_id="get_timeline"))))
    tools = service.resolve_agent_tools("uc01")
    assert {t.id for t in tools} == {"get_ticket", "get_timeline"}
