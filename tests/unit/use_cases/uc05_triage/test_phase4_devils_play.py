"""Phase 4 devil's-play — NATS resilience + traceparent propagation."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from oneops.errors import NATSUnavailableError
from oneops.use_cases.uc05_triage.agent import SUBJECT_APPLIED, TriageAgent
from oneops.use_cases.uc05_triage.contracts import Proposal
from oneops.use_cases.uc05_triage.nats_dispatcher import dispatch_propose
from oneops.use_cases.uc05_triage.traceparent import (
    extract_from_headers,
    parse_traceparent,
)


def _proposal() -> Proposal:
    return Proposal(
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


# ── Probe 1: NATS down → API surfaces NATSUnavailableError ─────────────────

class TestProbeNatsDown:
    @pytest.mark.asyncio
    async def test_dispatch_propose_propagates_when_nats_down(self) -> None:
        class _Down:
            async def request(self, *a, **k):
                raise NATSUnavailableError("broker unreachable")

        with pytest.raises(NATSUnavailableError):
            await dispatch_propose(
                nats=_Down(),  # type: ignore[arg-type]
                tenant_id="T001", service_id="incident",
                ticket_row={"incident_id": "X", "title": "x",
                            "description": "y"},
            )


# ── Probe 2: traceparent valid round-trip ──────────────────────────────────

class TestProbeTraceparentRoundtrip:
    def test_parse_then_format_matches_input(self) -> None:
        tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        out = parse_traceparent(tp)
        assert out is not None
        # Round-trip preserves trace_id + span_id + flags
        trace_id, span_id, flags = out
        assert trace_id == "0af7651916cd43dd8448eb211c80319c"
        assert span_id == "b7ad6b7169203331"
        assert flags == 1


# ── Probe 3: traceparent extracted from NATS headers ──────────────────────

class TestProbeTraceparentFromNatsHeaders:
    def test_extracted_from_dict(self) -> None:
        headers = {
            "traceparent":
                "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
            "x-tenant-id": "T001",
        }
        tp = extract_from_headers(headers)
        assert tp is not None
        assert parse_traceparent(tp) is not None

    def test_missing_returns_none(self) -> None:
        assert extract_from_headers({"x-tenant-id": "T001"}) is None


# ── Probe 4: runner exception → agent replies with error envelope ──────────

class TestProbeRunnerException:
    @pytest.mark.asyncio
    async def test_propose_handler_returns_error_envelope_not_crash(self) -> None:
        class _Nats:
            def __init__(self): self.published = []
            async def subscribe(self, *a, **k): return None
            async def publish(self, subject, payload, headers=None):
                self.published.append((subject, payload))

        async def broken_runner(**_):
            raise RuntimeError("simulated crash")

        nats = _Nats()
        agent = TriageAgent(nats=nats, runner=broken_runner, store=None)  # type: ignore[arg-type]

        class _Msg:
            data = json.dumps({
                "tenant_id": "T001", "service_id": "incident",
                "ticket_row": {"incident_id": "X", "title": "x",
                                "description": "y"},
            }).encode()
            reply = "reply.inbox.x"

        await agent._on_propose(_Msg())
        # Crash did NOT propagate; an error envelope was published instead
        assert len(nats.published) == 1
        body = json.loads(nats.published[0][1].decode())
        assert body["error"] == "propose_failed"


# ── Probe 5: decide → No → no applied broadcast ─────────────────────────────

class TestProbeDecideNoNoApplied:
    @pytest.mark.asyncio
    async def test_no_choice_does_not_publish_applied(self, tmp_path) -> None:
        from oneops.use_cases.uc05_triage.stores.json_store import JsonFixtureStore

        fx = tmp_path / "demo.json"
        fx.write_text(json.dumps({
            "tenant_id": "T001",
            "incidents": [{"incident_id": "INC0000001", "title": "x",
                            "description": "y", "status": "new",
                            "category": None, "triaged_at": None}],
            "requests": [],
        }))
        store = JsonFixtureStore(fx)

        class _Nats:
            def __init__(self): self.published = []
            async def subscribe(self, *a, **k): return None
            async def publish(self, subject, payload, headers=None):
                self.published.append((subject, payload))

        async def runner(**_): return _proposal()

        nats = _Nats()
        agent = TriageAgent(nats=nats, runner=runner, store=store)
        proposal = _proposal()

        class _Msg:
            data = json.dumps({
                "proposal_id": "p-001", "choice": "no",
                "actor_user_id": "tech1@corp",
                "final_values": None,
                "proposal": json.loads(proposal.model_dump_json()),
            }).encode()
            reply = "reply.inbox.no"

        await agent._on_decide(_Msg())
        applied_subjects = [s for s, _ in nats.published if s == SUBJECT_APPLIED]
        assert applied_subjects == []
