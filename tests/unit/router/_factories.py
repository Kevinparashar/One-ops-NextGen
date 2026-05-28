"""Shared builders for router tests — registry agents + a populated registry.

Underscore-prefixed: a test-support module, not a test file.
"""
from __future__ import annotations

from oneops.registry.models import (
    AbacTags,
    ActivationCondition,
    AgentRecord,
    ConditionOperator,
    ConditionSignal,
    DataClassification,
    DeterminismLevel,
    ExclusionRef,
    ExecutionTier,
    RoutingShape,
)
from oneops.registry.service import RegistryService
from oneops.registry.store import FileBackend


def intent_cond(*intents: str) -> ActivationCondition:
    return ActivationCondition(
        operator=ConditionOperator.LEAF, signal=ConditionSignal.INTENT_IN,
        values=tuple(intents))


def role_cond(*roles: str) -> ActivationCondition:
    return ActivationCondition(
        operator=ConditionOperator.LEAF, signal=ConditionSignal.ROLE_IN,
        values=tuple(roles))


def make_agent(
    agent_id: str,
    *,
    description: str = "An agent that does work.",
    intent_family: str = "testing",
    condition: ActivationCondition | None = None,
    tier: ExecutionTier = ExecutionTier.READ,
    audience: tuple[str, ...] = (),
    data_class: DataClassification = DataClassification.INTERNAL,
    depends_on: tuple[str, ...] = (),
    excludes: tuple[ExclusionRef, ...] = (),
    before_hooks: tuple[str, ...] = (),
) -> AgentRecord:
    from oneops.registry.models import Hooks

    hooks = Hooks(before_invocation=before_hooks) if before_hooks else Hooks()
    return AgentRecord(
        id=agent_id, version=1, owner="team-test", description=description,
        intent_family=intent_family, routing_shape=RoutingShape.SINGLE,
        activation_condition=condition or intent_cond("summary"),
        abac_tags=AbacTags(tier=tier, audience=audience,
                           data_classification=data_class),
        determinism_level=DeterminismLevel.LOW,
        depends_on=depends_on, excludes=excludes,
        hooks=hooks)


def make_registry(tmp_path, agents: list[AgentRecord]) -> RegistryService:
    """A file-backed registry with `agents` created and activated."""
    svc = RegistryService(FileBackend(tmp_path))
    for agent in agents:
        svc.agents.create(agent)
        svc.agents.activate(agent.id, 1)
    return svc
