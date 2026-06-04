"""API-side NATS dispatch for UC-5 propose/decide (Phase 4).

When a NATS client is available, the API publishes a request/reply to the
triage agent and unpacks the response. When NATS is down or no worker
responds within the timeout, we fall back to in-process execution so
the demo keeps working (graceful degradation, no silent loss).

Used by /api/uc05/routes.py — replaces the direct runner call when
NATS-mode is enabled.
"""
from __future__ import annotations

import json
from typing import Any

from oneops.adapters.nats_client import NATSClient
from oneops.observability import span
from oneops.use_cases.uc05_triage.agent import (
    SUBJECT_DECIDE,
    SUBJECT_PROPOSE,
)
from oneops.use_cases.uc05_triage.contracts import Outcome, Proposal

DEFAULT_TIMEOUT_S = 60.0


async def dispatch_propose(
    *,
    nats: NATSClient,
    tenant_id: str,
    service_id: str,
    ticket_row: dict[str, Any],
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Proposal:
    """Publish a propose request over NATS, await the Proposal reply."""
    payload = json.dumps({
        "tenant_id": tenant_id,
        "service_id": service_id,
        "ticket_row": ticket_row,
    }).encode("utf-8")
    with span("uc05.dispatch.propose",
              **{"oneops.tenant_id": tenant_id,
                 "uc05.service_id": service_id,
                 "nats.subject": SUBJECT_PROPOSE}):
        reply = await nats.request(SUBJECT_PROPOSE, payload, timeout=timeout_s)
        body = json.loads(reply.decode("utf-8"))
        if "error" in body:
            raise RuntimeError(f"agent error: {body['message']}")
        return Proposal.model_validate(body)


async def dispatch_decide(
    *,
    nats: NATSClient,
    proposal: Proposal,
    proposal_id: str,
    choice: str,
    actor_user_id: str,
    final_values: dict[str, Any] | None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Outcome:
    """Publish a decide request over NATS, await the Outcome reply."""
    payload = json.dumps({
        "proposal_id": proposal_id,
        "choice": choice,
        "actor_user_id": actor_user_id,
        "final_values": final_values,
        "proposal": json.loads(proposal.model_dump_json()),
    }).encode("utf-8")
    with span("uc05.dispatch.decide",
              **{"oneops.tenant_id": proposal.tenant_id,
                 "uc05.proposal_id": proposal_id,
                 "uc05.choice": choice,
                 "nats.subject": SUBJECT_DECIDE}):
        reply = await nats.request(SUBJECT_DECIDE, payload, timeout=timeout_s)
        body = json.loads(reply.decode("utf-8"))
        if "error" in body:
            raise RuntimeError(f"agent error: {body['message']}")
        return Outcome.model_validate(body)


__all__ = [
    "dispatch_propose", "dispatch_decide",
    "DEFAULT_TIMEOUT_S",
]
