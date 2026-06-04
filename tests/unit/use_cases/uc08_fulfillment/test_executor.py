"""UC-8 executor — end-to-end scenario tests against live Supabase.

Each scenario exercises the wave-based orchestrator with the InProcess
adapter (deterministic, no real integrations) and asserts the final DB
state. Skipped if POSTGRES_URL is not set.

Scenarios covered (DOC-09 §UC-8):
  • 8.1 happy path           — onboarding all tasks succeed → fulfilled
  • 8.2 transient retry      — TRANSIENT fail then success → retry budget
  • 8.5 cancel + compensate  — partial run + saga rollback
  • 8.10 partial failure     — some tasks PERMANENT-fail → partial_failure
"""
from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from oneops.use_cases.uc08_fulfillment import core, executor
from oneops.use_cases.uc08_fulfillment.adapters.inprocess import (
    FailurePolicy,
    InProcessIntegrationAdapter,
)
from oneops.use_cases.uc08_fulfillment.adapters.protocol import (
    AdapterErrorClass,
)
from oneops.use_cases.uc08_fulfillment.contracts import (
    FulfillmentOutcome,
    FulfillmentRequest,
    TriggerType,
)

TEST_TENANT = "T001"
pytestmark = [
    pytest.mark.integration,  # lives in tests/unit/ but needs a live DB; runs in the integration lane (P0-1)
    pytest.mark.skipif(
        not os.getenv("POSTGRES_URL"),
        reason="POSTGRES_URL not set — live DB tests skipped",
    ),
]

_USER_POOL = ("USR00001", "USR00002", "USR00003")


# ── Shared fixtures ────────────────────────────────────────────────────────


async def _connect():
    return await asyncpg.connect(os.environ["POSTGRES_URL"])


async def _purge(conn) -> None:
    await conn.execute(
        """
        DELETE FROM itsm.fulfillment_run WHERE tenant_id=$1
          AND ritm_id IN (
            SELECT ritm_id FROM itsm.request_item
             WHERE tenant_id=$1 AND request_id LIKE 'REQ_UC08_EXEC_%'
          )
        """, TEST_TENANT,
    )
    await conn.execute(
        "DELETE FROM itsm.approval WHERE tenant_id=$1 AND ritm_id IN ("
        "SELECT ritm_id FROM itsm.request_item WHERE tenant_id=$1 "
        "AND request_id LIKE 'REQ_UC08_EXEC_%')", TEST_TENANT,
    )
    await conn.execute(
        "DELETE FROM itsm.task WHERE tenant_id=$1 AND ritm_id IN ("
        "SELECT ritm_id FROM itsm.request_item WHERE tenant_id=$1 "
        "AND request_id LIKE 'REQ_UC08_EXEC_%')", TEST_TENANT,
    )
    await conn.execute(
        "DELETE FROM itsm.request_item WHERE tenant_id=$1 "
        "AND request_id LIKE 'REQ_UC08_EXEC_%'", TEST_TENANT,
    )
    await conn.execute(
        "DELETE FROM itsm.request WHERE tenant_id=$1 "
        "AND request_id LIKE 'REQ_UC08_EXEC_%'", TEST_TENANT,
    )


@pytest.fixture
async def conn():
    c = await _connect()
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def fresh_ritm(conn, request):
    """Seed an SR, run core.fulfill_request, return the ritm_id + run_id.

    The executor tests start from here — they don't re-test decomposition.
    """
    await _purge(conn)
    sr_id = f"REQ_UC08_EXEC_{uuid.uuid4().hex[:8].upper()}"
    user_id = _USER_POOL[hash(request.node.name) % len(_USER_POOL)]
    await conn.execute(
        """
        INSERT INTO itsm.request (
            tenant_id, request_id, title, description, status,
            category, requested_for, requested_by, created_at
        ) VALUES ($1,$2,'UC-8 exec test','seed','new','onboarding',
                  $3,$3, now())
        """,
        TEST_TENANT, sr_id, user_id,
    )

    req = FulfillmentRequest(
        tenant_id=TEST_TENANT,
        request_id=sr_id,
        catalog_item_id="CAT_ONBOARDING",
        variables={
            "employee_name": "Exec Test",
            "employee_email": "exec.test@corp.example",
            "start_date": "2026-06-15",
            "department": "engineering",
            "requested_for": user_id,
            "laptop_model": "T14",
            "office_location": "HQ",
        },
        requested_for=user_id, opened_by=user_id,
        trigger_type=TriggerType.PORTAL,
    )
    outcome = await core.fulfill_request(req, connection_provider=_connect)

    try:
        yield {
            "sr_id": sr_id, "user_id": user_id,
            "ritm_id": outcome.ritm_id, "run_id": outcome.run_id,
            "tasks_total": outcome.tasks_total,
        }
    finally:
        await _purge(conn)


# ── 8.1 Happy path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_8_1_happy_path_all_tasks_succeed(conn, fresh_ritm):
    """Onboarding template — no failures injected. Every task succeeds,
    RITM ends 'fulfilled', task counts add up."""
    adapter = InProcessIntegrationAdapter()
    outcome = await executor.execute_plan(
        tenant_id=TEST_TENANT, ritm_id=fresh_ritm["ritm_id"],
        adapter=adapter, connection_provider=_connect,
    )

    assert outcome.outcome == FulfillmentOutcome.FULFILLED, outcome.display_text
    assert outcome.tasks_total == fresh_ritm["tasks_total"]
    assert outcome.tasks_completed == fresh_ritm["tasks_total"]
    assert outcome.tasks_failed == 0

    # DB end-state assertions
    ritm_row = await conn.fetchrow(
        "SELECT state, completed_tasks, failed_tasks FROM itsm.request_item "
        "WHERE tenant_id=$1 AND ritm_id=$2",
        TEST_TENANT, fresh_ritm["ritm_id"],
    )
    assert ritm_row["state"] == "fulfilled"
    assert int(ritm_row["completed_tasks"]) == fresh_ritm["tasks_total"]
    assert int(ritm_row["failed_tasks"]) == 0

    run_row = await conn.fetchrow(
        "SELECT outcome, finished_at FROM itsm.fulfillment_run "
        "WHERE tenant_id=$1 AND ritm_id=$2",
        TEST_TENANT, fresh_ritm["ritm_id"],
    )
    assert run_row["outcome"] == "fulfilled"
    assert run_row["finished_at"] is not None


# ── 8.2 Transient retry ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_8_2_transient_retry_eventually_succeeds(
    conn, fresh_ritm,
):
    """A flaky adapter call (fails TRANSIENT twice, then succeeds) is
    retried under the task's retry budget; final RITM is fulfilled."""
    # InProcess adapter's FailurePolicy: fail one method N times.
    policy = FailurePolicy(
        method="provision_email_mailbox",
        error_class=AdapterErrorClass.TRANSIENT,
        fail_first_n=2,
    )
    adapter = InProcessIntegrationAdapter(failure_policies=[policy])

    outcome = await executor.execute_plan(
        tenant_id=TEST_TENANT, ritm_id=fresh_ritm["ritm_id"],
        adapter=adapter, connection_provider=_connect,
    )

    assert outcome.outcome == FulfillmentOutcome.FULFILLED, outcome.display_text
    # The mailbox task should record retry_count ≥ 2.
    mb_retry = await conn.fetchval(
        "SELECT retry_count FROM itsm.task "
        "WHERE tenant_id=$1 AND ritm_id=$2 AND tool_id='provision_email_mailbox'",
        TEST_TENANT, fresh_ritm["ritm_id"],
    )
    assert (mb_retry or 0) >= 2, (
        f"expected ≥2 retries on mailbox; got {mb_retry}")


# ── 8.10 Partial failure (PERMANENT) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_8_10_permanent_failure_yields_partial(
    conn, fresh_ritm,
):
    """A PERMANENT failure on one task blocks downstream deps; tasks with
    no dep on it still complete. Final RITM = failed (or partial_failure
    depending on whether any done tasks exist)."""
    policy = FailurePolicy(
        method="grant_vpn_access",
        error_class=AdapterErrorClass.PERMANENT,
        fail_first_n=99,
    )
    adapter = InProcessIntegrationAdapter(failure_policies=[policy])

    outcome = await executor.execute_plan(
        tenant_id=TEST_TENANT, ritm_id=fresh_ritm["ritm_id"],
        adapter=adapter, connection_provider=_connect,
    )

    assert outcome.outcome in (
        FulfillmentOutcome.PARTIAL, FulfillmentOutcome.FAILED,
    ), outcome.display_text
    assert outcome.tasks_failed >= 1

    ritm_state = await conn.fetchval(
        "SELECT state FROM itsm.request_item "
        "WHERE tenant_id=$1 AND ritm_id=$2",
        TEST_TENANT, fresh_ritm["ritm_id"],
    )
    assert ritm_state == "failed"


# ── 8.5 Cancel + saga compensation ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_8_5_compensation_after_partial_run(
    conn, fresh_ritm,
):
    """Run executor → at least 1 task completes → call compensate_ritm
    → those completed tasks invoke the matching compensation method on
    the adapter, RITM ends 'cancelled', compensation_log is populated."""
    adapter = InProcessIntegrationAdapter()
    # Drive to completion first so we have something to rollback.
    out1 = await executor.execute_plan(
        tenant_id=TEST_TENANT, ritm_id=fresh_ritm["ritm_id"],
        adapter=adapter, connection_provider=_connect,
    )
    assert out1.outcome == FulfillmentOutcome.FULFILLED

    summary = await executor.compensate_ritm(
        tenant_id=TEST_TENANT, ritm_id=fresh_ritm["ritm_id"],
        adapter=adapter, connection_provider=_connect,
        reason="user_cancelled_test",
    )

    assert summary["compensated_tasks"] >= 1
    # Adapter's compensation_log should contain at least one rollback entry.
    assert len(adapter.compensation_log) >= 1
    # RITM cancelled
    ritm_state = await conn.fetchval(
        "SELECT state FROM itsm.request_item "
        "WHERE tenant_id=$1 AND ritm_id=$2",
        TEST_TENANT, fresh_ritm["ritm_id"],
    )
    assert ritm_state == "cancelled"


# ── Idempotency: re-invoking execute_plan on a terminal RITM is a no-op ──


@pytest.mark.asyncio
async def test_execute_plan_is_idempotent_on_terminal_ritm(conn, fresh_ritm):
    adapter = InProcessIntegrationAdapter()
    out1 = await executor.execute_plan(
        tenant_id=TEST_TENANT, ritm_id=fresh_ritm["ritm_id"],
        adapter=adapter, connection_provider=_connect,
    )
    out2 = await executor.execute_plan(
        tenant_id=TEST_TENANT, ritm_id=fresh_ritm["ritm_id"],
        adapter=adapter, connection_provider=_connect,
    )
    assert out1.outcome == out2.outcome == FulfillmentOutcome.FULFILLED
    assert out1.tasks_completed == out2.tasks_completed
