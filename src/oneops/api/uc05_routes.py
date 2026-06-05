"""UC-5 Triage API routes.

Three resources, four endpoints:
  GET  /api/uc05/queue-summary           ← landing-page counts per service
  GET  /api/uc05/queue?service_id=...    ← per-service list of untriaged rows
  POST /api/uc05/propose                 ← run Tools 1+2+3 + assembly → Proposal
  POST /api/uc05/decide                  ← apply (yes) or discard (no)

Dev-mode auth: tenant + user + role come from x-tenant-id / x-user-id / x-role
headers — matches the existing chat API pattern in app.py. Production wires
JWT claims upstream and rewrites _principal_from_headers in app.py.

The store is injected via Depends(get_ticket_store) so prod paths can swap
JsonFixtureStore -> DbStore without touching the routes.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from oneops.observability import span
from oneops.observability.metrics import histogram as _metric_hist
from oneops.observability.metrics import increment as _metric_inc
from oneops.use_cases.uc05_triage.contracts import (
    DecisionChoice,
    Outcome,
    Proposal,
    ServiceId,
    TriageDecision,
)
from oneops.use_cases.uc05_triage.queue import (
    is_in_queue,
    missing_uc5_fields,
    triage_fields_for,
)
from oneops.use_cases.uc05_triage.stores.base import TicketStore
from oneops.use_cases.uc05_triage.stores.json_store import JsonFixtureStore

router = APIRouter(prefix="/api/uc05", tags=["uc05-triage"])

# Default store path — JSON fixture inside the UC-5 folder
_DEFAULT_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "oneops" / "use_cases" / "uc05_triage" / "fixtures" / "demo_tickets.json"
)

_TRIAGE_ROLES: frozenset[str] = frozenset(
    {
        "technician_l1", "technician_l2", "triage_desk", "admin",
        # Existing chat-side roles — UC-5 accepts these so the same identity
        # works for both /api/chat and /api/uc05/*. employee / guest are still
        # 403'd because they're not in this set.
        "service_desk_agent", "manager",
    }
)
"""Roles permitted to call UC-5 endpoints."""


# ── Dependency injection ─────────────────────────────────────────────────────

_store: TicketStore | None = None


def get_ticket_store() -> TicketStore:
    """Returns the configured store. Defaults to JsonFixtureStore against the
    demo fixture. Production overrides this in startup."""
    global _store
    if _store is None:
        _store = JsonFixtureStore(_DEFAULT_FIXTURE_PATH)
    return _store


def set_ticket_store(store: TicketStore) -> None:
    """Test/prod hook — swap the store implementation."""
    global _store
    _store = store


def _principal(request: Request) -> tuple[str, str, str]:
    """Extract (tenant_id, user_id, role) from headers. Matches app.py's pattern."""
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


def _require_triage_role(role: str) -> None:
    if role not in _TRIAGE_ROLES:
        raise HTTPException(
            403,
            detail=f"role {role!r} not permitted; need one of {sorted(_TRIAGE_ROLES)}",
        )


# ── Pydantic shapes for the API surface ──────────────────────────────────────

class QueueSummaryServiceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    untriaged_count: int


class QueueSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incidents: QueueSummaryServiceBlock
    requests: QueueSummaryServiceBlock


class QueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_id: str
    service_id: ServiceId
    title: str
    description_snippet: str
    created_at: str
    status: str
    missing_fields: list[str]
    missing_field_count: int


class ProposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_id: str = Field(min_length=1, max_length=64)
    service_id: ServiceId


class DecideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: str = Field(min_length=1, max_length=64)
    choice: DecisionChoice
    final_values: dict[str, Any] | None = None


# In-memory proposal cache. proposal_id -> Proposal. Production would use
# Dragonfly/Redis with TTL. For Section J the dict is fine.
_proposal_cache: dict[str, Proposal] = {}


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/queue-summary", response_model=QueueSummaryResponse)
async def queue_summary(
    request: Request,
    store: TicketStore = Depends(get_ticket_store),
) -> QueueSummaryResponse:
    tenant, user, role = _principal(request)
    _require_triage_role(role)

    with span("ai.request",
              **{"oneops.endpoint": "uc05.queue_summary",
                 "oneops.tenant_id": tenant, "oneops.user_id": user,
                 "oneops.role": role}):
        inc_rows = await store.list_all(service_id="incident", tenant_id=tenant)  # type: ignore[attr-defined]
        req_rows = await store.list_all(service_id="request", tenant_id=tenant)  # type: ignore[attr-defined]
    return QueueSummaryResponse(
        incidents=QueueSummaryServiceBlock(
            untriaged_count=sum(1 for r in inc_rows if is_in_queue(r, "incident"))
        ),
        requests=QueueSummaryServiceBlock(
            untriaged_count=sum(1 for r in req_rows if is_in_queue(r, "request"))
        ),
    )


@router.get("/queue", response_model=list[QueueItem])
async def queue(
    request: Request,
    service_id: ServiceId = Query(...),
    store: TicketStore = Depends(get_ticket_store),
) -> list[QueueItem]:
    tenant, user, role = _principal(request)
    _require_triage_role(role)

    with span("ai.request",
              **{"oneops.endpoint": "uc05.queue",
                 "oneops.tenant_id": tenant, "oneops.user_id": user,
                 "oneops.role": role, "uc05.service_id": service_id}):
        rows = await store.list_all(service_id=service_id, tenant_id=tenant)  # type: ignore[attr-defined]
    out: list[QueueItem] = []
    id_field = f"{service_id}_id"
    for row in rows:
        if not is_in_queue(row, service_id):
            continue
        missing = missing_uc5_fields(row, service_id)
        desc = str(row.get("description") or "")
        out.append(QueueItem(
            ticket_id=str(row.get(id_field) or ""),
            service_id=service_id,
            title=str(row.get("title") or ""),
            description_snippet=desc[:120] + ("..." if len(desc) > 120 else ""),
            created_at=str(row.get("created_at") or ""),
            status=str(row.get("status") or "new"),
            missing_fields=missing,
            missing_field_count=len(missing),
        ))
    out.sort(key=lambda r: r.created_at)
    return out


# The executor-backed propose runner — UC-5's ONLY propose path (Phase 3b: the
# bespoke runner/graph were retired). Runs the triage plan on the MAIN executor.
# Real wiring in app.py at startup; tests inject a stub. Signature carries the
# actor context the executor's authz_recheck before-hook needs:
#     async fn(*, service_id, ticket_id, tenant_id, user_id, role) -> Proposal
ExecutorProposeRunner = Callable[..., Awaitable[Proposal]]
_executor_propose_runner: ExecutorProposeRunner | None = None


def set_executor_propose_runner(fn: ExecutorProposeRunner | None) -> None:
    """Wire (or clear) the executor-backed propose runner."""
    global _executor_propose_runner
    _executor_propose_runner = fn


# NATS-mode override for the decide path. When set, /decide forwards to NATS
# instead of calling apply_triage_decision in-process.
# Signature: async fn(proposal, payload, actor_user_id) -> Outcome
DecideDispatcher = Callable[..., Awaitable[Any]]
_decide_dispatcher: DecideDispatcher | None = None


def set_decide_dispatcher(fn: DecideDispatcher | None) -> None:
    """Wire the NATS decide hop. None ⇒ local in-process apply."""
    global _decide_dispatcher
    _decide_dispatcher = fn


@router.post("/propose/stream")
async def propose_stream(payload: ProposeRequest, request: Request):
    """Live-streaming twin of /propose — emits the shared agent/tool activity
    events, then the normal `Proposal` as `final.payload` so the triage card
    renders unchanged with the live panel on top."""
    import uuid

    from fastapi.responses import StreamingResponse

    from oneops.api.streaming import event_stream, publish_tool
    from oneops.executor.step_runner import _tool_action

    request_id = "req_" + uuid.uuid4().hex[:18]
    reg = getattr(request.app.state, "registry", None)
    tool = reg.tools.get_optional("check_duplicate_candidates") if reg else None
    action = _tool_action(tool) if tool else ""
    store = get_ticket_store()

    async def run_final():
        prop = await publish_tool(
            request_id, agent_id="uc05_triage",
            tool_id="check_duplicate_candidates", action=action,
            run=lambda: propose(payload, request, store=store))
        return prop.model_dump(mode="json")

    return StreamingResponse(
        event_stream(request_id, run_final),
        media_type="application/x-ndjson")


@router.post("/propose", response_model=Proposal)
async def propose(
    payload: ProposeRequest,
    request: Request,
    store: TicketStore = Depends(get_ticket_store),
) -> Proposal:
    tenant, user, role = _principal(request)
    _require_triage_role(role)

    _sp_cm = span("ai.request",
                   **{"oneops.endpoint": "uc05.propose",
                      "oneops.tenant_id": tenant, "oneops.user_id": user,
                      "oneops.role": role, "uc05.ticket_id": payload.ticket_id,
                      "uc05.service_id": payload.service_id})
    _sp_cm.__enter__()
    import time as _time
    _t0 = _time.perf_counter()
    try:
        return await _propose_impl(payload=payload, tenant=tenant,
                                   user=user, role=role, store=store)
    finally:
        # Latency emission — defensive try/except so any metric-side error
        # is invisible to the caller (rule §2.7).
        try:
            _metric_hist("ai.agent.latency_ms",
                         (_time.perf_counter() - _t0) * 1000.0,
                         agent_id="uc05_triage",
                         tenant_id=tenant,
                         operation="propose")
        except Exception:                                       # noqa: BLE001
            pass
        _sp_cm.__exit__(None, None, None)


async def _propose_impl(*, payload, tenant, user, role, store):
    _metric_inc("ai.agent.runs.total", 1, agent_id="uc05_triage",
                tenant_id=tenant, operation="propose",
                status="started")
    try:
        # Load the row first — tenant-scoped read; 404 on miss (no info leak).
        # These API-level gates (404/409) stay at the route for both runners;
        # the executor path re-loads the row inside its handlers (standard
        # registry-handler pattern), which is harmless.
        try:
            row = await store.get_ticket(
                service_id=payload.service_id,
                ticket_id=payload.ticket_id,
                tenant_id=tenant,
            )
        except KeyError:
            raise HTTPException(404, detail="ticket not found in this tenant")

        # Refuse if already fully triaged (Rule B)
        if not missing_uc5_fields(row, payload.service_id):
            raise HTTPException(409, detail="ticket already fully triaged")

        # Run the triage plan on the MAIN executor (the only propose path).
        if _executor_propose_runner is None:
            raise HTTPException(503, detail="executor propose runner not wired")
        proposal = await _executor_propose_runner(
            service_id=payload.service_id,
            ticket_id=payload.ticket_id,
            tenant_id=tenant,
            user_id=user,
            role=role,
        )
        _proposal_cache[proposal.proposal_id] = proposal
        _metric_inc("ai.agent.runs.total", 1, agent_id="uc05_triage",
                    tenant_id=tenant, operation="propose",
                    status="success")
        return proposal
    except HTTPException:
        # 4xx and similar are deliberate refusals (ticket not found,
        # already triaged, role denied). They are NOT worker failures
        # and must not pollute the success-ratio denominator.
        _metric_inc("ai.agent.runs.total", 1, agent_id="uc05_triage",
                    tenant_id=tenant, operation="propose",
                    status="refused")
        raise
    except Exception:
        _metric_inc("ai.agent.runs.total", 1, agent_id="uc05_triage",
                    tenant_id=tenant, operation="propose",
                    status="failed")
        raise


@router.post("/decide", response_model=Outcome)
async def decide(
    payload: DecideRequest,
    request: Request,
    store: TicketStore = Depends(get_ticket_store),
) -> Outcome:
    tenant, user, role = _principal(request)
    _require_triage_role(role)

    _sp_cm = span("ai.request",
                   **{"oneops.endpoint": "uc05.decide",
                      "oneops.tenant_id": tenant, "oneops.user_id": user,
                      "oneops.role": role,
                      "uc05.proposal_id": payload.proposal_id,
                      "uc05.choice": payload.choice})
    _sp_cm.__enter__()
    import time as _time
    _t0 = _time.perf_counter()
    try:
        return await _decide_impl(payload=payload, tenant=tenant, user=user, store=store)
    finally:
        try:
            _metric_hist("ai.agent.latency_ms",
                         (_time.perf_counter() - _t0) * 1000.0,
                         agent_id="uc05_triage",
                         tenant_id=tenant,
                         operation="decide")
        except Exception:                                       # noqa: BLE001
            pass
        _sp_cm.__exit__(None, None, None)


async def _decide_impl(*, payload, tenant, user, store):
    _metric_inc("ai.agent.runs.total", 1, agent_id="uc05_triage",
                tenant_id=tenant, operation="decide",
                status="started")
    try:
        return await _decide_impl_inner(payload=payload, tenant=tenant,
                                          user=user, store=store)
    except HTTPException:
        # Deliberate refusal — does not represent a worker failure.
        _metric_inc("ai.agent.runs.total", 1, agent_id="uc05_triage",
                    tenant_id=tenant, operation="decide",
                    status="refused")
        raise
    except Exception:
        _metric_inc("ai.agent.runs.total", 1, agent_id="uc05_triage",
                    tenant_id=tenant, operation="decide",
                    status="failed")
        raise


async def _decide_impl_inner(*, payload, tenant, user, store):
    proposal = _proposal_cache.get(payload.proposal_id)
    if proposal is None:
        raise HTTPException(404, detail="proposal not found or expired")
    if proposal.tenant_id != tenant:
        raise HTTPException(404, detail="proposal not found or expired")

    # Validate technician-edited values are restricted to UC-5-owned fields
    if payload.final_values:
        allowed = set(triage_fields_for(proposal.service_id))
        bad = [k for k in payload.final_values if k not in allowed]
        if bad:
            raise HTTPException(
                422,
                detail=f"final_values contains non-triage fields: {bad}",
            )

    # Build the TriageDecision; actor_user_id always from headers, never body
    decision = TriageDecision(
        proposal_id=payload.proposal_id,
        choice=payload.choice,
        actor_user_id=user,
    )

    try:
        if _decide_dispatcher is not None:
            outcome = await _decide_dispatcher(
                proposal=proposal,
                proposal_id=payload.proposal_id,
                choice=payload.choice,
                actor_user_id=user,
                final_values=payload.final_values,
            )
        else:
            from oneops.use_cases.uc05_triage.apply import apply_triage_decision
            outcome = await apply_triage_decision(
                proposal=proposal,
                decision=decision,
                final_values=payload.final_values,
                store=store,
            )
    except RuntimeError as exc:
        # Optimistic-lock conflict from the store
        raise HTTPException(409, detail=str(exc))
    except KeyError:
        raise HTTPException(404, detail="ticket not found")

    # Evict the proposal — single-use
    _proposal_cache.pop(payload.proposal_id, None)
    _metric_inc("ai.agent.runs.total", 1, agent_id="uc05_triage",
                tenant_id=tenant, operation="decide",
                status="success")
    return outcome
