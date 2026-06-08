"""Section I assembly — combines Tool 1+2+3 outputs into a Proposal.

Computes the 5 derived fields (locked 2026-05-29):
  • overall_confidence_score = mean of per-field confidences
  • confidence_tier          = tier(overall) using triage_confidence_tiers block
  • risk_class               = deterministic priority -> risk map
  • mutation_intent          = "recommend_only" (constant)
  • (sla_due is computed in apply.py at write time, not at assembly)

Reads `triage_confidence_tiers` from registries/v2/platform/service-schema.json so
thresholds are tenant-tunable without code change.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from oneops.observability import span
from oneops.use_cases.uc05_triage.contracts import (
    AssignmentRecommendation,
    ConfidenceTier,
    DuplicateCheckResult,
    PrioritizationResult,
    Priority,
    Proposal,
    RiskClass,
)

_DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[4]
    / "registries" / "v2" / "platform" / "service-schema.json"
)

# Priority -> risk_class deterministic map (locked 2026-05-29).
_RISK_MAP: dict[str, RiskClass] = {
    "Low":    "low",
    "Medium": "low",
    "High":   "medium",
    "Urgent": "high",
}


def assemble_proposal(
    *,
    ticket_id: str,
    service_id: str,
    tenant_id: str,
    duplicate: DuplicateCheckResult,
    assignment: AssignmentRecommendation,
    prioritization: PrioritizationResult,
    schema_path: Path | None = None,
) -> Proposal:
    """Build the Proposal that the proposal card renders."""
    with span("uc05.assembly",
              **{"oneops.tenant_id": tenant_id,
                 "uc05.service_id": service_id,
                 "uc05.ticket_id": ticket_id}) as _sp:
        confidences = _collect_confidences(duplicate, assignment, prioritization)
        overall = round(mean(confidences), 4) if confidences else 0.0

        tier = _tier_for(overall, schema_path)
        risk = _RISK_MAP.get(prioritization.priority, "low")
        try:
            _sp.set_attribute("uc05.confidence_tier", tier)
            _sp.set_attribute("uc05.risk_class", risk)
            _sp.set_attribute("uc05.overall_confidence", overall)
        except Exception:
            pass

        return _build_proposal(
            ticket_id=ticket_id, service_id=service_id, tenant_id=tenant_id,
            duplicate=duplicate, assignment=assignment, prioritization=prioritization,
            overall=overall, tier=tier, risk=risk,
        )


def _build_proposal(*, ticket_id, service_id, tenant_id, duplicate,
                     assignment, prioritization, overall, tier, risk) -> Proposal:
    return Proposal(
        proposal_id=f"p-{uuid.uuid4().hex[:16]}",
        ticket_id=ticket_id,
        service_id=service_id,  # type: ignore[arg-type]
        tenant_id=tenant_id,
        created_at=datetime.now(UTC),
        suggested_category=duplicate.suggested_category,
        suggested_subcategory=duplicate.suggested_subcategory,
        suggested_assigned_to=duplicate.suggested_assigned_to,
        suggested_ci_id=duplicate.suggested_ci_id,
        suggested_impact=prioritization.impact,
        suggested_urgency=prioritization.urgency,
        suggested_priority=prioritization.priority,
        suggested_assignment_group=assignment.assignment_group,
        suggested_tags=duplicate.suggested_tags,
        duplicate_verdict=duplicate.duplicate_verdict,
        top_duplicate_id=duplicate.top_match.id if duplicate.top_match else None,
        top_duplicate_score=duplicate.top_match.fused_score if duplicate.top_match else None,
        overall_confidence_score=overall,
        confidence_tier=tier,
        risk_class=risk,
        mutation_intent="recommend_only",
        prioritization_basis=prioritization.basis,
        assignment_basis=assignment.basis,
        assignment_confidence=assignment.confidence,
    )


def derive_risk_class(priority: Priority) -> RiskClass:
    """Public helper — deterministic priority -> risk_class map."""
    return _RISK_MAP.get(priority, "low")


# ── Internals ────────────────────────────────────────────────────────────────

def _collect_confidences(
    duplicate: DuplicateCheckResult,
    assignment: AssignmentRecommendation,
    prioritization: PrioritizationResult,
) -> list[float]:
    out: list[float] = []
    # Field suggestions from Tool 1 (per-field confidences)
    for fs in duplicate.field_suggestions.values():
        if fs.value is not None:
            out.append(fs.confidence)
    # Assignment confidence (Tool 2) — only count when we have a value
    if assignment.assignment_group is not None:
        out.append(assignment.confidence)
    # Priority confidence (Tool 3): LLM-inferred for incident, deterministic
    # for request. Map basis to a confidence proxy.
    pri_basis = prioritization.basis.get("priority", "")
    if pri_basis.startswith("matrix["):
        out.append(1.0)  # matrix lookup is deterministic when impact/urgency known
    return out


def _tier_for(overall: float, schema_path: Path | None) -> ConfidenceTier:
    cfg = _load_tier_cfg(schema_path)
    if overall >= cfg["auto_apply_at"]:
        return "auto"
    if overall >= cfg["propose_at"]:
        return "propose"
    return "refuse"


def _load_tier_cfg(schema_path: Path | None) -> dict[str, float]:
    path = schema_path or _DEFAULT_SCHEMA_PATH
    data = json.loads(Path(path).read_text())
    block = data.get("triage_confidence_tiers", {})
    return {
        "auto_apply_at": float(block.get("auto_apply_at", 0.90)),
        "propose_at":    float(block.get("propose_at",    0.50)),
        "refuse_below":  float(block.get("refuse_below",  0.50)),
    }
