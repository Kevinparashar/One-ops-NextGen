"""UC-5 triage agent — NATS subscriber for DECIDE (Phase 3b: propose retired).

Wire layout (propose now runs on the MAIN executor, not here):

    API /api/uc05/decide   ──publish──▶  oneops.uc05.triage.decide
                                          │
                                          ▼
                                  TriageAgent worker (this module)
                                          │
                                          ▼
                               apply_triage_decision(...)
                                          │
                                          ▼
                          NATS reply ──▶  API → Outcome JSON

The agent uses a queue group (`uc05-triage-workers`) so multiple replicas
share the load and at-most-one consumes each message. W3C traceparent is
auto-injected by the NATS client adapter so Tempo shows one trace tree
spanning API → broker → worker → store.
"""
from __future__ import annotations

import json
from typing import Any

from oneops.adapters.nats_client import NATSClient
from oneops.observability import span
from oneops.use_cases.uc05_triage.apply import apply_triage_decision
from oneops.use_cases.uc05_triage.contracts import (
    Proposal,
    TriageDecision,
)
from oneops.use_cases.uc05_triage.stores.base import TicketStore

SUBJECT_DECIDE = "oneops.uc05.triage.decide"
SUBJECT_APPLIED = "oneops.uc05.triage.applied"
"""Subject vocabulary (propose retired in Phase 3b — handled by the executor):
  • decide    — request/reply, payload {proposal_id, choice, actor_user_id,
                                         final_values, proposal}
  • applied   — fire-and-forget broadcast on successful Yes, for SIEM/audit
"""

QUEUE_GROUP = "uc05-triage-workers"


class TriageAgent:
    """NATS-subscribed UC-5 DECIDE worker (apply the approved triage values).

    Propose is no longer handled here — it runs on the main executor (Phase 3b).
    Started in app.py _lifespan; gracefully stopped on shutdown.
    """

    def __init__(
        self,
        *,
        nats: NATSClient,
        store: TicketStore,
    ) -> None:
        self._nats = nats
        self._store = store
        self._subs: list[Any] = []

    async def start(self) -> None:
        """Subscribe to the decide subject with the shared queue group."""
        sub_decide = await self._nats.subscribe(
            SUBJECT_DECIDE, handler=self._on_decide, queue=QUEUE_GROUP,
        )
        self._subs.append(sub_decide)

    async def stop(self) -> None:
        """Drain subscriptions on shutdown."""
        for sub in self._subs:
            try:
                await sub.drain() if hasattr(sub, "drain") else None
                if hasattr(sub, "unsubscribe"):
                    await sub.unsubscribe()
            except Exception:
                pass
        self._subs.clear()

    # ── handlers ─────────────────────────────────────────────────────────────

    async def _on_decide(self, msg: Any) -> None:
        """Resolve a decision (yes/no) — read the proposal payload from the
        message, call apply_triage_decision, return the Outcome."""
        with span("uc05.agent.on_decide",
                  **{"nats.subject": SUBJECT_DECIDE}):
            try:
                payload = json.loads(msg.data.decode("utf-8"))
                proposal = Proposal.model_validate(payload["proposal"])
                decision = TriageDecision(
                    proposal_id=payload["proposal_id"],
                    choice=payload["choice"],
                    actor_user_id=payload["actor_user_id"],
                )
                outcome = await apply_triage_decision(
                    proposal=proposal,
                    decision=decision,
                    final_values=payload.get("final_values"),
                    store=self._store,
                )
                reply = outcome.model_dump_json().encode("utf-8")

                # Broadcast applied event for SIEM/audit consumers
                if outcome.outcome == "applied":
                    await self._nats.publish(
                        SUBJECT_APPLIED,
                        json.dumps({
                            "proposal_id": outcome.proposal_id,
                            "ticket_id": outcome.ticket_id,
                            "tenant_id": proposal.tenant_id,
                            "actor_user_id": outcome.actor_user_id,
                            "decided_at": outcome.decided_at.isoformat(),
                        }).encode("utf-8"),
                    )
            except Exception as exc:
                reply = json.dumps({
                    "error": "decide_failed",
                    "message": str(exc)[:200],
                }).encode("utf-8")
            if msg.reply:
                await self._nats.publish(msg.reply, reply)


__all__ = [
    "TriageAgent",
    "SUBJECT_DECIDE", "SUBJECT_APPLIED",
    "QUEUE_GROUP",
]
