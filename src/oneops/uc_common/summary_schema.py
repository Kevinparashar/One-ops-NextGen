"""EntitySummary — the canonical UC-1 response shape.

One shape for every service (incident, service_request, problem, change,
asset, cmdb_ci). Per-service variation lives in the display_spec, NOT in
the envelope. This is the contract every UC-1 handler returns and every
consumer (UI, aggregator, LLM, OTel) reads.

Design rules enforced by validation (production-grade, 1000-UC ready):

  1. `summary` is a single coherent paragraph (no bullet lists, no markdown
     tables) — the narrative section. The LLM composes it; the faithfulness
     verifier gates it; the deterministic composer is the fallback. Either
     way, the shape is one paragraph.
  2. `key_details` are scalar (Moveworks attention budget). A row whose
     natural value is list-shaped becomes a count + a follow-up `ActionRef`.
  3. RBAC-redacted rows are dropped, not rendered as "N/A" or "[redacted]"
     (no silent-failure leak). `truncated=True` surfaces that data was
     withheld without naming the field.
  4. `claim_provenance` anchors every narrative claim to a `KeyDetail.label`
     or a `record_context.<path>` — produced by the faithfulness verifier.
     A summary that fails verification falls back to the deterministic
     composer (canned response — zero claims, empty provenance).
  5. `data_freshness` + `cache_age_s` are not optional decoration — they
     make the response audit-honest about whether the user is seeing live
     state. Handlers stamp these from `ToolContext.cache_hint`.
  6. `confidence` carries the handler's self-assessment (deterministic =
     1.0, LLM = gateway-reported, fallback = 0.5). The executor's
     handoff/aggregation logic branches on this; handlers do not.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# Schema version window — codec N/N-1 rule (ADR-0001). Bumped only on a
# breaking shape change; additive optional fields are backward-compatible.
SUMMARY_SCHEMA_CURRENT = 1
SUMMARY_SCHEMA_MIN_SUPPORTED = 1


# ── Enums ────────────────────────────────────────────────────────────────


class EntityType(StrEnum):
    """The six ITSM entity types UC-1 summarises. Closed set — adding a new
    entity type is a deliberate registry change, not a runtime invention.
    UI/aggregator may switch on this for an icon, NEVER for a content rule."""

    INCIDENT = "incident"
    SERVICE_REQUEST = "service_request"
    PROBLEM = "problem"
    CHANGE = "change"
    ASSET = "asset"
    CMDB_CI = "cmdb_ci"


ENTITY_TYPES: frozenset[str] = frozenset(e.value for e in EntityType)


class KeyDetailKind(StrEnum):
    """The display-kind hint a renderer/LLM uses to format a row. Closed set
    — keeps the renderer simple, prevents handler-side invention."""

    TEXT = "text"
    DATE = "date"
    DURATION = "duration"
    ENUM = "enum"          # status/priority codes
    ID_REF = "id_ref"      # cross-entity reference id
    MONEY = "money"
    METRIC = "metric"      # numeric measurement


class CitationSource(StrEnum):
    """Where a row of evidence came from. Closed set — citation rendering
    and audit queries depend on this being predictable."""

    ITSM = "itsm"           # incident/request/change/problem record
    CMDB = "cmdb"           # asset / CI record
    KB = "kb"               # knowledge base article
    GRAPH = "graph"         # IT operations graph relationship
    INTERNAL = "internal"


class DataFreshness(StrEnum):
    LIVE = "live"
    CACHED = "cached"


# ── Value objects (frozen — they are not mutated after construction) ────


class KeyDetail(BaseModel):
    """One row in the structured `Key Details` section.

    `label` is the human-facing column ("Reported By"); `value` is what the
    user sees ("USR00007"); `raw` is the machine value the next tool can
    feed back into the system without re-parsing. `kind` lets renderers and
    downstream LLMs format consistently."""

    # extra="ignore" — forward-compat: a producer on schema_version=2 may
    # add fields; a v1 consumer drops them rather than reject the envelope.
    model_config = {"frozen": True, "extra": "ignore"}

    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=400)
    raw: Any | None = None
    kind: KeyDetailKind = KeyDetailKind.TEXT

    @field_validator("value")
    @classmethod
    def _strip_check(cls, v: str) -> str:
        if v.strip() != v:
            raise ValueError("KeyDetail.value must not have leading/trailing whitespace")
        return v


class Citation(BaseModel):
    """Provenance for a fact shown to the user.

    Every `key_details` row that came from a system record has at least one
    citation. The UI uses `url` for deep-links; audit uses `record_id` +
    `fetched_at` to reconstruct what the user actually saw."""

    model_config = {"frozen": True, "extra": "ignore"}

    source: CitationSource
    record_id: str = Field(min_length=1, max_length=128)
    url: str | None = Field(default=None, max_length=2048)
    fetched_at: datetime


class ActionRef(BaseModel):
    """A next-step the caller MAY take, already RBAC-filtered.

    `actions_available` is a closed list. A renderer/LLM never offers an
    action that isn't here; that would be an unauthorised path. Handlers
    populate this via `oneops.uc_common.actions.filter_actions` against
    the current `AuthzDecision` — never by listing every possible action."""

    model_config = {"frozen": True, "extra": "ignore"}

    action_id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=80)
    requires_confirmation: bool = True       # action-tier defaults true
    requires_slots: tuple[str, ...] = ()


class PartyRef(BaseModel):
    """A person referenced inside a summary (assignee, requester, approver).

    Kept structured even when also rendered as a `KeyDetail` row — so the
    aggregator/LLM can address the party without re-parsing the row value."""

    model_config = {"frozen": True, "extra": "ignore"}

    user_id: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=160)
    role: str | None = Field(default=None, max_length=80)


class ClaimRef(BaseModel):
    """One narrative claim → its anchor in the structured payload.

    Produced by the faithfulness verifier. `claim` is the LLM-extracted
    sentence/assertion; `anchor` names the supporting fact, either as a
    `key_details` label (e.g., 'Status') or a record_context path
    (e.g., 'workaround.applied_at'). An empty `claim_provenance` means the
    deterministic-fallback path ran — the canned response carries no
    claims to verify."""

    model_config = {"frozen": True, "extra": "ignore"}

    claim: str = Field(min_length=1, max_length=800)
    anchor: str = Field(min_length=1, max_length=200)
    anchor_kind: str = Field(min_length=1, max_length=32)   # "key_detail" | "record_context"


# ── Envelope ─────────────────────────────────────────────────────────────


class EntitySummary(BaseModel):
    """The canonical UC-1 response. Same shape for all six entity types.

    Returned from every UC-1 handler. Validated at the `ToolRunner`
    boundary — a handler that returns the wrong shape produces a typed
    `FAILED` step result, never a silent leak into a prompt.

    Evolution policy (1000-UC discipline):
      * ADD a field → declare it Optional with a default → backward-compat
        is automatic; old producers decode cleanly.
      * RENAME a field → use `Field(alias='old_name')` on the new name and
        keep the old name on the receiver for one N/N-1 window.
      * DELETE a field → leave it as deprecated-Optional for one window,
        then drop. Receivers tolerate its absence by default.
      * Breaking shape change → bump `SUMMARY_SCHEMA_CURRENT` and shift the
        support window; the version-window validator rejects out-of-window
        envelopes loudly. `extra='ignore'` lets a v(N+1) producer still be
        consumed by a vN consumer for additive fields."""

    model_config = {"frozen": True, "extra": "ignore"}

    # Wire-version (codec N/N-1 — ADR-0001). Pinned literal — the validator
    # below enforces it so an out-of-window envelope is rejected at construction.
    schema_version: int = SUMMARY_SCHEMA_CURRENT

    # ── Identity ─────────────────────────────────────────────────────────
    entity_type: EntityType
    entity_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)

    # ── Section 1: narrative ─────────────────────────────────────────────
    summary: str = Field(min_length=1, max_length=4000)

    # ── Section 2: structured rows (ordered, schema-prescribed) ──────────
    key_details: tuple[KeyDetail, ...]

    # ── Trust + provenance ───────────────────────────────────────────────
    citations: tuple[Citation, ...] = ()
    claim_provenance: tuple[ClaimRef, ...] = ()

    # ── Cache transparency (DOC-13A grounding lineage) ───────────────────
    data_freshness: DataFreshness = DataFreshness.LIVE
    cache_age_s: int | None = Field(default=None, ge=0)

    # ── Attention budget ─────────────────────────────────────────────────
    truncated: bool = False    # True if rows were dropped (RBAC or missing)

    # ── Authorised next-steps (already RBAC-filtered by the handler) ─────
    actions_available: tuple[ActionRef, ...] = ()

    # ── People (optional — also appears in key_details when present) ─────
    assignee: PartyRef | None = None
    requester: PartyRef | None = None

    # ── Confidence (graduated autonomy — DOC-02 Phase 4 / Agentforce) ────
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    confidence_source: str = Field(default="deterministic", max_length=32)

    # ── Constraints ──────────────────────────────────────────────────────

    @field_validator("schema_version")
    @classmethod
    def _version_in_window(cls, v: int) -> int:
        if not (SUMMARY_SCHEMA_MIN_SUPPORTED <= v <= SUMMARY_SCHEMA_CURRENT):
            raise ValueError(
                f"EntitySummary schema_version={v} outside support window "
                f"[{SUMMARY_SCHEMA_MIN_SUPPORTED}, {SUMMARY_SCHEMA_CURRENT}]"
            )
        return v

    @field_validator("summary")
    @classmethod
    def _summary_is_paragraph(cls, v: str) -> str:
        # The narrative section is a single coherent paragraph. Markdown
        # bullets / headers in this slot break the renderer contract — the
        # structured slot for key/value rows is `key_details`, not here.
        stripped = v.strip()
        if not stripped:
            raise ValueError("EntitySummary.summary cannot be blank")
        for marker in ("\n- ", "\n* ", "\n# ", "\n## ", "\n```"):
            if marker in v:
                raise ValueError(
                    "EntitySummary.summary must be a single paragraph — "
                    "structured rows belong in key_details"
                )
        return v

    @field_validator("key_details")
    @classmethod
    def _key_details_unique_and_ordered(
        cls, v: tuple[KeyDetail, ...]
    ) -> tuple[KeyDetail, ...]:
        if not v:
            raise ValueError("EntitySummary.key_details must not be empty")
        labels = [kd.label for kd in v]
        if len(labels) != len(set(labels)):
            raise ValueError("EntitySummary.key_details labels must be unique")
        return v

    @model_validator(mode="after")
    def _cache_age_consistency(self) -> EntitySummary:
        if self.data_freshness is DataFreshness.LIVE and self.cache_age_s is not None:
            raise ValueError("data_freshness=live → cache_age_s must be None")
        if self.data_freshness is DataFreshness.CACHED and self.cache_age_s is None:
            raise ValueError("data_freshness=cached → cache_age_s must be set")
        return self


__all__ = [
    "SUMMARY_SCHEMA_CURRENT",
    "SUMMARY_SCHEMA_MIN_SUPPORTED",
    "ENTITY_TYPES",
    "EntityType",
    "KeyDetailKind",
    "CitationSource",
    "DataFreshness",
    "KeyDetail",
    "Citation",
    "ActionRef",
    "PartyRef",
    "ClaimRef",
    "EntitySummary",
]
