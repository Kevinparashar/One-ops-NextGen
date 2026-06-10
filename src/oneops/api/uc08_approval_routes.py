"""UC-8 approval decision — the NON-chat "IT team handles it on the request" action.

IMPORTANT — this is NOT the catalog request path. UC-8 catalog *requesting* is
chat-only (the old button/REST `uc08_routes.py` was removed on purpose). This
endpoint is the runbook-mandated NON-chat APPROVE action: the runbook says
"approve/reassign → the IT team handles it on the request", so approve/reject must
live outside the chat agent. This is the seam a technician portal / IT-team tool
calls to release or stop a parked request.

It does NOT touch the catalog request flow, adds no chat tool, and is inert for
any request the gate didn't park.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from oneops.use_cases.uc08_fulfillment import approval as _approval
from oneops.use_cases.uc08_fulfillment import db as _db
from oneops.use_cases.uc08_fulfillment import tools as _tools

router = APIRouter(prefix="/api/uc08", tags=["uc08-approvals"])


class DecisionBody(BaseModel):
    tenant_id: str
    decision: Literal["approved", "rejected"]
    decided_by: str
    comment: str | None = None


@router.post("/approvals/{approval_id}/decision")
async def decide(approval_id: str, body: DecisionBody) -> dict:
    """Approve or reject one parked approval. On approve, releases the held
    fulfilment; on reject, stops it. The actor must be the assigned approver."""
    conn = await _db.default_connection_provider()
    try:
        outcome = await _approval.decide_approval(
            approval_id=approval_id, decision=body.decision,
            decided_by=body.decided_by, tenant_id=body.tenant_id,
            comment=body.comment, conn=conn)
    finally:
        await conn.close()

    if not outcome.ok:
        raise HTTPException(status_code=400, detail=outcome.message)

    dispatched = False
    if outcome.should_dispatch and outcome.ritm_id:
        dispatched = await _tools.release_fulfilment(
            tenant_id=body.tenant_id, ritm_id=outcome.ritm_id)

    return {
        "ok": True,
        "approval_id": approval_id,
        "ritm_id": outcome.ritm_id,
        "state": outcome.state,
        "dispatched": dispatched,
        "message": outcome.message,
    }
