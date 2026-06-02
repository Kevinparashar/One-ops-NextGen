"""UC-2 Similar Tickets — button-mode REST routes.

Endpoint:
  POST /api/uc02/similar-tickets

Topology (matches UC-5):
  Browser → AWS API Gateway → ingress (this) → `_similar_runner`
                                              ↓
                              dev: in-process find_similar()
                              prod: NATS dispatch_find_similar() → agent worker
                              ↓
                              identical SimilarTicketsResponse model

Headers (dev mode; prod wires JWT upstream):
  x-tenant-id, x-user-id, x-role

Edge cases handled at the boundary:
  • Empty / whitespace ticket_id → 422 via Pydantic (also explicit 400)
  • Bare digits without service_id → 400 ambiguous
  • Unsupported prefix (PBM/CHG/KB) → 400 out of scope
  • Service_id contradicts prefix → 400
  • Ticket not found → 404 (single message; no cross-tenant leak)
  • RBAC denial → 403
  • Cache hit → 200 with `cached: true`, sub-10ms
  • DB unreachable → 503
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from oneops.observability import get_logger, span
from oneops.observability.metrics import increment as _metric_inc
from oneops.uc_common import TimeFilter
from oneops.use_cases.uc02_similar_tickets.contracts import (
    PreferStatus,
    ServiceId,
    SimilarTicketsResponse,
)
from oneops.use_cases.uc02_similar_tickets.id_resolver import (
    ResolveError,
)
from oneops.use_cases.uc02_similar_tickets.id_resolver import (
    resolve as resolve_id,
)

_log = get_logger("oneops.api.uc02")

router = APIRouter(prefix="/api/uc02", tags=["uc02-similar-tickets"])

# Same role set as UC-5 — service-desk-grade and above can run similarity.
# end_user can call too but the SQL RBAC predicate filters their results
# to tickets they reported (silent exclusion per UC-2.7).
_PERMITTED_ROLES: frozenset[str] = frozenset({
    "technician_l1", "technician_l2", "triage_desk", "admin",
    "service_desk_agent", "manager",
    "end_user", "requester",
})


# ── Wire-points (DI / NATS swap) ─────────────────────────────────────────────

SimilarRunner = Callable[..., Awaitable[SimilarTicketsResponse]]

_similar_runner: SimilarRunner | None = None


def set_similar_runner(fn: SimilarRunner | None) -> None:
    """Wire either in-process `core.find_similar` (dev) or the NATS dispatcher
    (prod). Called from app.py lifespan."""
    global _similar_runner
    _similar_runner = fn


# Cache (Dragonfly) — same backend as chat-turn cache. Per-call key carries
# tenant + ticket + scope, so cross-tenant collisions are impossible.
_cache_get: Callable[..., Awaitable[dict | None]] | None = None
_cache_put: Callable[..., Awaitable[None]] | None = None


def set_result_cache(
    *,
    getter: Callable[..., Awaitable[dict | None]] | None,
    putter: Callable[..., Awaitable[None]] | None,
) -> None:
    """Optional Dragonfly cache. None ⇒ no caching (still correct, just slower)."""
    global _cache_get, _cache_put
    _cache_get = getter
    _cache_put = putter


# ── Request / response wrappers ──────────────────────────────────────────────


class SimilarTicketsRouteRequest(BaseModel):
    """POST body. Extra fields rejected (rule §2.7)."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1, max_length=64)
    service_id: ServiceId | None = None
    max_results: int = Field(default=5, ge=1, le=20)
    time_filter: TimeFilter | None = None
    """Structured time scope. Button callers pass it directly; chat callers
    skip this field — the executor's extractor populates it from the
    message text and threads it through the tool context."""
    same_category_only: bool = False
    same_service_only: bool = False
    prefer_status: PreferStatus = "any"
    min_similarity_score: float = Field(default=0.5, ge=0.0, le=1.0)
    diagnosis_confirm: bool = True


# ── helpers ──────────────────────────────────────────────────────────────────


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


def _require_role(role: str) -> None:
    if role not in _PERMITTED_ROLES:
        raise HTTPException(
            403, detail=f"role {role!r} cannot call /api/uc02/* "
                        f"(allowed: {sorted(_PERMITTED_ROLES)})")


def _cache_key(
    *, tenant_id: str, user_id: str, role: str, body: SimilarTicketsRouteRequest,
    canonical_id: str, service_id: str,
) -> str:
    import hashlib

    from oneops.api.cache_version import PIPELINE_CACHE_VERSION
    tf_part = (
        body.time_filter.model_dump_json()
        if body.time_filter is not None else "_"
    )
    raw = (
        f"{tenant_id}\x1f{user_id}\x1f{role}\x1f"
        f"{canonical_id}\x1f{service_id}\x1f"
        f"{body.max_results}\x1f{tf_part}\x1f"
        f"{body.same_category_only}\x1f{body.same_service_only}\x1f"
        f"{body.prefer_status}\x1f{body.min_similarity_score:.3f}\x1f"
        f"{body.diagnosis_confirm}\x1fv={PIPELINE_CACHE_VERSION}"
    )
    return "uc02:sim:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


# ── route ────────────────────────────────────────────────────────────────────


@router.post("/similar-tickets", response_model=SimilarTicketsResponse)
async def similar_tickets(
    payload: SimilarTicketsRouteRequest,
    request: Request,
) -> SimilarTicketsResponse:
    """Find tickets similar to `payload.ticket_id` for the calling tenant.

    Spec: ai-service-use-cases.md §UC-2.
    """
    tenant_id, user_id, role = _principal(request)
    _require_role(role)

    # ── ID canonicalisation (UC-2 edge cases #1–#8) ──────────────────────────
    try:
        resolved = resolve_id(payload.ticket_id, payload.service_id)
    except ResolveError as e:
        _metric_inc("ai.uc02.bad_request.total", 1,
                    tenant_id=tenant_id, reason="resolve_error")
        raise HTTPException(400, detail=str(e))

    canonical_id = resolved.entity_id
    service_id: ServiceId = resolved.service_id  # type: ignore[assignment]

    # Dashboard counter — emit the CANONICAL registry agent_id so Grafana
    # aggregates button + chat into one bar (both go through the same agent
    # `uc02_similar_tickets`). Previously this emitted
    # the legacy `similar_tickets_agent` label which split the dashboard
    # into two phantom rows. Aligned 2026-05-30.
    _metric_inc("ai.agent.runs.total", 1,
                agent_id="uc02_similar_tickets",
                tenant_id=tenant_id, service_id=service_id,
                status="started")

    with span("ai.request", **{
        "oneops.endpoint": "uc02.similar-tickets",
        "oneops.agent_id": "uc02_similar_tickets",
        "oneops.tenant_id": tenant_id,
        "oneops.user_id": user_id,
        "oneops.role": role,
        "uc02.service_id": service_id,
        "uc02.source_ticket_id": canonical_id,
        "uc02.max_results": payload.max_results,
    }):
        # ── Cache lookup ────────────────────────────────────────────────────
        ckey = _cache_key(
            tenant_id=tenant_id, user_id=user_id, role=role,
            body=payload, canonical_id=canonical_id, service_id=service_id,
        )
        if _cache_get is not None:
            try:
                cached = await _cache_get(tenant_id=tenant_id, key=ckey)
            except Exception:                                      # noqa: BLE001
                cached = None
            if cached is not None:
                _metric_inc("ai.uc02.cache.hits.total", 1,
                            tenant_id=tenant_id, service_id=service_id)
                _log.info("oneops.api.uc02.cache_hit",
                          tenant_id=tenant_id, ticket_id=canonical_id)
                cached["cached"] = True
                _metric_inc("ai.agent.runs.total", 1,
                            agent_id="uc02_similar_tickets",
                            tenant_id=tenant_id, service_id=service_id,
                            status="success")
                return SimilarTicketsResponse(**cached)

        if _similar_runner is None:
            raise HTTPException(
                503, detail="UC-2 runner not wired — check app startup logs")

        # ── Dispatch to runner (in-process or NATS) ──────────────────────────
        try:
            resp = await _similar_runner(
                tenant_id=tenant_id,
                service_id=service_id,
                ticket_id=canonical_id,
                user_id=user_id,
                role=role,
                max_results=payload.max_results,
                time_filter=payload.time_filter,
                same_category_only=payload.same_category_only,
                same_service_only=payload.same_service_only,
                prefer_status=payload.prefer_status,
                min_similarity_score=payload.min_similarity_score,
                diagnosis_confirm=payload.diagnosis_confirm,
            )
        except RuntimeError as e:
            msg = str(e)
            if "no symptom_anchor embedding" in msg:
                # Ticket is real but its anchor row is not yet computed.
                # 503 says "try again shortly" — refresh worker will catch up.
                _metric_inc("ai.uc02.anchor_pending.total", 1,
                            tenant_id=tenant_id, service_id=service_id)
                raise HTTPException(
                    503, detail="embedding refresh pending for this ticket — "
                                "please retry in a few seconds")
            if "not found" in msg.lower():
                # Distinguishes a genuinely missing ticket from auth — but the
                # message is the same to avoid existence leaks (UC-2.4/2.7).
                raise HTTPException(404, detail="ticket not found")
            _log.warning("oneops.api.uc02.runner_error",
                         tenant_id=tenant_id, ticket_id=canonical_id, error=msg)
            raise HTTPException(500, detail="similar tickets lookup failed")
        except Exception:                                          # noqa: BLE001
            _log.exception("oneops.api.uc02.unexpected",
                           tenant_id=tenant_id, ticket_id=canonical_id)
            raise HTTPException(500, detail="similar tickets lookup failed")

        _metric_inc("ai.uc02.cache.misses.total", 1,
                    tenant_id=tenant_id, service_id=service_id)
        # ── Cache write (success path only) ──────────────────────────────────
        if _cache_put is not None:
            try:
                await _cache_put(
                    tenant_id=tenant_id, key=ckey,
                    value=resp.model_dump(mode="json"),
                )
                _metric_inc("ai.uc02.cache.writes.total", 1,
                            tenant_id=tenant_id, service_id=service_id)
            except Exception:                                      # noqa: BLE001
                pass  # cache write failure is never fatal

        _metric_inc("ai.agent.runs.total", 1,
                    agent_id="uc02_similar_tickets",
                    tenant_id=tenant_id, service_id=service_id,
                    status="success")
        return resp


@router.post("/similar-tickets/stream")
async def similar_tickets_stream(
    payload: SimilarTicketsRouteRequest, request: Request,
):
    """Live-streaming twin of /similar-tickets.

    Emits the shared `tool_start`/`tool_done` activity events (so the button
    shows the same live "agent + tool" panel as chat) and returns the normal
    `SimilarTicketsResponse` as `final.payload` — the modal renders its own
    results view unchanged, with the live panel added on top.
    """
    import uuid

    from fastapi.responses import StreamingResponse

    from oneops.api.streaming import event_stream, publish_tool
    from oneops.executor.step_runner import _tool_action

    request_id = "req_" + uuid.uuid4().hex[:18]
    reg = getattr(request.app.state, "registry", None)
    tool = reg.tools.get_optional("find_similar_entities") if reg else None
    action = _tool_action(tool) if tool else ""

    async def run_final():
        resp = await publish_tool(
            request_id, agent_id="uc02_similar_tickets",
            tool_id="find_similar_entities", action=action,
            run=lambda: similar_tickets(payload, request))
        return resp.model_dump(mode="json")

    return StreamingResponse(
        event_stream(request_id, run_final),
        media_type="application/x-ndjson")


__all__ = [
    "router",
    "set_similar_runner",
    "set_result_cache",
    "SimilarTicketsRouteRequest",
]
