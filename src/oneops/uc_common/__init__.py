"""UC-common — shared contracts that every use case obeys.

This is the substrate that lets the platform scale to many UCs without per-UC
shape divergence. Anything that crosses a UC boundary (the response a UC
returns, the verification rules that gate its narrative output) lives here,
not in a UC folder.

Each UC owns its OUTPUT FORMAT in code, bound to that use case — uc01 via
`use_cases/_shared/field_labels.humanise_record`, uc02 via
`uc02_similar_tickets/render`, uc03 via its answer composer. There is no
separate per-service display-spec data file; the rendering rule is code.

Design influences:
  * Moveworks — structured output at every tool boundary; one shape for the
    consumer (UI, aggregator, LLM), N adapters for the services.
  * Parlant — canned response at compliance/safety touchpoint; the
    deterministic fallback summary IS the canned response for UC-1.
  * Salesforce Agentforce — Trust Layer grounding: every claim in a
    narrative must anchor to a typed key_detail or record_context field.

Public surface:
    from oneops.uc_common import EntitySummary, KeyDetail, Citation
"""
from __future__ import annotations

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
from oneops.uc_common.time_filter import Boundary, TimeFilter

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
    # time_filter
    "TimeFilter",
    "Boundary",
]
