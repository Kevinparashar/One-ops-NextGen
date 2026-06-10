"""Phase 4 tests — API-side NATS dispatcher (request/reply over NATS)."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from oneops.errors import NATSUnavailableError
from oneops.use_cases.uc05_triage.contracts import Proposal
from oneops.use_cases.uc05_triage.nats_dispatcher import dispatch_decide


def _proposal_json() -> str:
    p = Proposal(
        proposal_id="p-001",
        ticket_id="INC0000001",
        service_id="incident",
        tenant_id="T001",
        created_at=datetime.now(UTC),
        suggested_category="network",
        suggested_subcategory="vpn",
        suggested_assigned_to="USR00003",
        suggested_ci_id="CI0000001",
        suggested_impact="On Department",
        suggested_urgency="High",
        suggested_priority="High",
        suggested_assignment_group="GRP-NETOPS",
        suggested_tags=["vpn"],
        duplicate_verdict="none",
        overall_confidence_score=0.8,
        confidence_tier="propose",
        risk_class="medium",
        prioritization_basis={"impact": "llm_inferred"},
        assignment_basis="majority_of_top_k",
        assignment_confidence=0.8,
    )
    return p.model_dump_json()


class _ReplyingNats:
    def __init__(self, reply: bytes) -> None:
        self._reply = reply
        self.request_calls: list[tuple[str, bytes]] = []

    async def request(self, subject, payload, *, timeout=30, headers=None):
        self.request_calls.append((subject, payload))
        return self._reply


class _RaisingNats:
    async def request(self, subject, payload, *, timeout=30, headers=None):
        raise NATSUnavailableError("no worker")


# ── dispatch_decide ─────────────────────────────────────────────────────────

class TestDispatchDecide:
    @pytest.mark.asyncio
    async def test_returns_outcome_on_success(self) -> None:
        outcome_body = json.dumps({
            "proposal_id": "p-001", "ticket_id": "INC0000001",
            "outcome": "applied",
            "actor_user_id": "tech1@corp",
            "decided_at": datetime.now(UTC).isoformat(),
            "applied_fields": {"category": "network"},
        }).encode()
        nats = _ReplyingNats(reply=outcome_body)
        from oneops.use_cases.uc05_triage.contracts import Proposal
        proposal = Proposal.model_validate_json(_proposal_json())
        out = await dispatch_decide(
            nats=nats, proposal=proposal, proposal_id="p-001",
            choice="yes", actor_user_id="tech1@corp", final_values=None,
        )
        assert out.outcome == "applied"
        subj, _ = nats.request_calls[0]
        assert subj == "oneops.uc05.triage.decide"

    @pytest.mark.asyncio
    async def test_error_envelope_raises(self) -> None:
        nats = _ReplyingNats(
            reply=json.dumps({"error": "decide_failed",
                              "message": "ticket not found"}).encode())
        proposal = Proposal.model_validate_json(_proposal_json())
        with pytest.raises(RuntimeError, match="ticket not found"):
            await dispatch_decide(
                nats=nats, proposal=proposal, proposal_id="p-001",
                choice="yes", actor_user_id="tech1@corp", final_values=None,
            )
