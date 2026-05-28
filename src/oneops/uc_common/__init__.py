"""UC-common — shared contracts that every use case obeys.

This is the substrate that lets POC-5-MW scale to 1000 UCs without per-UC
shape divergence. Anything that crosses a UC boundary (the response a UC
returns, the per-service display spec it renders by, the verification rules
that gate its narrative output) lives here, not in a UC folder.

Design influences:
  * Moveworks — structured output at every tool boundary; one shape for the
    consumer (UI, aggregator, LLM), N adapters for the services.
  * Parlant — canned response at compliance/safety touchpoint; the
    deterministic fallback summary IS the canned response for UC-1.
  * Salesforce Agentforce — Trust Layer grounding: every claim in a
    narrative must anchor to a typed key_detail or record_context field.
  * AgentScript — display specs are data, hot-reloadable; the rendering rule
    survives a runtime swap.

Public surface:
    from oneops.uc_common import EntitySummary, KeyDetail, Citation
    from oneops.uc_common import DisplaySpec, RowSpec, load_display_spec
"""
from __future__ import annotations

from oneops.uc_common.display_spec import (
    DEFAULT_DISPLAY_SPECS_ROOT,
    DisplaySpec,
    RowSpec,
    UnknownEntityTypeError,
    load_display_spec,
)
from oneops.uc_common.summary_schema import (
    ENTITY_TYPES,
    SUMMARY_SCHEMA_CURRENT,
    SUMMARY_SCHEMA_MIN_SUPPORTED,
    ActionRef,
    Citation,
    ClaimRef,
    EntitySummary,
    EntityType,
    KeyDetail,
    KeyDetailKind,
    PartyRef,
)

__all__ = [
    # summary_schema
    "EntitySummary",
    "KeyDetail",
    "KeyDetailKind",
    "Citation",
    "ActionRef",
    "ClaimRef",
    "PartyRef",
    "EntityType",
    "ENTITY_TYPES",
    "SUMMARY_SCHEMA_CURRENT",
    "SUMMARY_SCHEMA_MIN_SUPPORTED",
    # display_spec
    "DisplaySpec",
    "RowSpec",
    "load_display_spec",
    "DEFAULT_DISPLAY_SPECS_ROOT",
    "UnknownEntityTypeError",
]
