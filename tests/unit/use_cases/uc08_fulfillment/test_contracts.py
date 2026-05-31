"""Production-grade contract tests for UC-8 Fulfillment.

Asserts every boundary invariant the handlers + DB depend on:

  • Enum values match Postgres CHECK constraints in migration 0007
  • Pydantic models round-trip cleanly through JSON
  • DAG validators catch malformed templates / plans (cycles, missing refs)
  • Outcome counters invariant holds
  • `IntegrationAdapter` Protocol can be satisfied by a stub

These tests are PURE — no DB connection, no LLM calls, no NATS. They run
in milliseconds and gate every production-grade contract guarantee.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from oneops.use_cases.uc08_fulfillment.adapters import (
    AccountResult,
    AdapterResponse,
    GenericTaskResult,
    IntegrationAdapter,
    MailboxResult,
)
from oneops.use_cases.uc08_fulfillment.contracts import (
    AdapterErrorClass,
    Approval,
    ApprovalState,
    ApprovalType,
    CatalogTemplate,
    CatalogTemplateTask,
    FulfillmentOutcome,
    FulfillmentPlan,
    FulfillmentRequest,
    FulfillmentStatus,
    Outcome,
    RitmState,
    TaskPlanItem,
    TaskState,
    TaskType,
    TriggerType,
)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Enum-to-DB CHECK-constraint parity
# ══════════════════════════════════════════════════════════════════════════════
# Production-grade invariant: every Python enum value MUST equal a value in
# the corresponding Postgres CHECK constraint. Drift here = silent runtime
# failures at write time. These tests are the gate.


def test_ritm_state_values_match_db_constraint():
    """Migration 0007 CHECK constraint:
        state IN ('requested','approved','in_progress','fulfilled',
                  'cancelled','rejected')
    """
    assert {s.value for s in RitmState} == {
        "requested", "approved", "in_progress",
        "fulfilled", "cancelled", "rejected",
    }


def test_task_state_values_match_db_constraint():
    """Migration 0007 CHECK constraint:
        state IN ('pending','ready','in_progress','done','failed',
                  'skipped','blocked')
    """
    assert {s.value for s in TaskState} == {
        "pending", "ready", "in_progress",
        "done", "failed", "skipped", "blocked",
    }


def test_task_type_values_match_db_constraint():
    assert {t.value for t in TaskType} == {"automated", "manual"}


def test_approval_state_values_match_db_constraint():
    """Migration 0007 CHECK: state IN ('pending','approved','rejected','expired','withdrawn')"""
    assert {s.value for s in ApprovalState} == {
        "pending", "approved", "rejected", "expired", "withdrawn",
    }


def test_approval_type_values_match_db_constraint():
    assert {t.value for t in ApprovalType} == {
        "substitution", "budget", "manager", "security", "catalog_owner",
    }


def test_trigger_type_values_match_db_constraint():
    """Migration 0007 CHECK on fulfillment_run.trigger_type."""
    assert {t.value for t in TriggerType} == {
        "portal", "chat", "auto_retry", "cancel", "rollback",
    }


def test_fulfillment_outcome_values_match_db_constraint_plus_in_progress():
    """DB constraint covers terminal states. `in_progress` is a Python-side
    sentinel for the handler-returned envelope when the workflow is paused
    (e.g. awaiting approval) — it's never persisted to outcome column."""
    terminal = {"fulfilled", "partial", "failed", "cancelled"}
    python_values = {o.value for o in FulfillmentOutcome}
    assert terminal.issubset(python_values)
    assert "in_progress" in python_values


# ══════════════════════════════════════════════════════════════════════════════
# 2. FulfillmentRequest — boundary validation
# ══════════════════════════════════════════════════════════════════════════════


def test_fulfillment_request_minimal_valid():
    req = FulfillmentRequest(
        tenant_id="T001", request_id="REQ0001", catalog_item_id="CAT_LAPTOP_STD",
        requested_for="USR0001", opened_by="USR0001",
        trigger_type=TriggerType.PORTAL,
    )
    assert req.quantity == 1
    assert req.variables == {}
    assert req.idempotency_key is None


def test_fulfillment_request_rejects_extra_fields():
    """Boundary safety: a typo'd field name must FAIL, not silently drop."""
    with pytest.raises(ValidationError):
        FulfillmentRequest(
            tenant_id="T001", request_id="REQ0001",
            catalog_item_id="CAT_LAPTOP_STD",
            requested_for="USR0001", opened_by="USR0001",
            trigger_type=TriggerType.PORTAL,
            misspelled_field="oops",  # type: ignore[call-arg]
        )


def test_fulfillment_request_rejects_zero_quantity():
    with pytest.raises(ValidationError):
        FulfillmentRequest(
            tenant_id="T001", request_id="REQ0001",
            catalog_item_id="CAT_LAPTOP_STD",
            requested_for="USR0001", opened_by="USR0001",
            quantity=0, trigger_type=TriggerType.PORTAL,
        )


def test_fulfillment_request_rejects_huge_quantity():
    with pytest.raises(ValidationError):
        FulfillmentRequest(
            tenant_id="T001", request_id="REQ0001",
            catalog_item_id="CAT_LAPTOP_STD",
            requested_for="USR0001", opened_by="USR0001",
            quantity=101, trigger_type=TriggerType.PORTAL,
        )


def test_fulfillment_request_round_trips_through_json():
    req = FulfillmentRequest(
        tenant_id="T001", request_id="REQ0001", catalog_item_id="CAT_LAPTOP_STD",
        variables={"model": "T14"},
        requested_for="USR0001", opened_by="USR0001",
        trigger_type=TriggerType.CHAT,
        idempotency_key="key-abc-123",
    )
    blob = req.model_dump_json()
    parsed = FulfillmentRequest.model_validate_json(blob)
    assert parsed == req


# ══════════════════════════════════════════════════════════════════════════════
# 3. CatalogTemplate — DAG validation
# ══════════════════════════════════════════════════════════════════════════════


def _ct_task(tid: str, name: str, deps: list[str] | None = None,
             ttype: TaskType = TaskType.AUTOMATED) -> CatalogTemplateTask:
    return CatalogTemplateTask(
        task_id=tid, name=name, type=ttype,
        depends_on=deps or [], owner_group="GRP-X",
    )


def test_catalog_template_minimal_valid():
    tmpl = CatalogTemplate(
        catalog_item_id="CAT_X", tenant_id="T001",
        name="X", category="hardware", owner_group="GRP-X",
        estimated_total_minutes=60,
        tasks=(_ct_task("T1", "Step 1"),),
    )
    assert len(tmpl.tasks) == 1
    assert tmpl.tasks[0].task_id == "T1"


def test_catalog_template_rejects_unknown_depends_on():
    """Malformed template = config bug. Fail loud at parse time."""
    with pytest.raises(ValidationError) as ex:
        CatalogTemplate(
            catalog_item_id="CAT_X", tenant_id="T001",
            name="X", category="hardware", owner_group="GRP-X",
            estimated_total_minutes=60,
            tasks=(
                _ct_task("T1", "Step 1"),
                _ct_task("T2", "Step 2", deps=["T1", "T_DOES_NOT_EXIST"]),
            ),
        )
    assert "depends_on unknown task" in str(ex.value)


def test_catalog_template_rejects_self_dependency():
    with pytest.raises(ValidationError) as ex:
        CatalogTemplate(
            catalog_item_id="CAT_X", tenant_id="T001",
            name="X", category="hardware", owner_group="GRP-X",
            estimated_total_minutes=60,
            tasks=(_ct_task("T1", "Step 1", deps=["T1"]),),
        )
    assert "depends on itself" in str(ex.value)


def test_catalog_template_real_onboarding_shape_validates():
    """Sanity: the shape of an actual itsm.catalog_item.tasks JSONB row
    from the seeded demo data must validate. If this breaks, the loader
    needs adapting — but the contract is right."""
    onboarding_tasks = (
        _ct_task("T1", "Create AD account"),
        _ct_task("T2", "Create email and calendar"),
        _ct_task("T3", "Order laptop", ttype=TaskType.MANUAL),
        _ct_task("T4", "Provision VPN access", deps=["T1"]),
        _ct_task("T5", "Add to GitHub org", deps=["T1"]),
        _ct_task("T6", "License: IDE + Jira", deps=["T1"]),
        _ct_task("T7", "Create badge request", deps=["T1", "T2", "T3"], ttype=TaskType.MANUAL),
        _ct_task("T8", "Assign desk", deps=["T1", "T2", "T3"], ttype=TaskType.MANUAL),
        _ct_task("T9", "Send welcome kit", deps=["T7", "T8"]),
    )
    tmpl = CatalogTemplate(
        catalog_item_id="CAT_ONBOARDING", tenant_id="T001",
        name="Employee onboarding", category="onboarding",
        owner_group="GRP-SERVICE-DESK",
        estimated_total_minutes=2880,
        tasks=onboarding_tasks,
    )
    assert len(tmpl.tasks) == 9


# ══════════════════════════════════════════════════════════════════════════════
# 4. FulfillmentPlan — same DAG invariants
# ══════════════════════════════════════════════════════════════════════════════


def _plan_task(tid: str, deps: list[str] | None = None) -> TaskPlanItem:
    return TaskPlanItem(
        template_task_id=tid, task_name=f"step {tid}",
        task_type=TaskType.AUTOMATED, depends_on=deps or [],
    )


def test_plan_rejects_cycle_via_unknown_dep():
    with pytest.raises(ValidationError):
        FulfillmentPlan(
            ritm_id="RITM0001", catalog_item_id="CAT_X",
            tasks=(_plan_task("T1"), _plan_task("T2", deps=["T999"])),
            estimated_total_minutes=10,
        )


def test_plan_round_trips_through_json():
    p = FulfillmentPlan(
        ritm_id="RITM0001", catalog_item_id="CAT_X",
        tasks=(_plan_task("T1"), _plan_task("T2", deps=["T1"])),
        estimated_total_minutes=60,
    )
    blob = p.model_dump_json()
    parsed = FulfillmentPlan.model_validate_json(blob)
    assert parsed == p


# ══════════════════════════════════════════════════════════════════════════════
# 5. Outcome counter invariant
# ══════════════════════════════════════════════════════════════════════════════


def test_outcome_counters_sum_must_not_exceed_total():
    """Production-grade aggregation invariant. Catches off-by-one bugs
    where the handler accidentally double-counts a task."""
    with pytest.raises(ValidationError) as ex:
        Outcome(
            tenant_id="T001", request_id="REQ0001", ritm_id="RITM0001",
            catalog_item_id="CAT_X", run_id="run-1",
            outcome=FulfillmentOutcome.FULFILLED,
            tasks_total=5,
            tasks_completed=4, tasks_failed=1, tasks_skipped=1,
            tasks_in_progress=0,
        )
    assert "exceeds tasks_total" in str(ex.value)


def test_outcome_round_trips():
    o = Outcome(
        tenant_id="T001", request_id="REQ0001", ritm_id="RITM0001",
        catalog_item_id="CAT_X", run_id="run-1",
        outcome=FulfillmentOutcome.FULFILLED,
        tasks_total=9, tasks_completed=9,
        tasks_failed=0, tasks_skipped=0, tasks_in_progress=0,
    )
    parsed = Outcome.model_validate_json(o.model_dump_json())
    assert parsed == o


# ══════════════════════════════════════════════════════════════════════════════
# 6. AdapterResponse + Protocol satisfiability
# ══════════════════════════════════════════════════════════════════════════════


def test_adapter_response_success_path():
    resp = AdapterResponse[AccountResult](
        success=True,
        idempotency_key="key-1",
        result=AccountResult(account_id="ad-001", login="john.smith"),
        duration_ms=42,
    )
    assert resp.result is not None
    assert resp.result.account_id == "ad-001"
    assert resp.error_class is None


def test_adapter_response_failure_carries_error_class():
    resp = AdapterResponse[AccountResult](
        success=False,
        idempotency_key="key-1",
        error_class=AdapterErrorClass.TRANSIENT,
        error_message="timeout",
        retry_after_seconds=5,
    )
    assert resp.result is None
    assert resp.error_class == AdapterErrorClass.TRANSIENT
    assert resp.retry_after_seconds == 5


def test_adapter_response_partial_state_for_compensation():
    """When a permanent failure leaves external state half-mutated, the
    `partial_state` field captures what DID commit so saga compensation
    can roll back accurately (DOC-09 §UC-8 8.5, 8.10)."""
    resp = AdapterResponse[MailboxResult](
        success=False,
        idempotency_key="key-2",
        error_class=AdapterErrorClass.PERMANENT,
        error_message="github add failed after mailbox created",
        partial_state={"mailbox_id": "MBX-001"},
    )
    assert resp.partial_state == {"mailbox_id": "MBX-001"}


def test_integration_adapter_protocol_can_be_satisfied():
    """A bare-minimum stub implementing every Protocol method type-checks
    successfully. This is the contract guarantee that real bindings can
    drop in without handler changes."""

    class _StubAdapter:
        async def create_directory_account(self, **kwargs):  # type: ignore[no-untyped-def]
            return AdapterResponse[AccountResult](
                success=True, idempotency_key=kwargs["idempotency_key"],
                result=AccountResult(account_id="x", login="x"),
            )

        async def provision_email_mailbox(self, **kwargs):  # type: ignore[no-untyped-def]
            return AdapterResponse[MailboxResult](
                success=True, idempotency_key=kwargs["idempotency_key"],
                result=MailboxResult(mailbox_id="x", primary_smtp="x@x"),
            )

        async def grant_vpn_access(self, **kwargs): ...   # type: ignore[no-untyped-def]
        async def add_to_groups(self, **kwargs): ...  # type: ignore[no-untyped-def]
        async def assign_software_license(self, **kwargs): ...  # type: ignore[no-untyped-def]
        async def order_hardware_asset(self, **kwargs): ...  # type: ignore[no-untyped-def]
        async def notify_milestone(self, **kwargs): ...  # type: ignore[no-untyped-def]
        async def disable_directory_account(self, **kwargs): ...  # type: ignore[no-untyped-def]
        async def deprovision_email_mailbox(self, **kwargs): ...  # type: ignore[no-untyped-def]
        async def revoke_vpn_access(self, **kwargs): ...  # type: ignore[no-untyped-def]
        async def release_software_license(self, **kwargs): ...  # type: ignore[no-untyped-def]
        async def cancel_hardware_order(self, **kwargs): ...  # type: ignore[no-untyped-def]

    # Runtime Protocol check passes
    stub = _StubAdapter()
    assert isinstance(stub, IntegrationAdapter)


# ══════════════════════════════════════════════════════════════════════════════
# 7. Approval model — pending state is the default and round-trips
# ══════════════════════════════════════════════════════════════════════════════


def test_approval_default_state_is_pending():
    a = Approval(
        approval_id="APP0001", tenant_id="T001",
        ritm_id="RITM0001",
        approval_type=ApprovalType.SUBSTITUTION,
        reason="Laptop T14 out of stock — substitute T14s?",
        requested_from="manager_of:USR0001",
        created_at=datetime.now(timezone.utc),
    )
    assert a.state == ApprovalState.PENDING
    assert a.decision is None


def test_approval_with_decision_round_trips():
    now = datetime.now(timezone.utc)
    a = Approval(
        approval_id="APP0001", tenant_id="T001",
        ritm_id="RITM0001",
        approval_type=ApprovalType.SUBSTITUTION,
        reason="T14 → T14s",
        payload={"from": "T14", "to": "T14s"},
        state=ApprovalState.APPROVED,
        decision=__import__(
            "oneops.use_cases.uc08_fulfillment.contracts",
            fromlist=["ApprovalDecision"],
        ).ApprovalDecision.APPROVED,
        requested_from="USR_MGR",
        decided_by="USR_MGR",
        created_at=now,
        decided_at=now,
    )
    parsed = Approval.model_validate_json(a.model_dump_json())
    assert parsed == a


# ══════════════════════════════════════════════════════════════════════════════
# 8. FulfillmentStatus — chat status-query output shape
# ══════════════════════════════════════════════════════════════════════════════


def test_fulfillment_status_minimal():
    now = datetime.now(timezone.utc)
    s = FulfillmentStatus(
        tenant_id="T001", request_id="REQ0001", ritm_id="RITM0001",
        catalog_item_id="CAT_ONBOARDING",
        state=RitmState.IN_PROGRESS,
        tasks_total=9,
        tasks_by_state={"done": 6, "in_progress": 2, "pending": 1},
        opened_at=now, updated_at=now,
    )
    assert sum(s.tasks_by_state.values()) == s.tasks_total
