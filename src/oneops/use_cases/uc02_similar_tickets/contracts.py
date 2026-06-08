"""Pydantic contracts for UC-2 Similar Tickets.

Strict typing at every boundary (rule §2.7). The same `SimilarTicketsResponse`
shape is returned by:
  • POST /api/uc02/similar-tickets   (button)
  • chat handler                     (via router → uc02_similar_tickets)

By construction the two paths cannot diverge: both publish onto the NATS
subject and the worker returns this exact model.

Spec source: docs/product/ai-service-use-cases.md §UC-2.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from oneops.uc_common import TimeFilter

# ── Enumerations (UC-2 v1 scope) ─────────────────────────────────────────────

ServiceId = Literal["incident", "request"]
"""v1 covers the two services with `ai.embeddings_<service>` substrate.
problem/change deferred (no `ai.embeddings_problem` table exists yet)."""

PreferStatus = Literal["any", "open", "resolved"]
"""UC-2.2/UC-2.5: 'open' biases duplicate detection;
'resolved' biases resolution-reuse; 'any' is neutral."""

SimilarFlag = Literal["likely_duplicate", "resolution_available"]
"""Spec §UC-2:
  • likely_duplicate: similarity > 0.90 AND same CI AND status='open'
  • resolution_available: similarity > 0.85 AND resolved
Only one flag fires per result; precedence: duplicate > resolution."""


# ── Input ────────────────────────────────────────────────────────────────────

class SimilarTicketsRequest(BaseModel):
    """Button (REST) and chat handler both build one of these before dispatch.

    `service_id` is optional only when `ticket_id` carries an unambiguous prefix
    (INC… → incident, REQ… → request). Bare digits require explicit service_id;
    otherwise the request is rejected at the boundary, never silently guessed.
    """

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    service_id: ServiceId | None = None

    # Caller principal — required for RBAC enforcement.
    user_id: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=64)

    # Result shaping
    max_results: int = Field(default=5, ge=1, le=20)
    """Spec default = top 5 in the demo output; cap 20."""

    # Scope filters (spec §UC-2 'Input: search scope')
    time_filter: TimeFilter | None = None
    """Structured time scope (preferred). Built by `TimeFilterExtractor`
    on the chat path; button callers may pass it directly. When set, the
    SQL applies a predicate on the configured boundary column (default
    `created_at`). See `oneops.uc_common.time_filter`."""
    same_category_only: bool = False
    same_service_only: bool = False
    prefer_status: PreferStatus = "any"
    min_similarity_score: float = Field(default=0.5, ge=0.0, le=1.0)
    """UC-2.3 low-signal: anything whose final composite (= the response
    field of the same name) falls below this is dropped from the tail.
    Default 0.5 — strips the "filler" results that previously appeared
    when there genuinely weren't 5 similar tickets in the universe."""

    # Optional precision step
    diagnosis_confirm: bool = True
    """Stage 5: cross-check on diagnosis_trail for top-K. Flag OFF for ultra-low
    latency callers; default ON because the extra ~50ms is usually worth it."""


# ── Output ───────────────────────────────────────────────────────────────────

class SimilarTicket(BaseModel):
    """One result row. Carries enough metadata for the UI to render the spec
    output and for the agent/UI to decide what to click into.

    `match_pct` is similarity in [0,100] — the spec output uses "92% match".
    `why_similar` is the operator-readable list of signals that fired
    (same_ci, same_category, same_service, resolved, recent, diagnosis_match).
    """

    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    service_id: ServiceId
    title: str
    status: str
    priority: str | None = None
    category: str | None = None
    subcategory: str | None = None
    service_name: str | None = None
    ci_id: str | None = None
    assigned_to: str | None = None
    assignment_group: str | None = None

    opened_at: datetime | None = None
    resolved_at: datetime | None = None

    similarity_score: float = Field(ge=0.0, le=1.0)
    """Raw composite score (semantic + metadata + recency). Sort key."""

    match_pct: int = Field(ge=0, le=100)
    """similarity_score * 100, integer. What the UI shows next to the ID."""

    confidence: float = Field(ge=0.0, le=1.0)
    """Semantic-only cosine (Stage 2). Useful for the UI to distinguish
    'strong match' from 'we boosted this with metadata'."""

    why_similar: list[str] = Field(default_factory=list)
    """Signal names that fired during re-rank — debuggable provenance."""

    discriminator: str | None = None
    """One-line, content-derived failure-mode label that distinguishes this
    result from its siblings (e.g. "tunnel-establishment failure" vs
    "DHCP-driven session loss"). Generated by a single batched LLM call per
    request so trust grows with the score. Empty/None when the LLM is
    unavailable — never blocks the result list."""

    flag: SimilarFlag | None = None
    """Spec §UC-2 duplicate / resolution-available labels."""


class SimilarTicketsResponse(BaseModel):
    """Worker → API → caller.

    Empty `results` is a valid response shape. The `message` field carries the
    spec-mandated user-visible explanation ("No significantly similar tickets
    found", "limited context — broaden your scope", etc.) so chat and button
    render identically.
    """

    model_config = ConfigDict(extra="forbid")

    source_ticket_id: str
    service_id: ServiceId
    tenant_id: str

    results: list[SimilarTicket] = Field(default_factory=list)
    total_candidates_considered: int = Field(ge=0, default=0)
    """Pre-rerank count after tenant + RBAC + scope filters. Operator metric."""

    message: str | None = None
    """User-visible explanation when results are empty or low-signal."""

    warning: str | None = None
    """UC-2.2 'limited context' style warnings — present alongside results."""

    cached: bool = False
    """True when served from Dragonfly. Pure observability, no behaviour."""

    time_filter: TimeFilter | None = None
    """Echo of the time filter that was applied. Lets the chat/UI render the
    label ("Found 4 tickets from {label}") without round-tripping back to the
    request. None ⇔ no time scope was applied."""

    source_ticket: SimilarTicket | None = None
    """Snapshot of the source ticket the user queried with — title, status,
    priority, CI, etc. Echoed back so the UI can show "you queried X, here
    are 5 like it" without a separate UC-1 call. similarity_score=1.0 and
    match_pct=100 are filled to satisfy the schema; the UI hides them."""


__all__ = [
    "ServiceId",
    "PreferStatus",
    "SimilarFlag",
    "SimilarTicketsRequest",
    "SimilarTicket",
    "SimilarTicketsResponse",
]
