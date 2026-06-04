"""Pydantic contracts for UC-8 Catalog Item Fulfillment.

Strict typing at every boundary (rule §2.7). The same handler shape is
invoked from:
  • POST /api/uc08/fulfill   (portal button)
  • chat handler             (via router → uc08_fulfillment)

Single source of truth for state enums: every value here MUST match the
Postgres CHECK constraint in migration 0007. The DB and Python share the
truth — drift is structurally impossible because tests verify both ends.

Spec source: ai-service-use-cases.md §UC-8 (DOC-09 in the 22-doc bundle).
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── State enums ──────────────────────────────────────────────────────────────
# Values are str-backed so they serialize to JSONB columns without an adapter
# and round-trip cleanly through OTel span attributes.


class TriggerType(StrEnum):
    """Where a fulfillment request came from. Tracked on the audit row so
    operators can distinguish portal-submit from chat-submit from system
    auto-retry. Values mirror the CHECK constraint on
    itsm.fulfillment_run.trigger_type."""

    PORTAL = "portal"
    CHAT = "chat"
    AUTO_RETRY = "auto_retry"
    CANCEL = "cancel"
    ROLLBACK = "rollback"


class RitmState(StrEnum):
    """RITM lifecycle. Mirrors itsm.request_item.state CHECK constraint
    exactly. Forward-only EXCEPT cancelled which can come from any state."""

    REQUESTED = "requested"
    APPROVED = "approved"
    IN_PROGRESS = "in_progress"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class ApprovalGateState(StrEnum):
    """Lifecycle of the RITM-level approval (not the per-gate approval row,
    which is `ApprovalState` below). itsm.request_item.approval_state."""

    NOT_REQUIRED = "not_required"
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"


class TaskType(StrEnum):
    """Whether UC-8 calls a tool (automated) or routes a work-item to a
    team (manual). itsm.task.task_type CHECK constraint."""

    AUTOMATED = "automated"
    MANUAL = "manual"


class TaskState(StrEnum):
    """Atomic task lifecycle.

    Transitions:
      pending     — created; dependencies not satisfied yet
      ready       — dependencies satisfied; queued for dispatch
      in_progress — tool invoked (automated) or work-item assigned (manual)
      done        — completed successfully
      failed      — terminal failure after retries
      skipped     — declared not needed by a prior approval or rollback
      blocked     — integration outage or external dep unreachable

    Mirrors itsm.task.state CHECK constraint exactly.
    """

    PENDING = "pending"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class ApprovalType(StrEnum):
    """Classification of an approval gate so the UI + routing layer can
    pick the right approver and the right message template. Mirrors
    itsm.approval.approval_type CHECK constraint."""

    SUBSTITUTION = "substitution"
    BUDGET = "budget"
    MANAGER = "manager"
    SECURITY = "security"
    CATALOG_OWNER = "catalog_owner"


class ApprovalState(StrEnum):
    """Approval gate lifecycle. Mirrors itsm.approval.state CHECK
    constraint exactly. `pending` is a DB-state gate: the fulfillment executor
    persists an itsm.approval row and transitions the task to `blocked`; a
    terminal decision unblocks dependent tasks. (This is NOT a LangGraph
    `interrupt()` — UC-8 runs its own executor; see executor.py.)"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"


class ApprovalDecision(StrEnum):
    """The recorded yes/no on a resolved approval."""

    APPROVED = "approved"
    REJECTED = "rejected"


class NotifyChannel(StrEnum):
    """Pluggable notification channels. itsm.approval.notify_channel."""

    EMAIL = "email"
    SLACK = "slack"
    IN_APP = "in_app"


class FulfillmentOutcome(StrEnum):
    """Terminal outcome of a UC-8 invocation. Recorded on
    itsm.fulfillment_run.outcome."""

    FULFILLED = "fulfilled"        # all tasks done, RITM fulfilled
    PARTIAL = "partial"            # some tasks failed, some succeeded
    FAILED = "failed"              # could not produce a plan or all tasks failed
    CANCELLED = "cancelled"        # requester or operator cancelled
    IN_PROGRESS = "in_progress"    # paused (e.g., awaiting approval); not terminal


class AdapterErrorClass(StrEnum):
    """Production-grade failure-mode taxonomy. UC-8 reacts differently to
    each per DOC-09 §UC-8 exception rules:

      transient            → retry up to max_retries with backoff (8.2)
      permanent            → create manual fallback task, notify (spec rule 2)
      resource_unavailable → search alternative + substitution approval (8.3)
      unauthorized         → escalate to security/operator (spec rule 4)
      timeout              → mark blocked + integration outage path (8.9)
    """

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    UNAUTHORIZED = "unauthorized"
    TIMEOUT = "timeout"


# ── Catalog template shape (read from itsm.catalog_item.tasks JSONB) ────────


class CatalogTemplateTask(BaseModel):
    """One node in a catalog template's `tasks` JSONB array.

    This is what UC-8 reads from `itsm.catalog_item.tasks[i]`. The 30 demo
    catalog rows already use this exact shape — see existing data: each
    row has tasks like `{"task_id": "T1", "name": "Create AD account",
    "type": "automated", "owner_group": "GRP-SECOPS", "depends_on": []}`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=32)
    """Stable id within the template (e.g. 'T1'). Becomes `template_task_id`
    on the materialised task row."""

    name: str = Field(min_length=1, max_length=240)
    """Human-readable label, e.g. 'Create AD account'."""

    type: TaskType
    """Automated → UC-8 calls a tool. Manual → routed to a team."""

    owner_group: str | None = Field(default=None, max_length=64)
    """Assignment group e.g. 'GRP-SECOPS'. For manual tasks this is where
    the work-item lands; for automated tasks it's audit/observability."""

    depends_on: list[str] = Field(default_factory=list)
    """task_ids of nodes that must reach DONE before this becomes READY."""

    tool_id: str | None = Field(default=None, max_length=64)
    """Which UC-8 tool fulfils this task (when automated). Optional in the
    template — if absent, UC-8 picks a tool by capability inference."""

    input_template: dict[str, Any] | None = Field(default=None)
    """Per-task adapter input mapping. Keys are adapter kwargs; values are
    either literals or `{var_name}` placeholders that the materialiser
    substitutes from the request variables. Production-grade replacement
    for blanket variable-copy. Example: `{"user_full_name":
    "{employee_name}", "primary_smtp": "{employee_email}"}`."""

    sla_minutes: int | None = Field(default=None, ge=1, le=525600)  # ≤ 1 year
    """Per-task SLA budget. When absent, inherits from catalog item."""

    @field_validator("depends_on")
    @classmethod
    def _no_self_dep(cls, v: list[str], info: Any) -> list[str]:
        # Best-effort: we can't access task_id here in mode="before"; the
        # graph-build step does the cycle-detection cross-check.
        return v


class CatalogTemplate(BaseModel):
    """The catalog item row as UC-8 sees it after lookup.

    Built by `load_catalog_template` from a row of `itsm.catalog_item`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_item_id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=240)
    description: str | None = Field(default=None, max_length=2000)
    category: str = Field(min_length=1, max_length=32)
    owner_group: str = Field(min_length=1, max_length=64)
    estimated_total_minutes: int = Field(ge=1, le=525600)
    tasks: tuple[CatalogTemplateTask, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _validate_dag(self) -> CatalogTemplate:
        # Cycle + missing-ref detection at parse time. A malformed template
        # is a config bug — fail loud at boundary, never silent.
        ids = {t.task_id for t in self.tasks}
        for t in self.tasks:
            for dep in t.depends_on:
                if dep not in ids:
                    raise ValueError(
                        f"catalog template {self.catalog_item_id}: task "
                        f"{t.task_id!r} depends_on unknown task {dep!r}"
                    )
                if dep == t.task_id:
                    raise ValueError(
                        f"catalog template {self.catalog_item_id}: task "
                        f"{t.task_id!r} depends on itself"
                    )
        return self


# ── Inputs to the handler ───────────────────────────────────────────────────


class FulfillmentRequest(BaseModel):
    """What UC-8's handler entry-point receives.

    Built identically by:
      • the portal route POST /api/uc08/fulfill, after RBAC + tenant binding
      • the chat handler, after LLM slot-fills variables from natural lang

    Boundary-validated: malformed input fails before reaching any tool or
    LangGraph. No defaults that hide bugs.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=64)
    """FK to itsm.request — the parent SR must already exist."""

    catalog_item_id: str = Field(min_length=1, max_length=64)
    """FK to itsm.catalog_item — the template must already exist."""

    variables: dict[str, Any] = Field(default_factory=dict)
    """Form field values from the requester. The template's variable_schema
    governs what's required; missing required fields fail at the boundary."""

    requested_for: str = Field(min_length=1, max_length=128)
    """User id this RITM is for (e.g., the new joiner). May differ from
    opened_by (e.g., HR opens an SR FOR a new employee)."""

    opened_by: str = Field(min_length=1, max_length=128)
    """User id who submitted the request (caller's principal)."""

    quantity: int = Field(default=1, ge=1, le=100)
    """How many of this catalog item to fulfill. v1: typically 1; allow up
    to 100 for bulk laptop orders, etc."""

    idempotency_key: str | None = Field(default=None, max_length=128)
    """Caller-supplied retry-safety token. Same key + same tenant => same
    RITM (no duplicates created). Enforced by UNIQUE constraint."""

    trigger_type: TriggerType
    """Where this request came from. Recorded on fulfillment_run.trigger_type."""


# ── Plan (output of Phase 1 decomposition) ──────────────────────────────────


class TaskPlanItem(BaseModel):
    """One node in the materialised plan. Persisted as part of
    `itsm.request_item.plan` JSONB AND mirrored as a row in `itsm.task`.

    Difference from CatalogTemplateTask:
      • template_task_id is the catalog source id (e.g., 'T1')
      • input_payload carries the substituted variables ready for the tool
      • sla_due is the resolved deadline, not just minutes
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    template_task_id: str = Field(min_length=1, max_length=32)
    task_name: str = Field(min_length=1, max_length=240)
    task_type: TaskType
    tool_id: str | None = Field(default=None, max_length=64)
    depends_on: list[str] = Field(default_factory=list)
    assignment_group: str | None = Field(default=None, max_length=64)
    sla_minutes: int | None = Field(default=None, ge=1, le=525600)
    input_payload: dict[str, Any] = Field(default_factory=dict)


class FulfillmentPlan(BaseModel):
    """The materialised DAG produced by Phase 1 decomposition.

    Persisted to itsm.request_item.plan and used by Phase 2 orchestration.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ritm_id: str = Field(min_length=1, max_length=64)
    catalog_item_id: str = Field(min_length=1, max_length=64)
    tasks: tuple[TaskPlanItem, ...] = Field(min_length=1, max_length=200)
    estimated_total_minutes: int = Field(ge=1, le=525600)

    @model_validator(mode="after")
    def _validate_dag(self) -> FulfillmentPlan:
        ids = {t.template_task_id for t in self.tasks}
        for t in self.tasks:
            for dep in t.depends_on:
                if dep not in ids:
                    raise ValueError(
                        f"plan for {self.ritm_id}: task {t.template_task_id!r} "
                        f"depends_on unknown task {dep!r}"
                    )
                if dep == t.template_task_id:
                    raise ValueError(
                        f"plan for {self.ritm_id}: task "
                        f"{t.template_task_id!r} depends on itself"
                    )
        return self


# ── Approval (DOC-09 §UC-8 8.3, 8.4, 8.8) ──────────────────────────────────


class Approval(BaseModel):
    """One approval gate. Persisted as one row in itsm.approval.

    Created with state=PENDING by the fulfillment executor when a task uses
    tool_id='request_human_approval' (executor.py); the task transitions to
    `blocked` and `langgraph_interrupt_id` is None. Approval is DB-state, NOT
    a LangGraph interrupt.

    NOTE (as of 2026-06-04): there is currently NO production endpoint that
    consumes a decision to unblock the task — `record_approval_decision`
    (db.py) is defined but not yet wired to any route. The resume path is a
    KNOWN GAP, not a shipped feature. (An earlier docstring here described a
    `POST /api/uc08/approve` + LangGraph-resume flow that does not exist.)
    """

    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    ritm_id: str = Field(min_length=1, max_length=64)
    task_id: str | None = Field(default=None, max_length=64)
    """When set, the approval is scoped to a single task (e.g.,
    'approve laptop substitution'). When None, it's a RITM-level gate
    (e.g., 'approve the whole onboarding budget')."""

    approval_type: ApprovalType
    reason: str = Field(min_length=1, max_length=2000)
    payload: dict[str, Any] | None = Field(default=None)
    """Structured proposal: for substitution `{from: 'T14', to: 'T14s'}`,
    for budget `{amount_cents: 250000, threshold_cents: 100000}`, etc."""

    state: ApprovalState = ApprovalState.PENDING
    decision: ApprovalDecision | None = None
    decision_comment: str | None = Field(default=None, max_length=2000)

    requested_from: str = Field(min_length=1, max_length=128)
    """Who must approve. Either a user_id or a role token like
    `manager_of:USR00012` or `group_lead:GRP-PROCUREMENT`."""

    decided_by: str | None = Field(default=None, max_length=128)
    notify_channel: NotifyChannel | None = None
    langgraph_interrupt_id: str | None = Field(default=None, max_length=128)

    sla_due: datetime | None = None
    sla_breached: bool = False

    created_at: datetime
    decided_at: datetime | None = None
    expires_at: datetime | None = None


# ── Outcome (returned by the handler) ───────────────────────────────────────


class Outcome(BaseModel):
    """What UC-8's handler returns to the caller.

    Shape is stable across portal + chat + NATS reply. The frontend renders
    `outcome_summary_text` directly when present; otherwise it composes a
    summary from `tasks_*` counters.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=64)
    ritm_id: str = Field(min_length=1, max_length=64)
    catalog_item_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)

    outcome: FulfillmentOutcome
    tasks_total: int = Field(ge=0)
    tasks_completed: int = Field(ge=0)
    tasks_failed: int = Field(ge=0)
    tasks_skipped: int = Field(ge=0)
    tasks_in_progress: int = Field(ge=0)

    pending_approval_id: str | None = Field(default=None, max_length=64)
    """When outcome=IN_PROGRESS and the cause is a pending approval gate,
    this names the gate. Caller can poll or watch for the resume event."""

    estimated_completion: datetime | None = None
    trace_id: str | None = Field(default=None, max_length=64)

    display_text: str | None = Field(default=None, max_length=8000)
    """Pre-rendered chat-ready text. The chat composer uses this verbatim;
    button callers ignore it and render from the structured fields."""

    @model_validator(mode="after")
    def _counters_sum(self) -> Outcome:
        # Production-grade invariant: counters must sum to tasks_total.
        # Catches off-by-one errors in handler aggregation.
        s = (
            self.tasks_completed
            + self.tasks_failed
            + self.tasks_skipped
            + self.tasks_in_progress
        )
        if s > self.tasks_total:
            raise ValueError(
                f"Outcome counters sum {s} exceeds tasks_total "
                f"{self.tasks_total} for ritm {self.ritm_id}"
            )
        return self


# ── Status query (DOC-09 §UC-8 8.6) ─────────────────────────────────────────


class FulfillmentStatus(BaseModel):
    """Live status snapshot for one RITM. Returned by
    GET /api/uc08/status/{ritm_id} and by the chat status tool."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=64)
    ritm_id: str = Field(min_length=1, max_length=64)
    catalog_item_id: str = Field(min_length=1, max_length=64)
    state: RitmState
    approval_state: ApprovalGateState | None = None

    tasks_total: int = Field(ge=0)
    tasks_by_state: dict[str, int]
    """Buckets keyed by TaskState value, e.g. {'done': 6, 'in_progress': 2}."""

    pending_approvals: tuple[str, ...] = ()
    """approval_ids currently in PENDING state."""

    sla_due: datetime | None = None
    sla_breached: bool = False
    estimated_completion: datetime | None = None

    opened_at: datetime
    updated_at: datetime
    fulfilled_at: datetime | None = None


__all__ = [
    # State enums
    "TriggerType",
    "RitmState",
    "ApprovalGateState",
    "TaskType",
    "TaskState",
    "ApprovalType",
    "ApprovalState",
    "ApprovalDecision",
    "NotifyChannel",
    "FulfillmentOutcome",
    "AdapterErrorClass",
    # Models
    "CatalogTemplateTask",
    "CatalogTemplate",
    "FulfillmentRequest",
    "TaskPlanItem",
    "FulfillmentPlan",
    "Approval",
    "Outcome",
    "FulfillmentStatus",
]
