"""Phase 4 tests — UC-5 triage agent (NATS subscriber + dispatcher)."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from oneops.use_cases.uc05_triage.agent import (
    QUEUE_GROUP,
    SUBJECT_APPLIED,
    SUBJECT_DECIDE,
    SUBJECT_PROPOSE,
    TriageAgent,
)
from oneops.use_cases.uc05_triage.contracts import (
    Proposal,
)

# ── Test doubles ────────────────────────────────────────────────────────────

class _FakeMsg:
    def __init__(self, data: bytes, reply: str = "") -> None:
        self.data = data
        self.reply = reply


class _FakeNats:
    """Records publishes + subscribes; returns canned replies for request()."""
    def __init__(self) -> None:
        self.subs: list[tuple[str, str, Any]] = []
        self.publishes: list[tuple[str, bytes]] = []

    async def subscribe(self, subject, *, handler, queue=""):
        self.subs.append((subject, queue, handler))
        return _StubSub()

    async def publish(self, subject: str, payload: bytes,
                       headers: dict | None = None) -> None:
        self.publishes.append((subject, payload))

    async def request(self, subject, payload, *, timeout=30, headers=None):
        # Not used in agent tests
        return b'{"ok": true}'


class _StubSub:
    async def unsubscribe(self) -> None: ...
    async def drain(self) -> None: ...


def _proposal(ticket_id: str = "INC0000001") -> Proposal:
    return Proposal(
        proposal_id="p-001",
        ticket_id=ticket_id,
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
        suggested_tags=["vpn", "tunnel", "gateway"],
        duplicate_verdict="none",
        overall_confidence_score=0.8,
        confidence_tier="propose",
        risk_class="medium",
        prioritization_basis={"impact": "llm_inferred"},
        assignment_basis="majority_of_top_k",
        assignment_confidence=0.8,
    )


# ── Subject vocabulary lock ─────────────────────────────────────────────────

class TestSubjects:
    def test_subjects_locked(self) -> None:
        assert SUBJECT_PROPOSE == "oneops.uc05.triage.propose"
        assert SUBJECT_DECIDE == "oneops.uc05.triage.decide"
        assert SUBJECT_APPLIED == "oneops.uc05.triage.applied"
        assert QUEUE_GROUP == "uc05-triage-workers"


# ── Agent startup ───────────────────────────────────────────────────────────

class TestStart:
    @pytest.mark.asyncio
    async def test_subscribes_to_both_subjects(self) -> None:
        nats = _FakeNats()

        async def runner(*, ticket_row, service_id, tenant_id):
            return _proposal()

        agent = TriageAgent(nats=nats, runner=runner, store=None)  # type: ignore[arg-type]
        await agent.start()
        subjects = sorted({s[0] for s in nats.subs})
        assert SUBJECT_PROPOSE in subjects
        assert SUBJECT_DECIDE in subjects
        # Both share the queue group
        assert all(s[1] == QUEUE_GROUP for s in nats.subs)

    @pytest.mark.asyncio
    async def test_stop_drains_subscriptions(self) -> None:
        nats = _FakeNats()

        async def runner(*, ticket_row, service_id, tenant_id):
            return _proposal()

        agent = TriageAgent(nats=nats, runner=runner, store=None)  # type: ignore[arg-type]
        await agent.start()
        await agent.stop()
        # _subs cleared after stop
        assert agent._subs == []


# ── _on_propose handler ─────────────────────────────────────────────────────

class TestOnPropose:
    @pytest.mark.asyncio
    async def test_runs_runner_and_replies_proposal_json(self) -> None:
        nats = _FakeNats()

        async def runner(*, ticket_row, service_id, tenant_id):
            assert tenant_id == "T001"
            assert service_id == "incident"
            return _proposal(ticket_id=ticket_row["incident_id"])

        agent = TriageAgent(nats=nats, runner=runner, store=None)  # type: ignore[arg-type]
        msg = _FakeMsg(
            data=json.dumps({
                "tenant_id": "T001", "service_id": "incident",
                "ticket_row": {"incident_id": "INC0000007",
                                "title": "X", "description": "Y"},
            }).encode(),
            reply="reply.inbox.1",
        )
        await agent._on_propose(msg)
        # One publish to the reply inbox
        assert len(nats.publishes) == 1
        subject, payload = nats.publishes[0]
        assert subject == "reply.inbox.1"
        body = json.loads(payload.decode())
        assert body["ticket_id"] == "INC0000007"
        assert body["mutation_intent"] == "recommend_only"

    @pytest.mark.asyncio
    async def test_runner_exception_returns_error_envelope(self) -> None:
        nats = _FakeNats()

        async def runner(*, ticket_row, service_id, tenant_id):
            raise RuntimeError("gateway 500")

        agent = TriageAgent(nats=nats, runner=runner, store=None)  # type: ignore[arg-type]
        msg = _FakeMsg(
            data=json.dumps({
                "tenant_id": "T001", "service_id": "incident",
                "ticket_row": {"incident_id": "X", "title": "x",
                                "description": "y"},
            }).encode(),
            reply="reply.inbox.2",
        )
        await agent._on_propose(msg)
        body = json.loads(nats.publishes[0][1].decode())
        assert body["error"] == "propose_failed"
        assert "gateway" in body["message"]

    @pytest.mark.asyncio
    async def test_no_reply_inbox_does_not_publish(self) -> None:
        nats = _FakeNats()

        async def runner(*, ticket_row, service_id, tenant_id):
            return _proposal()

        agent = TriageAgent(nats=nats, runner=runner, store=None)  # type: ignore[arg-type]
        msg = _FakeMsg(
            data=json.dumps({
                "tenant_id": "T001", "service_id": "incident",
                "ticket_row": {"incident_id": "X", "title": "x",
                                "description": "y"},
            }).encode(),
            reply="",
        )
        await agent._on_propose(msg)
        assert nats.publishes == []


# ── _on_decide handler ──────────────────────────────────────────────────────

class TestOnDecide:
    @pytest.mark.asyncio
    async def test_decide_yes_publishes_applied_event(self, tmp_path) -> None:
        # Real JsonFixtureStore so apply_triage_decision can write
        import json as _json

        from oneops.use_cases.uc05_triage.stores.json_store import JsonFixtureStore

        fx = tmp_path / "demo.json"
        fx.write_text(_json.dumps({
            "tenant_id": "T001",
            "incidents": [{
                "incident_id": "INC0000001",
                "title": "VPN", "description": "drops",
                "status": "new",
                "category": None, "subcategory": None, "service_name": None,
                "impact": None, "urgency": None, "priority": None,
                "assignment_group": None, "assigned_to": None,
                "ci_id": None, "triaged_at": None,
            }],
            "requests": [],
        }))
        store = JsonFixtureStore(fx)
        nats = _FakeNats()

        async def runner(*, ticket_row, service_id, tenant_id):
            return _proposal()

        agent = TriageAgent(nats=nats, runner=runner, store=store)
        proposal = _proposal()
        msg = _FakeMsg(
            data=json.dumps({
                "proposal_id": "p-001",
                "choice": "yes",
                "actor_user_id": "tech1@corp",
                "final_values": None,
                "proposal": _json.loads(proposal.model_dump_json()),
            }).encode(),
            reply="reply.inbox.3",
        )
        await agent._on_decide(msg)
        # Two publishes: applied broadcast (first) + reply (second)
        assert len(nats.publishes) == 2
        # Locate by subject — order is applied first, reply second
        by_subject = {s: p for s, p in nats.publishes}
        assert SUBJECT_APPLIED in by_subject
        assert "reply.inbox.3" in by_subject
        # Reply payload carries the Outcome
        reply_body = json.loads(by_subject["reply.inbox.3"].decode())
        assert reply_body["outcome"] == "applied"
        # Applied payload carries audit metadata
        applied = json.loads(by_subject[SUBJECT_APPLIED].decode())
        assert applied["ticket_id"] == "INC0000001"
        assert applied["actor_user_id"] == "tech1@corp"

    @pytest.mark.asyncio
    async def test_decide_no_does_not_publish_applied(self, tmp_path) -> None:
        import json as _json

        from oneops.use_cases.uc05_triage.stores.json_store import JsonFixtureStore

        fx = tmp_path / "demo.json"
        fx.write_text(_json.dumps({
            "tenant_id": "T001",
            "incidents": [{"incident_id": "INC0000001",
                            "title": "x", "description": "y",
                            "status": "new", "category": None,
                            "triaged_at": None}],
            "requests": [],
        }))
        store = JsonFixtureStore(fx)
        nats = _FakeNats()

        async def runner(**_):
            return _proposal()

        agent = TriageAgent(nats=nats, runner=runner, store=store)
        proposal = _proposal()
        msg = _FakeMsg(
            data=json.dumps({
                "proposal_id": "p-001",
                "choice": "no",
                "actor_user_id": "tech1@corp",
                "final_values": None,
                "proposal": _json.loads(proposal.model_dump_json()),
            }).encode(),
            reply="reply.inbox.4",
        )
        await agent._on_decide(msg)
        # Only the reply — NO applied broadcast
        applied_subjects = [s for s, _ in nats.publishes if s == SUBJECT_APPLIED]
        assert applied_subjects == []

    @pytest.mark.asyncio
    async def test_decide_apply_exception_returns_error_envelope(self) -> None:
        nats = _FakeNats()

        async def runner(**_): return _proposal()

        class _BrokenStore:
            async def get_ticket(self, **_): raise KeyError("missing")
            async def apply(self, **_): raise KeyError("missing")
            async def list_all(self, **_): return []

        agent = TriageAgent(nats=nats, runner=runner, store=_BrokenStore())  # type: ignore[arg-type]
        proposal = _proposal()
        import json as _json
        msg = _FakeMsg(
            data=json.dumps({
                "proposal_id": "p-001", "choice": "yes",
                "actor_user_id": "tech1@corp",
                "final_values": None,
                "proposal": _json.loads(proposal.model_dump_json()),
            }).encode(),
            reply="reply.inbox.5",
        )
        await agent._on_decide(msg)
        body = json.loads(nats.publishes[0][1].decode())
        assert body["error"] == "decide_failed"
