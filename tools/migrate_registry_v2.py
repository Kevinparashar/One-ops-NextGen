"""Migrate the three built use cases into the v2 declarative registry.

P1 migration (MIGRATION.md). Reads nothing destructively — the legacy
`registries/*.json` files are left untouched. This script *authors* the new
`registries/v2/` declarative records for the three UCs that exist in code
today (UC-1 summarization, UC-3 KB lookup, UC-99 conversational) plus their
tools and the request-envelope schema.

The declarative fields the legacy registry never had — `activation_condition`,
`determinism_level`, `hooks`, `routing_shape`, `abac_tags.tier` — are curated
here per agent. Descriptions are deliberately rewritten *tight* (<=600 chars,
MAX_DESCRIPTION_CHARS) — the legacy descriptions ran ~900 chars, which the
attention-budget discipline (Moveworks) does not allow.

Run:  .venv/bin/python tools/migrate_registry_v2.py [--force]

`--force` wipes `registries/v2/` and rebuilds. Without it, the script refuses
to run if records already exist (no silent overwrite).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from oneops.registry import (
    AbacTags,
    ActivationCondition,
    AgentRecord,
    ConditionOperator,
    ConditionSignal,
    DataClassification,
    DeterminismLevel,
    ExecutionTier,
    RegistryService,
    RoutingShape,
    SchemaRecord,
    ToolParameter,
    ToolRecord,
    ToolRef,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
V2_ROOT = REPO_ROOT / "registries" / "v2"


def _leaf(signal: ConditionSignal, *values: str) -> ActivationCondition:
    return ActivationCondition(
        operator=ConditionOperator.LEAF, signal=signal, values=tuple(values)
    )


# ── Tool records (10 — the tools the three built agents reference) ───────

_TICKET_PARAMS = (
    ToolParameter(name="ticket_id", type="str", required=True,
                  description="Canonical work-record id (e.g. INC0048213)."),
    ToolParameter(name="tenant_id", type="str", required=True,
                  description="Tenant scope — supplied from the request envelope, never user text."),
    ToolParameter(name="service_id", type="str", required=True,
                  description="Service module: incident | request | problem | change."),
)

_TICKET_COND = _leaf(ConditionSignal.INTENT_IN, "summary", "field_read")
_KB_COND = _leaf(ConditionSignal.INTENT_IN, "kb_search", "kb_article_fetch", "kb_ticket_search")


def _tool_records() -> list[ToolRecord]:
    common = dict(owner="team-itsm", version=1)
    return [
        ToolRecord(
            id="get_ticket_details", description=(
                "Fetch the core field snapshot of a work record by id — title, "
                "description, status, priority, assignment group/assignee, "
                "categorisation, key timestamps. Read-only toward ITSM."),
            activation_condition=_TICKET_COND,
            handler_ref="oneops.use_cases.uc01_summarization.tools:get_ticket_details",
            execution_type=ExecutionTier.READ, parameters=_TICKET_PARAMS,
            timeout_ms=15_000, idempotent=True, requires_scopes=("read:ticket",), **common),
        ToolRecord(
            id="get_ticket_timeline", description=(
                "Fetch the chronological timeline of a work record — work notes, "
                "customer comments, and state transitions. Read-only."),
            activation_condition=_TICKET_COND,
            handler_ref="oneops.use_cases.uc01_summarization.tools:get_ticket_timeline",
            execution_type=ExecutionTier.READ, parameters=_TICKET_PARAMS,
            timeout_ms=15_000, idempotent=True, requires_scopes=("read:ticket",), **common),
        ToolRecord(
            id="get_ticket_links", description=(
                "Fetch records linked to a work record — related incidents, parent "
                "problem, linked changes, affected configuration items. Read-only."),
            activation_condition=_TICKET_COND,
            handler_ref="oneops.use_cases.uc01_summarization.tools:get_ticket_links",
            execution_type=ExecutionTier.READ, parameters=_TICKET_PARAMS,
            timeout_ms=15_000, idempotent=True, requires_scopes=("read:ticket",), **common),
        ToolRecord(
            id="get_ticket_attachment_metadata", description=(
                "Fetch attachment metadata for a work record — names and ids only, "
                "never binary content. Read-only."),
            activation_condition=_TICKET_COND,
            handler_ref="oneops.use_cases.uc01_summarization.tools:get_ticket_attachment_metadata",
            execution_type=ExecutionTier.READ, parameters=_TICKET_PARAMS,
            timeout_ms=15_000, idempotent=True, requires_scopes=("read:ticket",), **common),
        ToolRecord(
            id="summarize_entity", description=(
                "Synthesise a structured natural-language summary of a work record "
                "from its details, timeline, and links via the LLM gateway."),
            activation_condition=_leaf(ConditionSignal.INTENT_IN, "summary"),
            handler_ref="oneops.use_cases.uc01_summarization.tools:summarize_entity",
            execution_type=ExecutionTier.READ, parameters=_TICKET_PARAMS,
            timeout_ms=60_000, idempotent=True, requires_scopes=("read:ticket",), **common),
        ToolRecord(
            id="get_cached_summary", description=(
                "Read a previously generated summary from the AI summary cache, "
                "keyed by a content fingerprint. Returns null on miss or staleness."),
            activation_condition=_leaf(ConditionSignal.INTENT_IN, "summary"),
            handler_ref="oneops.use_cases.uc01_summarization.cache:get_cached_summary",
            execution_type=ExecutionTier.READ,
            parameters=(ToolParameter(name="fingerprint", type="str", required=True,
                        description="Content fingerprint identifying the cached summary."),),
            timeout_ms=5_000, idempotent=True, requires_scopes=("read:ticket",), **common),
        ToolRecord(
            id="put_cached_summary", description=(
                "Write a generated summary to the AI summary cache under its content "
                "fingerprint. Idempotent — re-writing the same fingerprint is safe."),
            activation_condition=_leaf(ConditionSignal.INTENT_IN, "summary"),
            handler_ref="oneops.use_cases.uc01_summarization.cache:put_cached_summary",
            execution_type=ExecutionTier.READ,
            parameters=(ToolParameter(name="fingerprint", type="str", required=True,
                        description="Content fingerprint to store the summary under."),),
            timeout_ms=5_000, idempotent=True, requires_scopes=("read:ticket",), **common),
        ToolRecord(
            id="search_kb", description=(
                "Hybrid keyword + semantic search of the published knowledge base. "
                "Returns ranked articles with relevance-scored excerpts. Read-only."),
            activation_condition=_KB_COND,
            handler_ref="oneops.use_cases.uc03_kb_lookup.kb_tools:search_kb",
            execution_type=ExecutionTier.READ,
            parameters=(ToolParameter(name="query", type="str", required=True,
                        description="Natural-language search query."),
                        ToolParameter(name="tenant_id", type="str", required=True,
                        description="Tenant scope from the request envelope.")),
            timeout_ms=20_000, idempotent=True, requires_scopes=("read:kb",), **common),
        ToolRecord(
            id="search_kb_by_ticket", description=(
                "Find knowledge articles relevant to a work record, using its title, "
                "description, and category as the query. Read-only."),
            activation_condition=_KB_COND,
            handler_ref="oneops.use_cases.uc03_kb_lookup.kb_tools:search_kb_by_ticket",
            execution_type=ExecutionTier.READ, parameters=_TICKET_PARAMS,
            timeout_ms=20_000, idempotent=True, requires_scopes=("read:kb",), **common),
        ToolRecord(
            id="get_kb_article", description=(
                "Fetch a single knowledge article by id — body, metadata, rating, "
                "tags. Published articles only. Read-only."),
            activation_condition=_KB_COND,
            handler_ref="oneops.use_cases.uc03_kb_lookup.kb_tools:get_kb_article",
            execution_type=ExecutionTier.READ,
            parameters=(ToolParameter(name="article_id", type="str", required=True,
                        description="Knowledge article id (e.g. KB0012345)."),
                        ToolParameter(name="tenant_id", type="str", required=True,
                        description="Tenant scope from the request envelope.")),
            timeout_ms=15_000, idempotent=True, requires_scopes=("read:kb",), **common),
    ]


# ── Agent records (the 3 built UCs) ──────────────────────────────────────


def _agent_records() -> list[AgentRecord]:
    return [
        AgentRecord(
            id="uc01_summarization", version=1, owner="team-itsm",
            description=(
                "Produces a structured natural-language summary of an ITSM work "
                "record — incident, request, problem, change — or an asset / CMDB "
                "CI. Also answers single-field reads (priority, status, owner, SLA) "
                "on a record already in focus. Read-only toward ITSM."),
            intent_family="entity_summary",
            routing_shape=RoutingShape.SINGLE,
            activation_condition=_leaf(ConditionSignal.INTENT_IN, "summary", "field_read"),
            tool_refs=tuple(ToolRef(tool_id=t) for t in (
                "get_cached_summary", "get_ticket_details", "get_ticket_timeline",
                "get_ticket_links", "get_ticket_attachment_metadata",
                "summarize_entity", "put_cached_summary")),
            policy_refs=("policy_pii_redact", "policy_tenant_scope"),
            abac_tags=AbacTags(
                service=("incident", "request", "problem", "change", "asset", "cmdb_ci"),
                tier=ExecutionTier.READ,
                audience=("viewer", "employee", "service_desk_agent", "manager"),
                data_classification=DataClassification.CONFIDENTIAL),
            determinism_level=DeterminismLevel.LOW),
        AgentRecord(
            id="uc03_kb_lookup", version=1, owner="team-itsm",
            description=(
                "Searches the published knowledge base for articles relevant to a "
                "user question or a work record, returning ranked results with "
                "relevance-scored excerpts. Also fetches a single article by id. "
                "Read-only; published, audience-matched articles only."),
            intent_family="knowledge_base",
            routing_shape=RoutingShape.SINGLE,
            activation_condition=_leaf(
                ConditionSignal.INTENT_IN, "kb_search", "kb_article_fetch", "kb_ticket_search"),
            tool_refs=tuple(ToolRef(tool_id=t) for t in (
                "search_kb", "search_kb_by_ticket", "get_kb_article")),
            policy_refs=("policy_kb_audience", "policy_tenant_scope"),
            abac_tags=AbacTags(
                service=("knowledge",),
                tier=ExecutionTier.READ,
                audience=("viewer", "employee", "service_desk_agent", "manager"),
                data_classification=DataClassification.INTERNAL),
            determinism_level=DeterminismLevel.LOW),
    ]
    # NOTE: there is no conversational / boundary "agent". Out-of-scope and
    # policy-boundary responses are produced by a platform component in the
    # routing layer (P5/P6), not by a catalogued agent — the registry holds
    # use-case agents only.


def _schema_records() -> list[SchemaRecord]:
    return [
        SchemaRecord(
            id="uc_request_envelope", version=1, owner="team-platform",
            description=(
                "The protobuf Envelope exchanged on every NATS boundary and "
                "written for every conversation event (ADR-0001). Carries "
                "schema_version, tenant_id, trace_context, idempotency_key, and "
                "a typed payload (UCRequest / UCResponse / ConversationEvent)."),
            format="protobuf",
            location="proto/oneops/v1/envelope.proto",
            deprecation_window_days=90),
    ]


def main() -> int:
    force = "--force" in sys.argv[1:]
    if V2_ROOT.exists() and any(V2_ROOT.rglob("*.json")):
        if not force:
            print(f"refusing to overwrite existing records in {V2_ROOT} — "
                  "pass --force to wipe and rebuild")
            return 1
        shutil.rmtree(V2_ROOT)
    V2_ROOT.mkdir(parents=True, exist_ok=True)

    service = RegistryService.from_path(str(V2_ROOT))

    tools = _tool_records()
    agents = _agent_records()
    schemas = _schema_records()

    for tool in tools:
        service.tools.create(tool)
        service.tools.activate(tool.id, 1)
    for agent in agents:
        service.agents.create(agent)
        service.agents.activate(agent.id, 1)
    for schema in schemas:
        service.schemas.create(schema)
        service.schemas.activate(schema.id, 1)

    # A migration that leaves the registry inconsistent is a failed migration.
    service.check_integrity()

    print(f"migrated → {V2_ROOT}")
    print(f"  agents : {len(agents)}  ({', '.join(a.id for a in agents)})")
    print(f"  tools  : {len(tools)}")
    print(f"  schemas: {len(schemas)}")
    print("  integrity: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
