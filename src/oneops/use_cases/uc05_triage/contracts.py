"""Pydantic contracts for UC-5 Triage.

Strict typing at every API and tool boundary (rule §2.7 no silent failures).
Literal types lock the small enumerations so a typo crashes at the boundary,
never reaches the graph.

Shapes flow:
  ProposeRequest        → graph entry
  ScoredNeighbour[]     → produced by retrieval engine, consumed by Tool 1/2
  DuplicateCheckResult  → Tool 1 output (also carries field aggregations)
  AssignmentRecommendation → Tool 2 output
  PrioritizationResult  → Tool 3 output
  Proposal              → assembled at graph fan-in, sent to frontend
  TriageDecision        → frontend reply (yes/no), graph resumes
  Outcome               → apply.py return value
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ── Enumerated values (Motadata + UC-5 contract) ──────────────────────────────

ServiceId = Literal["incident", "request"]
"""UC-5 covers two ticket types only. Enforced at API boundary."""

Impact = Literal["Low", "On Users", "On Department", "On Business"]
"""Motadata Priority Matrix impact axis. Verbatim from service-schema.json."""

Urgency = Literal["Low", "Medium", "High", "Urgent"]
"""Motadata Priority Matrix urgency axis."""

Priority = Literal["Low", "Medium", "High", "Urgent"]
"""Derived from impact x urgency via the Motadata 4x4 matrix lookup."""

DuplicateVerdict = Literal["duplicate", "none"]
"""Tool 1 emits 'duplicate' only when top match score >= threshold (default 0.85)."""

DecisionChoice = Literal["yes", "no"]
"""Binary approval — no third option, no free text. The frontend renders two buttons."""

OutcomeStatus = Literal["applied", "discarded"]


# ── Input contracts ───────────────────────────────────────────────────────────

class ProposeRequest(BaseModel):
    """Frontend → API. Identifies which existing ticket to triage.

    UC-5 operates on tickets that already exist in itsm.incident or
    itsm.request (Stage 1 portal flow filed them). The server fetches title,
    description, embedding, and any aggregation-relevant fields from the
    row — the technician never types them.
    """

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1, max_length=64)
    service_id: ServiceId
    tenant_id: str = Field(min_length=1, max_length=64)
    duplicate_threshold: float = Field(default=0.85, ge=0.5, le=1.0)
    max_candidates: int = Field(default=10, ge=1, le=50)


class TriageDecision(BaseModel):
    """Frontend → API. Yes/No approval for a proposal."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=64)
    choice: DecisionChoice
    actor_user_id: str = Field(min_length=1, max_length=128)
    """Required for audit — who clicked the button."""


# ── Retrieval contracts ───────────────────────────────────────────────────────

class ScoredNeighbour(BaseModel):
    """One row from the retrieval engine.

    `fields` is intentionally a free dict so the engine remains schema-driven:
    columns come from service-schema.json's retrieval_schema.neighbour_columns
    and may evolve without changing this model. Consumers access fields with
    `.get()` defensively — same pattern as the embedding-input builder.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    fields: dict[str, Any]
    vec_score: float = Field(ge=0.0, le=1.0)
    fts_score: float = Field(ge=0.0)
    fused_score: float = Field(ge=0.0, le=1.0)


# ── Tool outputs ──────────────────────────────────────────────────────────────

SuggestionBasis = Literal[
    "majority_of_top_k",
    "llm_tiebreak",
    "llm_propose",
    "below_coverage",
    "empty_neighbours",
    "below_confidence_floor",
]


class FieldSuggestion(BaseModel):
    """Per-field prediction with full provenance (Bundle A + LLM tiebreak).

    `confidence` = vote_fraction = winner_votes / total_votes_for_this_field
    `coverage`   = how many of top-K had a non-null value for this field
    `diversity`  = distinct non-null values seen in top-K
    `basis_ids`  = the neighbour IDs whose vote produced `value`
    `basis`      = how we arrived at `value` (auditable)
    `rationale`  = one-line "why?" string for the proposal card UI
    """

    model_config = ConfigDict(extra="forbid")

    value: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    coverage: float = Field(ge=0.0, le=1.0)
    diversity: int = Field(ge=0)
    basis_ids: list[str] = Field(default_factory=list)
    basis: SuggestionBasis
    rationale: str


class DuplicateCheckResult(BaseModel):
    """Tool 1 output. Carries the duplicate verdict AND the field aggregations.

    Two layers of access:
      • Shortcut strings (suggested_category etc.) — backward-compatible,
        easy to read from the proposal card.
      • field_suggestions dict — the full FieldSuggestion per column, with
        confidence + coverage + basis_ids + rationale. Used by the
        proposal card to show "4 of 5 similar" annotations.
    """

    model_config = ConfigDict(extra="forbid")

    candidates: list[ScoredNeighbour]
    top_match: ScoredNeighbour | None
    duplicate_verdict: DuplicateVerdict
    duplicate_threshold: float = Field(ge=0.5, le=1.0)

    # Shortcut strings (legacy / simple consumers).
    # Aligned 2026-05-29 PM to ai-service usecase.md UC-5 spec:
    #   keep:   category, subcategory (Step 1)
    #   add:    assigned_to (Step 4 agent), ci_id (Step 5 CI auto-link)
    #   drop:   service_name, catalog_item_id (not in spec)
    suggested_category: str | None = None
    suggested_subcategory: str | None = None       # incident only
    suggested_assigned_to: str | None = None       # both — Step 4 person
    suggested_ci_id: str | None = None             # both — Step 5 enrichment

    # Rich per-field provenance (Bundle A — keyed by column name).
    # The dict is the source of truth; the shortcuts above are convenience.
    field_suggestions: dict[str, FieldSuggestion] = Field(default_factory=dict)

    # Step 5 enrichment per ai-service usecase.md — tag keywords extracted
    # from neighbour titles. Empty list when nothing meaningful surfaces.
    # Hard cap: 3 distinct lowercase tokens, never repeated, never padded.
    suggested_tags: list[str] = Field(default_factory=list, max_length=3)


class AssignmentRecommendation(BaseModel):
    """Tool 2 output. Majority of `assignment_group` across the top-K neighbours.

    Parallels FieldSuggestion's shape — confidence + coverage + diversity +
    basis_ids + rationale — but stays as its own typed model because the
    Proposal references its fields directly.

    PERSON-LEVEL ASSIGNMENT (assigned_to) is deliberately NOT predicted by
    UC-5 today: it requires workload + skill + shift/PTO signals that aren't
    in our data. Industry convention (ServiceNow, Jira, BMC, Zendesk): ML
    predicts the group; a separate workload-balancing layer picks the person.
    Deferred to Phase 2 as a dedicated select_assignee tool.
    """

    model_config = ConfigDict(extra="forbid")

    assignment_group: str | None
    """None when confidence is below the floor (caller decides what to surface)."""
    confidence: float = Field(ge=0.0, le=1.0)
    """Fraction of voting top-K that picked the winning group."""
    coverage: float = Field(ge=0.0, le=1.0, default=0.0)
    """Fraction of top-K with a non-null assignment_group."""
    diversity: int = Field(ge=0, default=0)
    """Distinct non-null assignment_groups seen in top-K."""
    basis_ids: list[str] = Field(default_factory=list)
    """Neighbour IDs that voted for the chosen value."""
    basis: Literal[
        "majority_of_top_k",
        "llm_tiebreak",
        "llm_propose",
        "below_confidence_floor",
        "below_coverage",
        "empty_neighbours",
    ]
    rationale: str = ""
    """One-line "why?" string for the proposal card UI."""


class PrioritizationResult(BaseModel):
    """Tool 3 output. Impact + Urgency + Priority via the Motadata 4x4 matrix.

    The `basis` dict explains every output so the proposal card can render
    "why?" copy and the audit log captures the reasoning chain.
    """

    model_config = ConfigDict(extra="forbid")

    impact: Impact
    urgency: Urgency
    priority: Priority
    basis: dict[str, str]
    """e.g. {'impact': 'llm_inferred', 'urgency': 'sla_state_breached',
            'priority': 'matrix[On Department][Urgent]'}"""


# ── The proposal card payload ─────────────────────────────────────────────────

RiskClass = Literal["low", "medium", "high"]
ConfidenceTier = Literal["auto", "propose", "refuse"]
MutationIntent = Literal["recommend_only"]


class Proposal(BaseModel):
    """Assembled at the Section I assembly node. Sent to the frontend.

    Holds every suggestion the proposal card needs to render plus a unique
    proposal_id that the subsequent /decide call resumes the graph past
    the interrupt() boundary.

    Aligned to ai-service usecase.md UC-5 spec (locked 2026-05-29 PM):
      • category, subcategory, assigned_to, ci_id, assignment_group   ← from tools
      • impact, urgency, priority                                     ← Tool 3
      • duplicate flag                                                ← Tool 1
      • tag keywords                                                  ← LLM-first
      • overall_confidence_score, confidence_tier                     ← Section I assembly
      • risk_class                                                    ← Section I assembly
      • mutation_intent="recommend_only"                              ← Pydantic constant
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=64)
    ticket_id: str = Field(min_length=1, max_length=64)
    service_id: ServiceId
    tenant_id: str = Field(min_length=1, max_length=64)
    created_at: datetime

    # The suggested fields (per-type shape)
    suggested_category: str | None = None
    suggested_subcategory: str | None = None       # incident only
    suggested_assigned_to: str | None = None       # both
    suggested_ci_id: str | None = None             # both — Step 5 enrichment
    suggested_impact: Impact
    suggested_urgency: Urgency
    suggested_priority: Priority
    suggested_assignment_group: str | None = None
    suggested_tags: list[str] = Field(default_factory=list, max_length=3)

    # Duplicate flag
    duplicate_verdict: DuplicateVerdict
    top_duplicate_id: str | None = None
    top_duplicate_score: float | None = None

    # Section I assembly fields (5 derived)
    overall_confidence_score: float = Field(ge=0.0, le=1.0)
    confidence_tier: ConfidenceTier
    risk_class: RiskClass
    mutation_intent: MutationIntent = "recommend_only"

    # For "why?" reveal in the UI + audit chain
    prioritization_basis: dict[str, str]
    assignment_basis: Literal[
        "majority_of_top_k",
        "llm_tiebreak",
        "llm_propose",
        "below_confidence_floor",
        "below_coverage",
        "empty_neighbours",
    ]
    assignment_confidence: float = Field(ge=0.0, le=1.0)


# ── Outcome ──────────────────────────────────────────────────────────────────

class Outcome(BaseModel):
    """apply.py return value. What actually happened on Yes/No."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    ticket_id: str
    outcome: OutcomeStatus
    actor_user_id: str
    decided_at: datetime
    applied_fields: dict[str, Any] | None = None
    """On 'applied': the exact column->value map written. On 'discarded': None."""
