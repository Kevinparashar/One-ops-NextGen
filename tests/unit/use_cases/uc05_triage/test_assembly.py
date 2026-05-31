"""Unit tests for Section I assembly node.

Covers the 5 derived fields:
  • overall_confidence_score
  • confidence_tier
  • risk_class
  • mutation_intent (constant)
  • (sla_due is in apply.py — see test_apply.py)
"""
from __future__ import annotations

import pytest

from oneops.use_cases.uc05_triage.assembly import (
    assemble_proposal,
    derive_risk_class,
)
from oneops.use_cases.uc05_triage.contracts import (
    AssignmentRecommendation,
    DuplicateCheckResult,
    FieldSuggestion,
    PrioritizationResult,
    ScoredNeighbour,
)


def _dup(field_confidences: dict[str, float] | None = None,
         top_match: bool = True) -> DuplicateCheckResult:
    fs = {
        col: FieldSuggestion(value=val, confidence=conf, coverage=1.0,
                              diversity=1, basis_ids=[], basis="majority_of_top_k",
                              rationale=f"{val} via majority")
        for col, (val, conf) in (field_confidences or {}).items()
    }
    tm = ScoredNeighbour(id="INC0001", fields={}, vec_score=0.9,
                         fts_score=1.0, fused_score=0.89) if top_match else None
    return DuplicateCheckResult(
        candidates=[tm] if tm else [],
        top_match=tm,
        duplicate_verdict="duplicate" if tm else "none",
        duplicate_threshold=0.85,
        suggested_category=field_confidences.get("category", (None, 0))[0] if field_confidences else None,
        suggested_subcategory=field_confidences.get("subcategory", (None, 0))[0] if field_confidences else None,
        suggested_assigned_to=field_confidences.get("assigned_to", (None, 0))[0] if field_confidences else None,
        suggested_ci_id=field_confidences.get("ci_id", (None, 0))[0] if field_confidences else None,
        field_suggestions=fs,
    )


def _asn(group: str = "Network-L2", conf: float = 0.8) -> AssignmentRecommendation:
    return AssignmentRecommendation(
        assignment_group=group, confidence=conf, coverage=1.0,
        diversity=1, basis_ids=["A"], basis="majority_of_top_k",
        rationale="majority",
    )


def _pri(impact: str = "On Department", urgency: str = "High",
         priority: str = "High") -> PrioritizationResult:
    return PrioritizationResult(
        impact=impact, urgency=urgency, priority=priority,
        basis={"impact": "llm_inferred", "urgency": "llm_inferred",
               "priority": f"matrix[{impact}][{urgency}]"},
    )


class TestRiskClass:
    @pytest.mark.parametrize(("priority", "expected"), [
        ("Low",    "low"),
        ("Medium", "low"),
        ("High",   "medium"),
        ("Urgent", "high"),
    ])
    def test_each_priority_maps(self, priority, expected) -> None:
        assert derive_risk_class(priority) == expected  # type: ignore[arg-type]


class TestOverallConfidence:
    def test_mean_of_per_field_confidences(self) -> None:
        d = _dup({"category": ("network", 1.0), "subcategory": ("vpn", 0.75),
                  "assigned_to": ("USR00003", 1.0), "ci_id": ("CI001", 0.75)})
        p = assemble_proposal(
            ticket_id="X", service_id="incident", tenant_id="T001",
            duplicate=d, assignment=_asn(conf=0.8), prioritization=_pri(),
        )
        # 4 field confs (1.0, 0.75, 1.0, 0.75) + assignment 0.8 + matrix 1.0 = 6 values
        # mean = (1.0 + 0.75 + 1.0 + 0.75 + 0.8 + 1.0) / 6 = 5.3 / 6 ≈ 0.883
        assert p.overall_confidence_score == pytest.approx(0.883, abs=0.01)

    def test_confidence_clamped_to_unit_interval(self) -> None:
        d = _dup({"category": ("network", 1.0)})
        p = assemble_proposal(
            ticket_id="X", service_id="incident", tenant_id="T001",
            duplicate=d, assignment=_asn(), prioritization=_pri(),
        )
        assert 0.0 <= p.overall_confidence_score <= 1.0


class TestConfidenceTier:
    def test_high_confidence_yields_auto(self) -> None:
        # All confidences at 1.0 → mean = 1.0 → auto (>= 0.90)
        d = _dup({"category": ("network", 1.0), "subcategory": ("vpn", 1.0)})
        p = assemble_proposal(
            ticket_id="X", service_id="incident", tenant_id="T001",
            duplicate=d, assignment=_asn(conf=1.0), prioritization=_pri(),
        )
        assert p.confidence_tier == "auto"

    def test_mid_confidence_yields_propose(self) -> None:
        # Mean around 0.6 → propose (>= 0.50, < 0.90)
        d = _dup({"category": ("network", 0.6), "subcategory": ("vpn", 0.5)})
        p = assemble_proposal(
            ticket_id="X", service_id="incident", tenant_id="T001",
            duplicate=d, assignment=_asn(conf=0.5), prioritization=_pri(),
        )
        assert p.confidence_tier == "propose"

    def test_low_confidence_yields_refuse(self) -> None:
        d = _dup({"category": ("network", 0.2), "subcategory": ("vpn", 0.2)})
        p = assemble_proposal(
            ticket_id="X", service_id="incident", tenant_id="T001",
            duplicate=d, assignment=_asn(conf=0.2), prioritization=_pri(),
        )
        # Mean around 0.4 → below 0.50 → refuse
        assert p.confidence_tier == "refuse"


class TestMutationIntent:
    def test_always_recommend_only(self) -> None:
        d = _dup({"category": ("network", 0.8)})
        p = assemble_proposal(
            ticket_id="X", service_id="incident", tenant_id="T001",
            duplicate=d, assignment=_asn(), prioritization=_pri(),
        )
        assert p.mutation_intent == "recommend_only"


class TestProposalShape:
    def test_all_required_fields_present(self) -> None:
        d = _dup({"category": ("network", 1.0), "subcategory": ("vpn", 1.0)})
        d_with_tags = d.model_copy(update={"suggested_tags": ["vpn", "tunnel", "wi-fi"]})
        p = assemble_proposal(
            ticket_id="INC0000001", service_id="incident", tenant_id="T001",
            duplicate=d_with_tags, assignment=_asn(), prioritization=_pri(),
        )
        # Every spec field must be set
        assert p.proposal_id.startswith("p-")
        assert p.suggested_tags == ["vpn", "tunnel", "wi-fi"]
        assert p.suggested_impact == "On Department"
        assert p.suggested_priority == "High"
        assert p.risk_class == "medium"  # High -> medium
        assert p.mutation_intent == "recommend_only"
        assert isinstance(p.overall_confidence_score, float)
        assert p.confidence_tier in {"auto", "propose", "refuse"}
