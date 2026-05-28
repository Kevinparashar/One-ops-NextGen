"""Registry layer — the declarative specification store (P1).

The registry holds agents, tools, and message schemas as *data*. Every other
component (router, executor, AuthZ, policy) reads the registry; nothing
hardcodes an agent. Adding use-case #1001 is a new record, not a code change.

Public surface:
    from oneops.registry import load_registry, RegistryService
    from oneops.registry import AgentRecord, ToolRecord, SchemaRecord
"""
from __future__ import annotations

from oneops.registry.loader import DEFAULT_REGISTRY_ROOT, load_registry
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
    Hooks,
    JourneySlot,
    JourneySpec,
    RecordStatus,
    RoutingShape,
    SchemaRecord,
    ToolParameter,
    ToolRecord,
    ToolRef,
)
from oneops.registry.service import RegistryService
from oneops.registry.store import FileBackend, RegistryBackend, VersionedStore

__all__ = [
    "load_registry",
    "DEFAULT_REGISTRY_ROOT",
    "RegistryService",
    "RegistryBackend",
    "FileBackend",
    "VersionedStore",
    "AgentRecord",
    "ToolRecord",
    "SchemaRecord",
    "ActivationCondition",
    "ConditionOperator",
    "ConditionSignal",
    "AbacTags",
    "Hooks",
    "ExclusionRef",
    "ToolRef",
    "ToolParameter",
    "JourneySlot",
    "JourneySpec",
    "DeterminismLevel",
    "RoutingShape",
    "RecordStatus",
    "ExecutionTier",
    "DataClassification",
]
