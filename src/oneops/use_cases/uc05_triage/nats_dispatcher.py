"""API-side NATS dispatch for UC-5 DECIDE (Phase 4; propose retired in 3b).

When a NATS client is available, the API publishes a request/reply to the
triage agent and unpacks the response. When NATS is down or no worker
responds within the timeout, the route falls back to in-process apply so
the path keeps working (graceful degradation, no silent loss).

Propose no longer dispatches over NATS — it runs on the main executor.
"""
from __future__ import annotations

import json
from typing import Any

from oneops.adapters.nats_client import NATSClient
from oneops.observability import span
from oneops.use_cases.uc05_triage.agent import SUBJECT_DECIDE
from oneops.use_cases.uc05_triage.contracts import Outcome, Proposal

DEFAULT_TIMEOUT_S = 60.0


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
    "dispatch_decide",
    "DEFAULT_TIMEOUT_S",
]
