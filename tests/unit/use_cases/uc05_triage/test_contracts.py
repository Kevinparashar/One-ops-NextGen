"""Boundary tests for UC-5 Pydantic contracts.

Each Literal-typed field is tested both happy-path and adversarially —
typos / off-by-one / wrong service_id must crash at the boundary so they
never reach the graph (rule §2.7 no silent failures).
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from oneops.use_cases.uc05_triage.contracts import (
    AssignmentRecommendation,
    DuplicateCheckResult,
    Outcome,
    PrioritizationResult,
    Proposal,
    ProposeRequest,
    ScoredNeighbour,
    TriageDecision,
)

# ── ProposeRequest ────────────────────────────────────────────────────────────

class TestProposeRequest:
    def test_minimal_incident(self) -> None:
        r = ProposeRequest(ticket_id="INC0001175", service_id="incident", tenant_id="t1")
        assert r.duplicate_threshold == 0.85
        assert r.max_candidates == 10

    def test_minimal_request(self) -> None:
        r = ProposeRequest(ticket_id="SR0002001", service_id="request", tenant_id="t1")
        assert r.service_id == "request"

    def test_unknown_service_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProposeRequest(ticket_id="X1", service_id="problem", tenant_id="t1")  # type: ignore[arg-type]

    def test_threshold_floor(self) -> None:
        with pytest.raises(ValidationError):
            ProposeRequest(ticket_id="X1", service_id="incident", tenant_id="t1",
                           duplicate_threshold=0.4)

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProposeRequest(ticket_id="X1", service_id="incident", tenant_id="t1",
                           secret="hack")  # type: ignore[call-arg]

    def test_empty_ticket_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProposeRequest(ticket_id="", service_id="incident", tenant_id="t1")


# ── TriageDecision ────────────────────────────────────────────────────────────

class TestTriageDecision:
    def test_yes(self) -> None:
        d = TriageDecision(proposal_id="p1", choice="yes", actor_user_id="tech1")
        assert d.choice == "yes"

    def test_no(self) -> None:
        d = TriageDecision(proposal_id="p1", choice="no", actor_user_id="tech1")
        assert d.choice == "no"

    def test_maybe_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TriageDecision(proposal_id="p1", choice="maybe", actor_user_id="tech1")  # type: ignore[arg-type]

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TriageDecision(proposal_id="p1", choice="", actor_user_id="tech1")  # type: ignore[arg-type]

    def test_missing_actor_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TriageDecision(proposal_id="p1", choice="yes")  # type: ignore[call-arg]


# ── ScoredNeighbour ───────────────────────────────────────────────────────────

class TestScoredNeighbour:
    def test_typical(self) -> None:
        n = ScoredNeighbour(
            id="INC0001001",
            fields={"title": "VPN dropping", "category": "network"},
            vec_score=0.87,
            fts_score=2.1,
            fused_score=0.83,
        )
        assert n.fields["category"] == "network"

    def test_dynamic_fields(self) -> None:
        # Schema-driven: any column name is allowed in `fields`.
        n = ScoredNeighbour(
            id="INC0001001",
            fields={"future_field_we_dont_know_yet": "value"},
            vec_score=0.5,
            fts_score=0.5,
            fused_score=0.5,
        )
        assert "future_field_we_dont_know_yet" in n.fields

    def test_vec_score_clamped(self) -> None:
        with pytest.raises(ValidationError):
            ScoredNeighbour(id="X", fields={}, vec_score=1.5, fts_score=0, fused_score=0)


# ── DuplicateCheckResult ──────────────────────────────────────────────────────

class TestDuplicateCheckResult:
    def _neighbour(self, idx: int = 1, vec: float = 0.7) -> ScoredNeighbour:
        return ScoredNeighbour(id=f"INC000{idx}", fields={}, vec_score=vec,
                               fts_score=1.0, fused_score=vec)

    def test_duplicate_path(self) -> None:
        top = self._neighbour(1, 0.91)
        r = DuplicateCheckResult(
            candidates=[top, self._neighbour(2)],
            top_match=top,
            duplicate_verdict="duplicate",
            duplicate_threshold=0.85,
            suggested_category="network",
            suggested_subcategory="vpn",
            suggested_assigned_to="rohan.kapoor",
            suggested_ci_id="CI0000001",
        )
        assert r.duplicate_verdict == "duplicate"
        assert r.top_match.id == "INC0001"
        assert r.suggested_assigned_to == "rohan.kapoor"
        assert r.suggested_ci_id == "CI0000001"

    def test_none_path(self) -> None:
        r = DuplicateCheckResult(
            candidates=[self._neighbour(1, 0.4)],
            top_match=None,
            duplicate_verdict="none",
            duplicate_threshold=0.85,
            suggested_category="network",
        )
        assert r.top_match is None
        assert r.suggested_subcategory is None

    def test_invalid_verdict(self) -> None:
        with pytest.raises(ValidationError):
            DuplicateCheckResult(
                candidates=[],
                top_match=None,
                duplicate_verdict="maybe",  # type: ignore[arg-type]
                duplicate_threshold=0.85,
            )


# ── AssignmentRecommendation ─────────────────────────────────────────────────

class TestAssignmentRecommendation:
    def test_high_confidence(self) -> None:
        r = AssignmentRecommendation(
            assignment_group="Network-L2",
            confidence=1.0,
            basis="majority_of_top_k",
        )
        assert r.assignment_group == "Network-L2"

    def test_below_floor(self) -> None:
        r = AssignmentRecommendation(
            assignment_group=None,
            confidence=0.2,
            basis="below_confidence_floor",
        )
        assert r.assignment_group is None

    def test_empty(self) -> None:
        r = AssignmentRecommendation(
            assignment_group=None,
            confidence=0.0,
            basis="empty_neighbours",
        )
        assert r.confidence == 0.0

    def test_invalid_basis(self) -> None:
        with pytest.raises(ValidationError):
            AssignmentRecommendation(
                assignment_group="X",
                confidence=1.0,
                basis="vibes",  # type: ignore[arg-type]
            )


# ── PrioritizationResult ─────────────────────────────────────────────────────

class TestPrioritizationResult:
    def test_typical(self) -> None:
        r = PrioritizationResult(
            impact="On Department",
            urgency="High",
            priority="High",
            basis={"impact": "llm", "urgency": "llm", "priority": "matrix[OnDept][High]"},
        )
        assert r.priority == "High"

    def test_invalid_impact(self) -> None:
        with pytest.raises(ValidationError):
            PrioritizationResult(
                impact="Catastrophic",  # type: ignore[arg-type]
                urgency="High",
                priority="High",
                basis={},
            )

    def test_invalid_urgency_label_case(self) -> None:
        # Motadata vocabulary is case-sensitive: "high" != "High"
        with pytest.raises(ValidationError):
            PrioritizationResult(
                impact="On Users",
                urgency="high",  # type: ignore[arg-type]
                priority="High",
                basis={},
            )


# ── Proposal ─────────────────────────────────────────────────────────────────

class TestProposal:
    def _now(self) -> datetime:
        return datetime(2026, 5, 29, 15, 0, 0, tzinfo=UTC)

    def test_incident_full(self) -> None:
        p = Proposal(
            proposal_id="p-001",
            ticket_id="INC0001175",
            service_id="incident",
            tenant_id="t1",
            created_at=self._now(),
            suggested_category="network",
            suggested_subcategory="vpn",
            suggested_assigned_to="USR00003",
            suggested_ci_id="CI0000001",
            suggested_impact="On Department",
            suggested_urgency="High",
            suggested_priority="High",
            suggested_assignment_group="Network-L2",
            suggested_tags=["vpn", "tunnel", "wi-fi"],
            duplicate_verdict="duplicate",
            top_duplicate_id="INC0001001",
            top_duplicate_score=0.84,
            overall_confidence_score=0.85,
            confidence_tier="propose",
            risk_class="medium",
            prioritization_basis={"impact": "llm"},
            assignment_basis="majority_of_top_k",
            assignment_confidence=0.8,
        )
        assert p.suggested_subcategory == "vpn"
        assert p.top_duplicate_id == "INC0001001"
        assert p.suggested_assigned_to == "USR00003"
        assert p.risk_class == "medium"
        assert p.mutation_intent == "recommend_only"

    def test_request_shape(self) -> None:
        # Request: no subcategory; has assigned_to + ci_id per Section I spec
        p = Proposal(
            proposal_id="p-002",
            ticket_id="SR0002001",
            service_id="request",
            tenant_id="t1",
            created_at=self._now(),
            suggested_category="hardware",
            suggested_assigned_to="USR00009",
            suggested_impact="On Users",
            suggested_urgency="Medium",
            suggested_priority="Medium",
            suggested_assignment_group="Hardware-Fulfilment",
            duplicate_verdict="none",
            overall_confidence_score=0.62,
            confidence_tier="propose",
            risk_class="low",
            prioritization_basis={"impact": "catalog_map"},
            assignment_basis="majority_of_top_k",
            assignment_confidence=0.6,
        )
        assert p.suggested_subcategory is None
        assert p.suggested_assigned_to == "USR00009"
        assert p.mutation_intent == "recommend_only"
        # Dropped fields are not on the model anymore — must not be accessible
        assert not hasattr(p, "suggested_service_name")
        assert not hasattr(p, "suggested_catalog_item_id")

    def test_unknown_service_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Proposal(
                proposal_id="p-003",
                ticket_id="X",
                service_id="problem",  # type: ignore[arg-type]
                tenant_id="t1",
                created_at=self._now(),
                suggested_impact="Low", suggested_urgency="Low", suggested_priority="Low",
                duplicate_verdict="none",
                overall_confidence_score=0.0,
                confidence_tier="refuse",
                risk_class="low",
                prioritization_basis={}, assignment_basis="empty_neighbours",
                assignment_confidence=0.0,
            )


# ── Outcome ───────────────────────────────────────────────────────────────────

class TestOutcome:
    def _now(self) -> datetime:
        return datetime(2026, 5, 29, 15, 0, 0, tzinfo=UTC)

    def test_applied(self) -> None:
        o = Outcome(
            proposal_id="p-001",
            ticket_id="INC0001175",
            outcome="applied",
            actor_user_id="tech1",
            decided_at=self._now(),
            applied_fields={"category": "network", "priority": "High"},
        )
        assert o.outcome == "applied"
        assert o.applied_fields["priority"] == "High"

    def test_discarded(self) -> None:
        o = Outcome(
            proposal_id="p-001",
            ticket_id="INC0001175",
            outcome="discarded",
            actor_user_id="tech1",
            decided_at=self._now(),
        )
        assert o.applied_fields is None

    def test_invalid_outcome(self) -> None:
        with pytest.raises(ValidationError):
            Outcome(
                proposal_id="p-001",
                ticket_id="INC0001175",
                outcome="maybe",  # type: ignore[arg-type]
                actor_user_id="tech1",
                decided_at=self._now(),
            )
