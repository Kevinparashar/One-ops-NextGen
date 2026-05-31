"""Unit tests for apply.py — Yes/No paths + SLA clock + technician edits."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oneops.use_cases.uc05_triage.apply import (
    _compute_sla_due,
    _parse_duration,
    apply_triage_decision,
)
from oneops.use_cases.uc05_triage.contracts import Proposal, TriageDecision
from oneops.use_cases.uc05_triage.stores.json_store import JsonFixtureStore


def _make_fixture(tmp: Path) -> Path:
    p = tmp / "demo.json"
    p.write_text(json.dumps({
        "tenant_id": "T001",
        "incidents": [{
            "incident_id": "INC0000001",
            "title": "VPN drops at Mumbai",
            "description": "...",
            "status": "new",
            "category": None, "subcategory": None,
            "impact": None, "urgency": None, "priority": None,
            "assignment_group": None, "assigned_to": None,
            "ci_id": None, "triaged_at": None,
        }],
        "requests": [],
    }))
    return p


def _proposal(priority: str = "High") -> Proposal:
    return Proposal(
        proposal_id="p-001",
        ticket_id="INC0000001",
        service_id="incident",
        tenant_id="T001",
        created_at=datetime(2026, 5, 29, 18, 0, 0, tzinfo=UTC),
        suggested_category="network",
        suggested_subcategory="vpn",
        suggested_assigned_to="USR00003",
        suggested_ci_id="CI0000001",
        suggested_impact="On Department",
        suggested_urgency="High",
        suggested_priority=priority,  # type: ignore[arg-type]
        suggested_assignment_group="Network-L2",
        suggested_tags=["vpn", "tunnel", "wi-fi"],
        duplicate_verdict="duplicate",
        top_duplicate_id="INC0001001",
        top_duplicate_score=0.89,
        overall_confidence_score=0.85,
        confidence_tier="propose",
        risk_class="medium",
        prioritization_basis={"impact": "llm_inferred", "urgency": "llm_inferred",
                              "priority": "matrix[On Department][High]"},
        assignment_basis="majority_of_top_k",
        assignment_confidence=0.8,
    )


# ── SLA duration parsing ─────────────────────────────────────────────────────

class TestParseDuration:
    @pytest.mark.parametrize(("s", "expected"), [
        ("8h",    timedelta(hours=8)),
        ("4h",    timedelta(hours=4)),
        ("2h",    timedelta(hours=2)),
        ("30min", timedelta(minutes=30)),
        ("90sec", timedelta(seconds=90)),
        ("1d",    timedelta(days=1)),
    ])
    def test_each_format(self, s, expected) -> None:
        assert _parse_duration(s) == expected

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_duration("yesterday")


class TestComputeSlaDue:
    def test_uses_priority_map(self) -> None:
        # priority='Urgent' → '30min'
        now = datetime(2026, 5, 29, 18, 0, 0, tzinfo=UTC)
        due = _compute_sla_due("Urgent", now)
        assert due == now + timedelta(minutes=30)

    def test_unknown_priority_falls_back_to_8h(self) -> None:
        now = datetime(2026, 5, 29, 18, 0, 0, tzinfo=UTC)
        due = _compute_sla_due("Critical", now)  # not in the map
        assert due == now + timedelta(hours=8)


# ── apply_triage_decision: NO path ───────────────────────────────────────────

class TestApplyNo:
    @pytest.mark.asyncio
    async def test_no_returns_discarded_without_writing(self, tmp_path: Path) -> None:
        p = _make_fixture(tmp_path)
        store = JsonFixtureStore(p)
        decision = TriageDecision(proposal_id="p-001", choice="no",
                                  actor_user_id="tech1")
        outcome = await apply_triage_decision(
            proposal=_proposal(), decision=decision,
            final_values=None, store=store,
        )
        assert outcome.outcome == "discarded"
        assert outcome.applied_fields is None
        # JSON file untouched
        data = json.loads(p.read_text())
        assert data["incidents"][0]["category"] is None
        assert data["incidents"][0]["triaged_at"] is None


# ── apply_triage_decision: YES path ──────────────────────────────────────────

class TestApplyYes:
    @pytest.mark.asyncio
    async def test_yes_writes_ai_suggestions_when_no_edits(self, tmp_path: Path) -> None:
        p = _make_fixture(tmp_path)
        store = JsonFixtureStore(p)
        decision = TriageDecision(proposal_id="p-001", choice="yes",
                                  actor_user_id="tech1")
        outcome = await apply_triage_decision(
            proposal=_proposal(), decision=decision,
            final_values=None, store=store,
        )
        assert outcome.outcome == "applied"
        assert outcome.applied_fields["category"] == "network"
        assert outcome.applied_fields["priority"] == "High"
        # JSON row updated
        data = json.loads(p.read_text())
        row = data["incidents"][0]
        assert row["category"] == "network"
        assert row["assignment_group"] == "Network-L2"
        assert row["assigned_to"] == "USR00003"
        assert row["status"] == "assigned"

    @pytest.mark.asyncio
    async def test_yes_overrides_with_technician_edits(self, tmp_path: Path) -> None:
        """Rule D — technician edits override AI suggestions."""
        p = _make_fixture(tmp_path)
        store = JsonFixtureStore(p)
        decision = TriageDecision(proposal_id="p-001", choice="yes",
                                  actor_user_id="tech1")
        outcome = await apply_triage_decision(
            proposal=_proposal(),
            decision=decision,
            final_values={"category": "email",     # changed
                          "priority": "Medium",     # changed
                          "assignment_group": "Email-Support"},  # changed
            store=store,
        )
        # Edited fields win; untouched fields stay AI suggestion
        assert outcome.applied_fields["category"] == "email"
        assert outcome.applied_fields["priority"] == "Medium"
        assert outcome.applied_fields["assignment_group"] == "Email-Support"
        assert outcome.applied_fields["subcategory"] == "vpn"  # untouched

    @pytest.mark.asyncio
    async def test_yes_sets_sla_due_from_final_priority(self, tmp_path: Path) -> None:
        p = _make_fixture(tmp_path)
        store = JsonFixtureStore(p)
        decision = TriageDecision(proposal_id="p-001", choice="yes",
                                  actor_user_id="tech1")
        now = datetime(2026, 5, 29, 18, 0, 0, tzinfo=UTC)
        # Proposal suggested High; technician edits to Urgent
        outcome = await apply_triage_decision(
            proposal=_proposal(priority="High"),
            decision=decision,
            final_values={"priority": "Urgent"},
            store=store, now=now,
        )
        # SLA should come from the FINAL priority (Urgent → 30min), not AI's High
        expected = (now + timedelta(minutes=30)).isoformat()
        assert outcome.applied_fields["sla_due"] == expected
