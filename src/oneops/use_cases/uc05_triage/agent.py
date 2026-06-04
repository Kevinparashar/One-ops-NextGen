"""UC-5 triage agent — NATS subscriber (Phase 4).

Wire layout:

    API /api/uc05/propose  ──publish──▶  oneops.uc05.triage.propose
                                          │
                                          ▼
                                  TriageAgent worker
                                  (this module)
                                          │
                                          ▼
                               build_runner(...).runner(...)
                                          │
                                          ▼
                          NATS reply ──▶  API → Proposal JSON

    API /api/uc05/decide   ──publish──▶  oneops.uc05.triage.decide
                                          │
                                          ▼
                                  TriageAgent worker
                                          │
                                          ▼
                               apply_triage_decision(...)
                                          │
                                          ▼
                          NATS reply ──▶  API → Outcome JSON

The agent uses a queue group (`uc05-triage-workers`) so multiple replicas
share the load and at-most-one consumes each message. W3C traceparent is
auto-injected by the NATS client adapter so Tempo shows one trace tree
spanning API → broker → worker → tools → gateway.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from oneops.adapters.nats_client import NATSClient
from oneops.observability import span
from oneops.use_cases.uc05_triage.apply import apply_triage_decision
from oneops.use_cases.uc05_triage.contracts import (
    Proposal,
    TriageDecision,
)
from oneops.use_cases.uc05_triage.stores.base import TicketStore

SUBJECT_PROPOSE = "oneops.uc05.triage.propose"
SUBJECT_DECIDE = "oneops.uc05.triage.decide"
SUBJECT_APPLIED = "oneops.uc05.triage.applied"
"""Subject vocabulary (locked 2026-05-29):
  • propose   — request/reply, payload {tenant_id, service_id, ticket_row}
  • decide    — request/reply, payload {proposal_id, choice, actor_user_id,
                                         final_values, proposal}
  • applied   — fire-and-forget broadcast on successful Yes, for SIEM/audit
"""

QUEUE_GROUP = "uc05-triage-workers"

# Type for the production runner — matches Section J's _tools_runner shape.
RunnerFn = Callable[..., Awaitable[Proposal]]


class TriageAgent:
    """NATS-subscribed UC-5 triage worker.

    Started in app.py _lifespan; gracefully stopped on shutdown.
    """

    def __init__(
        self,
        *,
        nats: NATSClient,
        runner: RunnerFn,
        store: TicketStore,
    ) -> None:
        self._nats = nats
        self._runner = runner
        self._store = store
        self._subs: list[Any] = []

    async def start(self) -> None:
        """Subscribe to propose + decide subjects with the shared queue group."""
        sub_propose = await self._nats.subscribe(
            SUBJECT_PROPOSE, handler=self._on_propose, queue=QUEUE_GROUP,
        )
        sub_decide = await self._nats.subscribe(
            SUBJECT_DECIDE, handler=self._on_decide, queue=QUEUE_GROUP,
        )
        self._subs.extend([sub_propose, sub_decide])

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

    async def _on_propose(self, msg: Any) -> None:
        """Run a triage proposal in response to a propose request. NATS
        client handler signature varies; we use msg.data / msg.respond.
        Traceparent already extracted from headers by NATSClient."""
        with span("uc05.agent.on_propose",
                  **{"nats.subject": SUBJECT_PROPOSE}):
            try:
                payload = json.loads(msg.data.decode("utf-8"))
                proposal = await self._runner(
                    ticket_row=payload["ticket_row"],
                    service_id=payload["service_id"],
                    tenant_id=payload["tenant_id"],
                )
                reply = proposal.model_dump_json().encode("utf-8")
            except Exception as exc:
                reply = json.dumps({
                    "error": "propose_failed",
                    "message": str(exc)[:200],
                }).encode("utf-8")
            if msg.reply:
                await self._nats.publish(msg.reply, reply)

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
    "SUBJECT_PROPOSE", "SUBJECT_DECIDE", "SUBJECT_APPLIED",
    "QUEUE_GROUP",
]
