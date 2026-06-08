"""UC-8 Catalog Fulfillment — REST routes (button + chat-callable).

Endpoints (matches UC-2 / UC-5 conventions):

  POST /api/uc08/create-sr
       Mint a new SR id (SR + 7 digits per tenant), LLM-extract a clean
       title and preserved description from the user's free text, and
       INSERT into itsm.request with status='new' / stage='intake'.
       Reads back the inserted row. The embedding refresh trigger on
       itsm.request fires automatically on the INSERT (no extra wiring).

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

import asyncio
import os
from datetime import UTC
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from oneops.observability import get_logger
from oneops.observability.metrics import increment as _metric_inc

# Telemetry/HTTP literals → constants (sonar S1192).
_AI_UC08_FULFILL_FAILED_TOTAL = "ai.uc08.fulfill.failed.total"

_log = get_logger("oneops.api.uc08")

router = APIRouter(prefix="/api/uc08", tags=["uc08-fulfillment"])

# Strong refs to fire-and-forget background tasks so the event loop can't GC
# them mid-flight (sonar S7502 — "save this task in a variable").
_BACKGROUND_TASKS: set = set()

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
_nats_dispatcher = None  # set via set_nats_dispatcher() — when present, /fulfill routes the executor kick over NATS instead of an in-process asyncio task


def set_gateway(g: Any) -> None:
    global _gateway
    _gateway = g


def set_cache(c: Any) -> None:
    global _cache
    _cache = c


def set_nats_dispatcher(nats: Any) -> None:
    """Enable NATS-routed executor kick. When set, /fulfill publishes the
    execute-event to the UC-8 agent instead of running an in-process task.
    """
    global _nats_dispatcher
    _nats_dispatcher = nats


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


class HistoricalSuggestion(BaseModel):
    """A field value suggested from past SRs on this catalog item."""
    value: str | None
    evidence_count: int
    total_population: int
    evidence_label: str       # e.g. "15 of 20 similar SRs"


class EnrichedFields(BaseModel):
    """Production-grade enrichment for the editable variables form.

    All fields are SUGGESTIONS. The technician can override any of them
    in the form before clicking Proceed."""

    # Catalog-derived (deterministic when match is set)
    category: str | None
    assignment_group_from_catalog: str | None
    sla_due_iso: str | None              # now() + catalog.estimated_total_minutes

    # Priority matrix (computed in-memory)
    impact: str | None                   # Low / On Users / On Department / On Business
    urgency: str | None                  # Low / Medium / High / Urgent
    priority_canonical: str | None       # Low / Medium / High / Urgent
    priority_p_letter: str | None        # P1 / P2 / P3 / P4

    # Historical pattern suggestions
    assigned_to: HistoricalSuggestion | None
    approved_by: HistoricalSuggestion | None
    ci_id: HistoricalSuggestion | None
    assignment_group_historical: HistoricalSuggestion | None


class MatchResponse(BaseModel):
    candidates: list[MatchCandidate]
    auto_pick: MatchCandidate | None
    verdict: str  # "AUTO_PICK" | "RERANK_CHOSEN" | "NO_MATCH" | "WRONG_INTENT"
    rerank_used: bool
    rerank_confidence: float
    rerank_reasoning: str
    query_text: str
    enrichment: EnrichedFields | None    # populated when verdict picks a catalog
    enrichment_catalog_item_id: str | None  # which catalog the enrichment is for
    # LLM-as-judge verification of the rerank/auto-pick decision.
    # Always present when a catalog was chosen. UNCERTAIN when the judge
    # itself failed (timeout / parse error) — never blocks the flow.
    judge_verdict: str | None        # "FAITHFUL" | "UNFAITHFUL" | "UNCERTAIN" | null
    judge_confidence: float | None   # 0.0 – 1.0
    judge_reasoning: str | None      # one-sentence rationale


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


# ── POST /api/uc08/create-sr ───────────────────────────────────────────


class CreateSrRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_text: str = Field(min_length=1, max_length=8000)
    """Raw free-text the user typed in the chat box."""

    requested_for: str | None = Field(default=None, max_length=64)
    """Optional explicit beneficiary; if absent, defaults to the caller."""


class CreateSrResponse(BaseModel):
    request_id: str
    tenant_id: str
    title: str
    description: str
    status: str
    stage: str
    requested_by: str
    requested_for: str | None
    created_at: str
    title_source: str
    description_source: str
    title_reasoning: str
    # LLM-as-judge verification of the title/description extraction.
    # UNCERTAIN when the judge itself failed (timeout / parse error) —
    # never blocks the flow.
    judge_verdict: str         # "FAITHFUL" | "UNFAITHFUL" | "UNCERTAIN"
    judge_confidence: float    # 0.0 – 1.0
    judge_reasoning: str       # one-sentence rationale


@router.post("/create-sr", responses={400: {"description": "Invalid request"}, 502: {"description": "Upstream error"}, 503: {"description": "Service unavailable"}})
async def create_sr(
    payload: CreateSrRequest,
    request: Request,
) -> CreateSrResponse:
    """Mint an SR id + LLM-extract title/description + INSERT itsm.request.

    Side effects (intended):
      • One row added to itsm.request with status='new', stage='intake'.
      • The first user comment is inserted into the comments JSONB.
      • The existing embedding-refresh trigger fires asynchronously;
        within ~5s the new SR becomes searchable by UC-2.

    Failure modes:
      • 401  — missing principal headers
      • 403  — role not permitted
      • 422  — Pydantic validation (empty body, extra field, …)
      • 502  — LLM gateway timeout or failure
      • 500  — unexpected DB failure (re-raised after structured log)
    """
    tenant_id, user_id, role = _principal(request)
    _require_role(role, _PERMITTED_MATCH_ROLES, "create-sr")

    gateway = _gateway or getattr(request.app.state, "gateway", None)
    if gateway is None:
        raise HTTPException(503, detail="LLM gateway not initialised")

    from oneops.use_cases.uc08_fulfillment.judge import judge_extraction
    from oneops.use_cases.uc08_fulfillment.sr_id import next_sr_id
    from oneops.use_cases.uc08_fulfillment.text_extract import (
        TextExtractError,
        extract_title_and_description,
    )

    # ── 1. LLM extract title + description ─────────────────────────────
    try:
        extract = await extract_title_and_description(
            user_text=payload.user_text,
            gateway=gateway,
            tenant_id=tenant_id,
            user_id=user_id,
        )
    except TextExtractError as exc:
        _metric_inc("ai.uc08.create_sr.failed.total", 1,
                    tenant_id=tenant_id, reason="text_extract_failure")
        # Log the real cause internally; return an opaque message to the client
        # (no internal exception text leaks past the API boundary — P0-3).
        _log.warning("uc08.create_sr.text_extract_failed",
                     tenant_id=tenant_id, error=str(exc)[:200])
        raise HTTPException(502, detail="text extraction failed") from exc

    # ── 1b. LLM-as-judge — validate the extraction (does not reject) ───
    # Runs concurrently with the INSERT below. Verdict is surfaced in
    # the response so the caller can flag low-confidence outputs to the
    # user. We never block, retry, or demote the row based on the judge.
    judge_task = asyncio.create_task(judge_extraction(
        gateway=gateway,
        tenant_id=tenant_id,
        user_id=user_id,
        user_text=payload.user_text,
        extracted_title=extract.title,
        extracted_description=extract.description,
    ))

    # ── 2. INSERT — mint SR id + the row in a single connection ────────
    requested_for = (payload.requested_for or user_id).strip()
    conn = await _connect()
    try:
        sr_id = await next_sr_id(tenant_id=tenant_id, conn=conn)

        # ── Principal resolution (production-grade) ──────────────────
        # itsm.request has FKs on (tenant_id, requested_by) and
        # (tenant_id, requested_for) → itsm.sys_user. If the header
        # principal isn't a real sys_user (demo/test/dev tenant), fall
        # back to the first sys_user in the tenant. This keeps the API
        # surface stable regardless of how the caller is authenticated
        # — the alternative is a confusing 500 on every dev session.
        requested_by_db = await conn.fetchval(
            "SELECT user_id FROM itsm.sys_user "
            "WHERE tenant_id=$1 AND user_id=$2",
            tenant_id, user_id,
        )
        if requested_by_db is None:
            requested_by_db = await conn.fetchval(
                "SELECT user_id FROM itsm.sys_user "
                "WHERE tenant_id=$1 ORDER BY user_id LIMIT 1",
                tenant_id,
            )
            if requested_by_db is None:
                raise HTTPException(
                    400,
                    detail=f"tenant {tenant_id!r} has no sys_user rows; "
                           "seed sys_user before calling create-sr",
                )
            _log.info(
                "uc08.create_sr.principal_fallback",
                tenant_id=tenant_id, header_user_id=user_id,
                resolved_to=requested_by_db,
            )

        if requested_for and requested_for != user_id:
            requested_for_db = await conn.fetchval(
                "SELECT user_id FROM itsm.sys_user "
                "WHERE tenant_id=$1 AND user_id=$2",
                tenant_id, requested_for,
            )
        else:
            requested_for_db = requested_by_db

        # First user comment captures the original free text verbatim.
        comment_id = f"COM-{sr_id}-01"
        import datetime as _dt
        import json as _json
        first_comment = [{
            "comment_id":  comment_id,
            "author":      requested_by_db,
            "author_role": "agent",
            "is_public":   True,
            "timestamp":   _dt.datetime.now(_dt.UTC)
                              .replace(microsecond=0).isoformat(),
            "text":        payload.user_text.strip(),
        }]

        row = await conn.fetchrow(
            """
            INSERT INTO itsm.request (
              tenant_id, request_id, title, description,
              status, stage, requested_by, requested_for,
              comments, created_at, updated_at
            ) VALUES (
              $1, $2, $3, $4,
              'new', 'intake', $5, $6,
              $7::jsonb, now(), now()
            )
            RETURNING tenant_id, request_id, title, description,
                      status, stage, requested_by, requested_for,
                      created_at
            """,
            tenant_id, sr_id,
            extract.title, extract.description,
            requested_by_db, requested_for_db,
            _json.dumps(first_comment),
        )
    finally:
        await conn.close()

    # ── 3. Collect judge verdict (concurrent with the INSERT above) ────
    judge_result = await judge_task

    _metric_inc("ai.uc08.create_sr.total", 1,
                tenant_id=tenant_id, status="success",
                judge_verdict=judge_result.verdict.value)
    _log.info("uc08.create_sr.completed",
              tenant_id=tenant_id, request_id=sr_id,
              title_source=extract.title_source,
              user_text_chars=len(payload.user_text),
              judge_verdict=judge_result.verdict.value,
              judge_confidence=judge_result.confidence)

    return CreateSrResponse(
        request_id=row["request_id"],
        tenant_id=row["tenant_id"],
        title=row["title"],
        description=row["description"],
        status=row["status"],
        stage=row["stage"],
        requested_by=row["requested_by"],
        requested_for=row["requested_for"],
        created_at=row["created_at"].isoformat(),
        title_source=extract.title_source,
        description_source=extract.description_source,
        title_reasoning=extract.reasoning,
        judge_verdict=judge_result.verdict.value,
        judge_confidence=judge_result.confidence,
        judge_reasoning=judge_result.reasoning,
    )


# ── POST /api/uc08/match ───────────────────────────────────────────────


@router.post("/match/stream")
async def match_catalog_stream(payload: MatchRequest, request: Request):
    """Live-streaming twin of /match — emits the shared agent/tool activity
    events, then the normal `MatchResponse` as `final.payload` so the
    fulfillment wizard renders its match result unchanged, panel on top."""
    import uuid

    from fastapi.responses import StreamingResponse

    from oneops.api.streaming import event_stream, publish_tool

    request_id = "req_" + uuid.uuid4().hex[:18]

    async def run_final():
        resp = await publish_tool(
            request_id, agent_id="uc08_fulfillment",
            tool_id="match_catalog", action="",
            run=lambda: match_catalog(payload, request))
        return resp.model_dump(mode="json")

    return StreamingResponse(
        event_stream(request_id, run_final),
        media_type="application/x-ndjson")


async def _decide_verdict(
    *, payload: MatchRequest, r: Any, candidates: list,
    gateway: Any, tenant_id: str, user_id: str,
) -> tuple[str, Any, bool, float, str]:
    """Stage-2 decision over the cosine matches. Returns
    `(verdict, auto_pick, rerank_used, rerank_confidence, rerank_reasoning)`:
    AUTO_PICK for a confident top-1, an LLM rerank for soft-zone queries
    (RERANK_CHOSEN / WRONG_INTENT / …), else NO_MATCH."""
    from oneops.use_cases.uc08_fulfillment.catalog_reranker import (
        rerank,
        should_rerank,
    )
    from oneops.use_cases.uc08_fulfillment.catalog_search import (
        CatalogSearchError,
    )
    if not r.matches:
        return "NO_MATCH", None, False, 0.0, ""
    top1 = r.matches[0]
    do_rerank, _ = should_rerank(top1.cosine_score)
    if not do_rerank:
        if top1.is_auto_pick:
            return "AUTO_PICK", candidates[0], False, 0.0, ""
        return "NO_MATCH", None, False, 0.0, ""

    # PRODUCTION FIX: rerank on the ORIGINAL user text (description), not the
    # LLM-cleaned title. Title normalisation strips intent signals ("How do
    # I..." → "Installation of...") which fools the classifier into reading a
    # how-to question as a fulfilment request. Preserve user intent at the
    # boundary; transform only for display.
    rerank_input = payload.sr_description or payload.sr_title
    try:
        rr = await rerank(
            tenant_id=tenant_id, sr_text=rerank_input,
            candidates=r.matches, gateway=gateway,
            cache=_cache, user_id=user_id,
        )
    except CatalogSearchError as exc:
        _metric_inc("ai.uc08.match.failed.total", 1,
                    tenant_id=tenant_id, reason="rerank_error")
        _log.warning("uc08.match.rerank_failed",
                     tenant_id=tenant_id, error=str(exc)[:200])
        raise HTTPException(502, detail="catalog rerank failed") from exc
    verdict = "RERANK_CHOSEN" if rr.verdict == "CHOSEN" else rr.verdict
    auto_pick = None
    if rr.verdict == "CHOSEN" and rr.chosen_match is not None:
        auto_pick = next(
            (c for c in candidates if c.catalog_item_id == rr.chosen), None)
    return verdict, auto_pick, True, rr.confidence, rr.reasoning


async def _build_enrichment(
    *, conn: Any, tenant_id: str, chosen_catalog_id: str,
) -> EnrichedFields:
    """Assemble the enrichment block for a chosen catalog item: catalog
    metadata + derived SLA/priority + historical pattern suggestions."""
    from datetime import datetime, timedelta

    from oneops.use_cases.uc08_fulfillment.historical_suggest import (
        suggest_for_catalog_item,
    )
    from oneops.use_cases.uc08_fulfillment.priority import (
        derive_and_compute,
    )

    cat_row = await conn.fetchrow(
        "SELECT category, owner_group, estimated_total_minutes "
        "FROM itsm.catalog_item "
        "WHERE tenant_id=$1 AND catalog_item_id=$2",
        tenant_id, chosen_catalog_id,
    )
    cat_category = cat_row["category"] if cat_row else None
    cat_owner_group = cat_row["owner_group"] if cat_row else None
    cat_minutes = int(
        cat_row["estimated_total_minutes"]) if (
            cat_row and cat_row["estimated_total_minutes"]) else 240

    sla_due = datetime.now(UTC) + timedelta(minutes=cat_minutes)
    prio = derive_and_compute(
        catalog_category=cat_category,
        requested_for_is_vip=False,        # VIP lookup is a future hook
        sla_minutes_remaining=cat_minutes,
        explicit_urgency_signal=None,
    )
    hist = await suggest_for_catalog_item(
        tenant_id=tenant_id, catalog_item_id=chosen_catalog_id, conn=conn,
    )

    def _to_hist(h: Any) -> HistoricalSuggestion:
        return HistoricalSuggestion(
            value=h.value,
            evidence_count=h.evidence_count,
            total_population=h.total_population,
            evidence_label=h.evidence_label,
        )

    return EnrichedFields(
        category=cat_category,
        assignment_group_from_catalog=cat_owner_group,
        sla_due_iso=sla_due.replace(microsecond=0).isoformat(),
        impact=prio["impact"],
        urgency=prio["urgency"],
        priority_canonical=prio["priority_canonical"],
        priority_p_letter=prio["priority_p"],
        assigned_to=_to_hist(hist.assigned_to),
        approved_by=_to_hist(hist.approved_by),
        ci_id=_to_hist(hist.ci_id),
        assignment_group_historical=_to_hist(hist.assignment_group),
    )


async def _collect_judge(
    judge_task: Any,
) -> tuple[str | None, float | None, str | None]:
    """Await the concurrent LLM-as-judge task (if any) and unpack its
    verdict / confidence / reasoning. Returns all-None when no judge ran."""
    if judge_task is None:
        return None, None, None
    jr = await judge_task
    return jr.verdict.value, jr.confidence, jr.reasoning


@router.post("/match", responses={502: {"description": "Upstream error"}, 503: {"description": "Service unavailable"}})
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

    gateway = _gateway or getattr(request.app.state, "gateway", None)
    if gateway is None:
        raise HTTPException(503, detail="LLM gateway not initialised")

    from oneops.use_cases.uc08_fulfillment.catalog_search import (
        CatalogSearchError,
        find_closest_catalog_items,
    )

    conn = await _connect()
    try:
        try:
            r = await find_closest_catalog_items(
                tenant_id=tenant_id,
                sr_title=payload.sr_title,
                sr_description=payload.sr_description,
                sr_category=payload.sr_category,
                gateway=gateway, conn=conn, top_k=payload.top_k,
            )
        except CatalogSearchError as exc:
            _metric_inc("ai.uc08.match.failed.total", 1,
                        tenant_id=tenant_id, reason="search_error")
            _log.warning("uc08.match.search_failed",
                         tenant_id=tenant_id, error=str(exc)[:200])
            raise HTTPException(502, detail="catalog search failed") from exc

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

        verdict, auto_pick_resp, rerank_used, rerank_conf, rerank_reason = \
            await _decide_verdict(
                payload=payload, r=r, candidates=candidates,
                gateway=gateway, tenant_id=tenant_id, user_id=user_id)

        chosen_catalog_id = (
            auto_pick_resp.catalog_item_id
            if auto_pick_resp is not None else None)

        # ── LLM-as-judge — verify the chosen catalog matches user intent.
        # Fires concurrently with the enrichment SQL below. Never raises.
        judge_task = None
        if chosen_catalog_id is not None:
            from oneops.use_cases.uc08_fulfillment.judge import judge_rerank
            judge_task = asyncio.create_task(judge_rerank(
                gateway=gateway,
                tenant_id=tenant_id,
                user_id=user_id,
                user_text=(payload.sr_description or payload.sr_title),
                chosen_catalog_id=chosen_catalog_id,
                chosen_catalog_label=auto_pick_resp.name,
                chosen_catalog_description=(auto_pick_resp.description or ""),
            ))

        # ── Enrichment — only when we have a concrete catalog pick ─────
        # Auto-pick OR rerank chose one. NO_MATCH / WRONG_INTENT → null.
        enrichment: EnrichedFields | None = None
        enrichment_cat_id: str | None = None
        if chosen_catalog_id is not None:
            enrichment = await _build_enrichment(
                conn=conn, tenant_id=tenant_id,
                chosen_catalog_id=chosen_catalog_id)
            enrichment_cat_id = chosen_catalog_id

        # ── Collect judge verdict (concurrent with enrichment above) ───
        judge_verdict, judge_confidence, judge_reasoning = \
            await _collect_judge(judge_task)

        _metric_inc("ai.uc08.match.total", 1,
                    tenant_id=tenant_id, verdict=verdict,
                    rerank_used=str(rerank_used).lower(),
                    enriched=str(enrichment is not None).lower(),
                    judge_verdict=judge_verdict or "none")
        return MatchResponse(
            candidates=candidates,
            auto_pick=auto_pick_resp,
            verdict=verdict,
            rerank_used=rerank_used,
            rerank_confidence=rerank_conf,
            rerank_reasoning=rerank_reason,
            query_text=r.query_text,
            enrichment=enrichment,
            enrichment_catalog_item_id=enrichment_cat_id,
            judge_verdict=judge_verdict,
            judge_confidence=judge_confidence,
            judge_reasoning=judge_reasoning,
        )
    finally:
        await conn.close()


# ── POST /api/uc08/fulfill ─────────────────────────────────────────────


@router.post("/fulfill", responses={404: {"description": "Not found"}, 409: {"description": "Conflict"}})
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
        FulfillmentRequest,
        TriggerType,
    )
    from oneops.use_cases.uc08_fulfillment.core import (
        fulfill_request as core_fulfill,
    )
    from oneops.use_cases.uc08_fulfillment.errors import (
        CatalogItemNotFoundError,
        DuplicateRequestError,
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
        _metric_inc(_AI_UC08_FULFILL_FAILED_TOTAL, 1,
                    tenant_id=tenant_id, reason="request_not_found")
        raise HTTPException(404, detail=str(exc))
    except CatalogItemNotFoundError as exc:
        _metric_inc(_AI_UC08_FULFILL_FAILED_TOTAL, 1,
                    tenant_id=tenant_id, reason="catalog_not_found")
        raise HTTPException(404, detail=str(exc))
    except DuplicateRequestError as exc:
        _metric_inc(_AI_UC08_FULFILL_FAILED_TOTAL, 1,
                    tenant_id=tenant_id, reason="duplicate")
        raise HTTPException(409, detail=str(exc))

    # ── Async executor kickoff ────────────────────────────────────────
    # core.fulfill_request only PERSISTS the RITM + tasks. The workflow
    # itself runs via executor.execute_plan. Production routing:
    #   • If a NATS dispatcher is wired (set_nats_dispatcher at boot),
    #     publish to oneops.uc08.fulfill.execute → UC8FulfillmentAgent
    #     picks it up in a queue group worker. Agent-to-agent flow is
    #     visible in NATS + Tempo.
    #   • Otherwise, fall back to an in-process asyncio task so the
    #     demo still works when NATS is unavailable. Status polling
    #     against /api/uc08/status/{ritm_id} reflects executor progress
    #     either way (state is persisted to Postgres).
    dispatch_via = "asyncio"
    if _nats_dispatcher is not None:
        try:
            from oneops.use_cases.uc08_fulfillment.nats_dispatcher import (
                dispatch_execute,
            )
            await dispatch_execute(
                nats=_nats_dispatcher,
                tenant_id=tenant_id,
                ritm_id=outcome.ritm_id,
                trace_id=outcome.trace_id,
            )
            dispatch_via = "nats"
        except Exception as exc:                                  # noqa: BLE001
            _log.warning(
                "uc08.fulfill.nats_dispatch_failed_falling_back_to_asyncio",
                tenant_id=tenant_id, ritm_id=outcome.ritm_id,
                error=str(exc)[:200],
            )

    if dispatch_via == "asyncio":
        import asyncio as _asyncio

        from oneops.use_cases.uc08_fulfillment.adapters.inprocess import (
            InProcessIntegrationAdapter,
        )
        from oneops.use_cases.uc08_fulfillment.executor import execute_plan

        async def _kick(ritm_id: str, tenant_id: str, trace_id: str | None):
            try:
                await execute_plan(
                    tenant_id=tenant_id, ritm_id=ritm_id,
                    adapter=InProcessIntegrationAdapter(),
                    connection_provider=_cp,
                    trace_id=trace_id,
                )
            except Exception as exc:                              # noqa: BLE001
                _log.warning("uc08.fulfill.executor_kick_failed",
                             tenant_id=tenant_id, ritm_id=ritm_id,
                             error=str(exc)[:200])

        _bg_task = _asyncio.create_task(_kick(
            outcome.ritm_id, tenant_id, outcome.trace_id,
        ))
        _BACKGROUND_TASKS.add(_bg_task)
        _bg_task.add_done_callback(_BACKGROUND_TASKS.discard)

    _metric_inc("ai.uc08.fulfill.total", 1,
                tenant_id=tenant_id, outcome=outcome.outcome.value,
                dispatch=dispatch_via)
    _log.info("uc08.fulfill.completed",
              tenant_id=tenant_id, ritm_id=outcome.ritm_id,
              dispatch=dispatch_via)
    return FulfillResponse(
        ritm_id=outcome.ritm_id,
        run_id=outcome.run_id,
        outcome=outcome.outcome.value,
        tasks_total=outcome.tasks_total,
        display_text=outcome.display_text,
        trace_id=outcome.trace_id,
    )


# ── GET /api/uc08/status/{ritm_id} ─────────────────────────────────────


@router.get("/status/{ritm_id}", responses={404: {"description": "Not found"}})
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
