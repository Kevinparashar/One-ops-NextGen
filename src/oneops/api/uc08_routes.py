"""UC-8 Catalog Fulfillment — REST routes (button + chat-callable).

Endpoints (matches UC-2 / UC-5 conventions):

  POST /api/uc08/match
       Semantic search → top-K catalog candidates. Returns suggestion +
       optional auto-pick. Read-only — never invokes fulfillment.

  POST /api/uc08/fulfill
       Execute fulfillment for an EXPLICIT catalog_item_id. Caller MUST
       have confirmed the suggestion via /match first. Approval gate
       discipline preserved.

  GET  /api/uc08/status/{ritm_id}
       Read-only status of an in-flight or completed RITM.

Headers (dev mode; prod wires JWT upstream):
  x-tenant-id, x-user-id, x-role

Production wiring:
  • LiteLLM gateway for embedding + rerank LLM
  • Dragonfly cache for /match replays
  • OTel spans on every endpoint
  • Prometheus metrics: ai.uc08.{endpoint}.total + ai.agent.runs.total
  • Tenant-bound errors — no cross-tenant leakage
"""
from __future__ import annotations

import os
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from oneops.observability import get_logger
from oneops.observability.metrics import increment as _metric_inc

_log = get_logger("oneops.api.uc08")

router = APIRouter(prefix="/api/uc08", tags=["uc08-fulfillment"])

# RBAC: catalog-match is broad (any authenticated user can browse).
# Fulfillment is tighter — only roles that can request provisioning.
_PERMITTED_MATCH_ROLES: frozenset[str] = frozenset({
    "technician_l1", "technician_l2", "triage_desk", "admin",
    "service_desk_agent", "manager", "end_user", "requester",
})
_PERMITTED_FULFILL_ROLES: frozenset[str] = frozenset({
    "technician_l1", "technician_l2", "triage_desk", "admin",
    "service_desk_agent", "manager", "requester",
    # end_user CANNOT fulfill directly — must confirm via approval gate
})


# ── DI wiring set at lifespan startup ──────────────────────────────────


_gateway = None  # set via set_gateway() at boot
_cache = None    # set via set_cache() at boot


def set_gateway(g: Any) -> None:
    global _gateway
    _gateway = g


def set_cache(c: Any) -> None:
    global _cache
    _cache = c


# ── Request / response models ──────────────────────────────────────────


class MatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sr_title: str = Field(min_length=1, max_length=4000)
    sr_description: str = Field(default="", max_length=8000)
    sr_category: str | None = Field(default=None, max_length=64)
    top_k: int = Field(default=5, ge=1, le=20)


class MatchCandidate(BaseModel):
    catalog_item_id: str
    name: str
    description: str
    category: str
    owner_group: str
    cosine_score: float
    above_floor: bool
    is_auto_pick: bool


class MatchResponse(BaseModel):
    candidates: list[MatchCandidate]
    auto_pick: MatchCandidate | None
    verdict: str  # "AUTO_PICK" | "RERANK_CHOSEN" | "NO_MATCH" | "WRONG_INTENT"
    rerank_used: bool
    rerank_confidence: float
    rerank_reasoning: str
    query_text: str


class FulfillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=64)
    catalog_item_id: str = Field(min_length=1, max_length=64)
    variables: dict[str, Any] = Field(default_factory=dict)
    requested_for: str = Field(default="", max_length=64)
    quantity: int = Field(default=1, ge=1, le=99)
    idempotency_key: str | None = Field(default=None, max_length=64)


class FulfillResponse(BaseModel):
    ritm_id: str
    run_id: str
    outcome: str
    tasks_total: int
    display_text: str
    trace_id: str | None = None


# ── Helpers ────────────────────────────────────────────────────────────


def _principal(request: Request) -> tuple[str, str, str]:
    h = request.headers
    tenant = (h.get("x-tenant-id") or "").strip()
    user = (h.get("x-user-id") or "").strip()
    role = (h.get("x-role") or "").strip()
    if not tenant:
        raise HTTPException(401, detail="missing x-tenant-id header")
    if not user:
        raise HTTPException(401, detail="missing x-user-id header")
    if not role:
        raise HTTPException(401, detail="missing x-role header")
    return tenant, user, role


def _require_role(role: str, allowed: frozenset[str], endpoint: str) -> None:
    if role not in allowed:
        raise HTTPException(
            403,
            detail=(f"role {role!r} cannot call /api/uc08/{endpoint} "
                    f"(allowed: {sorted(allowed)})"),
        )


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(os.environ["POSTGRES_URL"])


# ── POST /api/uc08/match ───────────────────────────────────────────────


@router.post("/match", response_model=MatchResponse)
async def match_catalog(
    payload: MatchRequest,
    request: Request,
) -> MatchResponse:
    """Semantic catalog match — read-only, returns a suggestion.

    Two-stage retrieval:
      1. Embedding cosine search (fast, ~50ms)
      2. LLM reranker for soft-zone queries (~200ms, cached)

    Returns the suggestion the chat / UI should show the user. Approval
    gate is the CALLER's responsibility (UI button or chat-turn confirm).
    """
    tenant_id, user_id, role = _principal(request)
    _require_role(role, _PERMITTED_MATCH_ROLES, "match")

    if _gateway is None:
        raise HTTPException(503, detail="LLM gateway not initialised")

    from oneops.use_cases.uc08_fulfillment.catalog_search import (
        CatalogSearchError, find_closest_catalog_items,
    )
    from oneops.use_cases.uc08_fulfillment.catalog_reranker import (
        rerank, should_rerank,
    )

    conn = await _connect()
    try:
        try:
            r = await find_closest_catalog_items(
                tenant_id=tenant_id,
                sr_title=payload.sr_title,
                sr_description=payload.sr_description,
                sr_category=payload.sr_category,
                gateway=_gateway, conn=conn, top_k=payload.top_k,
            )
        except CatalogSearchError as exc:
            _metric_inc("ai.uc08.match.failed.total", 1,
                        tenant_id=tenant_id, reason="search_error")
            raise HTTPException(502, detail=f"search failure: {exc}")

        candidates = [
            MatchCandidate(
                catalog_item_id=m.catalog_item_id,
                name=m.name, description=m.description,
                category=m.category, owner_group=m.owner_group,
                cosine_score=m.cosine_score,
                above_floor=m.above_floor,
                is_auto_pick=m.is_auto_pick,
            )
            for m in r.matches
        ]

        verdict = "NO_MATCH"
        auto_pick_resp = None
        rerank_used = False
        rerank_conf = 0.0
        rerank_reason = ""

        if not r.matches:
            verdict = "NO_MATCH"
        else:
            top1 = r.matches[0]
            do_rerank, _ = should_rerank(top1.cosine_score)
            if not do_rerank and top1.is_auto_pick:
                verdict = "AUTO_PICK"
                auto_pick_resp = candidates[0]
            elif do_rerank:
                rerank_used = True
                try:
                    rr = await rerank(
                        tenant_id=tenant_id, sr_text=payload.sr_title,
                        candidates=r.matches, gateway=_gateway,
                        cache=_cache, user_id=user_id,
                    )
                except CatalogSearchError as exc:
                    _metric_inc("ai.uc08.match.failed.total", 1,
                                tenant_id=tenant_id, reason="rerank_error")
                    raise HTTPException(502, detail=f"rerank failure: {exc}")
                verdict = (
                    "RERANK_CHOSEN" if rr.verdict == "CHOSEN"
                    else rr.verdict
                )
                rerank_conf = rr.confidence
                rerank_reason = rr.reasoning
                if rr.verdict == "CHOSEN" and rr.chosen_match is not None:
                    auto_pick_resp = next(
                        (c for c in candidates
                         if c.catalog_item_id == rr.chosen),
                        None,
                    )

        _metric_inc("ai.uc08.match.total", 1,
                    tenant_id=tenant_id, verdict=verdict,
                    rerank_used=str(rerank_used).lower())
        return MatchResponse(
            candidates=candidates,
            auto_pick=auto_pick_resp,
            verdict=verdict,
            rerank_used=rerank_used,
            rerank_confidence=rerank_conf,
            rerank_reasoning=rerank_reason,
            query_text=r.query_text,
        )
    finally:
        await conn.close()


# ── POST /api/uc08/fulfill ─────────────────────────────────────────────


@router.post("/fulfill", response_model=FulfillResponse)
async def fulfill(
    payload: FulfillRequest,
    request: Request,
) -> FulfillResponse:
    """Execute fulfillment for an explicitly-chosen catalog_item_id.

    The caller (UI button or chat confirm) MUST have already shown the
    user the match and gotten explicit consent — this endpoint is the
    action stage. Approval gates declared in the catalog template (via
    request_human_approval tasks) fire inside the executor.
    """
    tenant_id, user_id, role = _principal(request)
    _require_role(role, _PERMITTED_FULFILL_ROLES, "fulfill")

    from oneops.use_cases.uc08_fulfillment.contracts import (
        FulfillmentRequest, TriggerType,
    )
    from oneops.use_cases.uc08_fulfillment.core import (
        fulfill_request as core_fulfill,
    )
    from oneops.use_cases.uc08_fulfillment.errors import (
        CatalogItemNotFoundError, DuplicateRequestError,
        RequestNotFoundError,
    )

    req = FulfillmentRequest(
        tenant_id=tenant_id,
        request_id=payload.request_id,
        catalog_item_id=payload.catalog_item_id,
        variables=payload.variables,
        requested_for=(payload.requested_for or user_id),
        opened_by=user_id,
        quantity=payload.quantity,
        idempotency_key=payload.idempotency_key,
        trigger_type=TriggerType.PORTAL,
    )

    async def _cp():
        return await _connect()

    try:
        outcome = await core_fulfill(req, connection_provider=_cp)
    except RequestNotFoundError as exc:
        _metric_inc("ai.uc08.fulfill.failed.total", 1,
                    tenant_id=tenant_id, reason="request_not_found")
        raise HTTPException(404, detail=str(exc))
    except CatalogItemNotFoundError as exc:
        _metric_inc("ai.uc08.fulfill.failed.total", 1,
                    tenant_id=tenant_id, reason="catalog_not_found")
        raise HTTPException(404, detail=str(exc))
    except DuplicateRequestError as exc:
        _metric_inc("ai.uc08.fulfill.failed.total", 1,
                    tenant_id=tenant_id, reason="duplicate")
        raise HTTPException(409, detail=str(exc))

    _metric_inc("ai.uc08.fulfill.total", 1,
                tenant_id=tenant_id, outcome=outcome.outcome.value)
    return FulfillResponse(
        ritm_id=outcome.ritm_id,
        run_id=outcome.run_id,
        outcome=outcome.outcome.value,
        tasks_total=outcome.tasks_total,
        display_text=outcome.display_text,
        trace_id=outcome.trace_id,
    )


# ── GET /api/uc08/status/{ritm_id} ─────────────────────────────────────


@router.get("/status/{ritm_id}")
async def status(ritm_id: str, request: Request) -> dict[str, Any]:
    """Read-only status. Tenant-bound."""
    tenant_id, _, role = _principal(request)
    _require_role(role, _PERMITTED_MATCH_ROLES, "status")

    from oneops.use_cases.uc08_fulfillment.db import get_status
    from oneops.use_cases.uc08_fulfillment.errors import (
        RequestItemNotFoundError,
    )

    conn = await _connect()
    try:
        try:
            s = await get_status(
                tenant_id=tenant_id, ritm_id=ritm_id, conn=conn,
            )
        except RequestItemNotFoundError as exc:
            raise HTTPException(404, detail=str(exc))
        _metric_inc("ai.uc08.status.total", 1,
                    tenant_id=tenant_id, state=s.state.value)
        return s.model_dump(mode="json")
    finally:
        await conn.close()
