"""UC-5 triage — standard registry-dispatched tool handlers (B-refactor, Phase 1).

These wrap UC-5's three triage tools in the platform-standard
`async (arguments: dict, context: dict) -> dict` handler contract, so they can be
declared in the registry (`registries/v2/tools/uc05_triage/*.json`) and dispatched
by the MAIN executor like every other UC — instead of UC-5's bespoke runner+graph.

This is the agents-as-data alignment (see docs/agent-skills-spec.md + the uc05
onboarding scope): orchestration becomes a registry PLAN run by the one executor
(check_duplicates → [recommend_assignment ∥ prioritize] → assemble), with the
executor's Send fan-out + data-flow binding carrying check_duplicates' `candidates`
into recommend_assignment and its category/subcategory into prioritize.

PHASE 1 (this file) is ADDITIVE: it adds the standard handlers + their per-request
adapter rebuild from injected dependencies. UC-5's existing runner/graph/routes are
untouched until Phase 3 retires them — so nothing breaks while the executor path is
built and validated against the existing golden tests.

Dependencies are module-injected at boot (mirrors `set_summarize_llm` in UC-1):
  • `set_uc05_gateway`             — the LlmGateway (embed + chat adapters)
  • `set_uc05_connection_provider` — async () -> asyncpg connection
  • `set_uc05_ticket_store`        — TicketStore for the tenant-scoped row read
Each handler is tenant-scoped from `context` and returns a JSON-serialisable dict
(`.model_dump()`), so the executor can bind outputs between steps. No silent
failures: a missing dependency or row is an explicit typed outcome.
"""
from __future__ import annotations

from typing import Any

from oneops.observability import get_logger
from oneops.use_cases.uc05_triage.adapters import (
    make_embed_fn,
    make_infer_fn,
    make_propose_fn,
    make_tag_fn,
    make_tiebreak_fn,
)
from oneops.use_cases.uc05_triage.contracts import ScoredNeighbour
from oneops.use_cases.uc05_triage.tools.check_duplicates import (
    check_duplicate_candidates,
)
from oneops.use_cases.uc05_triage.tools.prioritize import prioritize_entity
from oneops.use_cases.uc05_triage.tools.recommend_assignment import (
    recommend_assignment,
)

_log = get_logger("oneops.use_cases.uc05_triage.handlers")

# ── module-injected dependencies (wired at app boot, like set_summarize_llm) ──
_gateway: Any | None = None
_connection_provider: Any | None = None
_ticket_store: Any | None = None


def set_uc05_gateway(gateway: Any | None) -> None:
    global _gateway
    _gateway = gateway


def set_uc05_connection_provider(provider: Any | None) -> None:
    global _connection_provider
    _connection_provider = provider


def set_uc05_ticket_store(store: Any | None) -> None:
    global _ticket_store
    _ticket_store = store


def _deps_ready() -> bool:
    return _gateway is not None and _connection_provider is not None \
        and _ticket_store is not None


async def _load_row(service_id: str, ticket_id: str, tenant_id: str) -> dict | None:
    """Tenant-scoped row read via the injected store. None if missing (no leak)."""
    try:
        row = await _ticket_store.get_ticket(
            service_id=service_id, ticket_id=ticket_id, tenant_id=tenant_id)
    except KeyError:
        return None
    return dict(row) if row is not None else None


def _err(code: str, message: str) -> dict[str, Any]:
    return {"outcome": code, "message": message}


# ── Tool 1: check duplicate candidates ───────────────────────────────────────


async def check_duplicates(arguments: dict[str, Any],
                           context: dict[str, Any]) -> dict[str, Any]:
    """Standard handler for UC-5 Tool 1. Outputs a DuplicateCheckResult dict —
    including `candidates`, which the executor binds into recommend_assignment."""
    if not _deps_ready():
        return _err("dependency_unavailable", "uc05 dependencies not wired")
    tenant_id = str(context.get("tenant_id") or "")
    user_id = str(context.get("user_id") or "")
    service_id = str(arguments.get("service_id") or "")
    ticket_id = str(arguments.get("ticket_id") or "")
    if not (tenant_id and service_id and ticket_id):
        return _err("invalid_request", "tenant_id, service_id, ticket_id required")

    row = await _load_row(service_id, ticket_id, tenant_id)
    if row is None:
        return _err("not_found", f"{service_id}/{ticket_id} not found in tenant")

    conn = await _connection_provider()
    result = await check_duplicate_candidates(
        service_id=service_id, tenant_id=tenant_id, ticket_row=row,
        embed_fn=make_embed_fn(_gateway, tenant_id=tenant_id, user_id=user_id),
        conn=conn,
        tiebreak_fn=make_tiebreak_fn(_gateway, tenant_id=tenant_id, user_id=user_id),
        tag_fn=make_tag_fn(_gateway, tenant_id=tenant_id, user_id=user_id),
        propose_fn=make_propose_fn(_gateway, tenant_id=tenant_id, user_id=user_id),
    )
    return result.model_dump()


# ── Tool 2: recommend assignment (consumes Tool 1's candidates via binding) ──


async def recommend_assignment_handler(arguments: dict[str, Any],
                                       context: dict[str, Any]) -> dict[str, Any]:
    """Standard handler for UC-5 Tool 2. `candidates` is bound from Tool 1's
    output by the executor (data-flow binding); `probe_text`/`ticket_row`
    optional (only used on the LLM tiebreak path)."""
    if not _deps_ready():
        return _err("dependency_unavailable", "uc05 dependencies not wired")
    tenant_id = str(context.get("tenant_id") or "")
    user_id = str(context.get("user_id") or "")
    raw_candidates = arguments.get("candidates") or []
    candidates = [ScoredNeighbour.model_validate(c) if isinstance(c, dict) else c
                  for c in raw_candidates]
    result = await recommend_assignment(
        candidates=candidates,
        probe_text=str(arguments.get("probe_text") or ""),
        ticket_row=arguments.get("ticket_row"),
        tiebreak_fn=make_tiebreak_fn(_gateway, tenant_id=tenant_id, user_id=user_id),
    )
    return result.model_dump()


# ── Tool 3: prioritize ────────────────────────────────────────────────────────


async def prioritize(arguments: dict[str, Any],
                    context: dict[str, Any]) -> dict[str, Any]:
    """Standard handler for UC-5 Tool 3. category/subcategory are bound from
    Tool 1's output by the executor; loads the row for impact/urgency signals."""
    if not _deps_ready():
        return _err("dependency_unavailable", "uc05 dependencies not wired")
    tenant_id = str(context.get("tenant_id") or "")
    user_id = str(context.get("user_id") or "")
    service_id = str(arguments.get("service_id") or "")
    ticket_id = str(arguments.get("ticket_id") or "")
    if not (tenant_id and service_id and ticket_id):
        return _err("invalid_request", "tenant_id, service_id, ticket_id required")

    row = await _load_row(service_id, ticket_id, tenant_id)
    if row is None:
        return _err("not_found", f"{service_id}/{ticket_id} not found in tenant")

    result = await prioritize_entity(
        service_id=service_id, ticket_row=row,
        suggested_category=arguments.get("suggested_category"),
        suggested_subcategory=arguments.get("suggested_subcategory"),
        infer_fn=make_infer_fn(_gateway, tenant_id=tenant_id, user_id=user_id),
    )
    return result.model_dump()


__all__ = [
    "set_uc05_gateway",
    "set_uc05_connection_provider",
    "set_uc05_ticket_store",
    "check_duplicates",
    "recommend_assignment_handler",
    "prioritize",
]
