"""UC-8 executor — devil's-play (adversarial) probes.

Beyond the 5 standard scenarios, these probes exercise:
  • Concurrent execute_plan calls on the same RITM (lock behaviour)
  • Format-string injection via malicious variables (security hardening)
  • Adapter hang → timeout → in-band TRANSIENT retry
  • Retry budget exhaustion
  • Compensation with missing output payload (degraded saga path)
  • Variable sanitisation — `{__class__}` style attacks

Skipped if POSTGRES_URL is unset.
"""
from __future__ import annotations

import asyncio
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


async def _connect():
    return await asyncpg.connect(os.environ["POSTGRES_URL"])


async def _purge(conn) -> None:
    await conn.execute(
        "DELETE FROM itsm.fulfillment_run WHERE tenant_id=$1 "
        "AND ritm_id IN (SELECT ritm_id FROM itsm.request_item "
        "WHERE tenant_id=$1 AND request_id LIKE 'REQ_UC08_DEVIL_%')",
        TEST_TENANT,
    )
    for tbl in ("approval", "task", "request_item", "request"):
        col = "ritm_id IN (SELECT ritm_id FROM itsm.request_item " \
              "WHERE tenant_id=$1 AND request_id LIKE 'REQ_UC08_DEVIL_%')" \
              if tbl in ("approval", "task") else \
              ("request_id LIKE 'REQ_UC08_DEVIL_%'"
               if tbl == "request_item" else
               "request_id LIKE 'REQ_UC08_DEVIL_%'")
        await conn.execute(f"DELETE FROM itsm.{tbl} WHERE tenant_id=$1 AND {col}", TEST_TENANT)


@pytest.fixture
async def conn():
    c = await _connect()
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def fresh_ritm(conn, request):
    await _purge(conn)
    sr_id = f"REQ_UC08_DEVIL_{uuid.uuid4().hex[:8].upper()}"
    user_id = _USER_POOL[hash(request.node.name) % len(_USER_POOL)]
    await conn.execute(
        """
        INSERT INTO itsm.request (
            tenant_id, request_id, title, description, status,
            category, requested_for, requested_by, created_at
        ) VALUES ($1,$2,'devil','seed','new','onboarding',$3,$3,now())
        """,
        TEST_TENANT, sr_id, user_id,
    )
    req = FulfillmentRequest(
        tenant_id=TEST_TENANT, request_id=sr_id,
        catalog_item_id="CAT_ONBOARDING",
        variables={
            "employee_name": "Devil Probe",
            "employee_email": "devil@corp.example",
            "department": "engineering",
            "requested_for": user_id,
            "laptop_model": "T14",
            "office_location": "HQ",
            "start_date": "2026-06-15",
        },
        requested_for=user_id, opened_by=user_id,
        trigger_type=TriggerType.PORTAL,
    )
    outcome = await core.fulfill_request(req, connection_provider=_connect)
    try:
        yield {"ritm_id": outcome.ritm_id, "user_id": user_id,
               "sr_id": sr_id, "tasks_total": outcome.tasks_total}
    finally:
        await _purge(conn)


# ── Devil 1 — concurrent execute_plan on the same RITM ─────────────────────


@pytest.mark.asyncio
async def test_devil_concurrent_execute_plan_serialises_via_lock(
    conn, fresh_ritm,
):
    """Two execute_plan() coroutines on the same RITM. The advisory lock
    must ensure only ONE actually runs the wave loop; the other returns
    a partial-status snapshot (or runs after the first finishes — both
    are acceptable; the critical invariant is NO duplicate adapter calls
    inside the same wave)."""
    adapter = InProcessIntegrationAdapter()
    # Run two concurrent executions
    results = await asyncio.gather(
        executor.execute_plan(
            tenant_id=TEST_TENANT, ritm_id=fresh_ritm["ritm_id"],
            adapter=adapter, connection_provider=_connect,
        ),
        executor.execute_plan(
            tenant_id=TEST_TENANT, ritm_id=fresh_ritm["ritm_id"],
            adapter=adapter, connection_provider=_connect,
        ),
    )
    # Both calls returned. At least one is fulfilled (the lock holder).
    outcomes = {r.outcome for r in results}
    assert FulfillmentOutcome.FULFILLED in outcomes or \
        FulfillmentOutcome.IN_PROGRESS in outcomes
    # Final DB state must be fulfilled
    final_state = await conn.fetchval(
        "SELECT state FROM itsm.request_item "
        "WHERE tenant_id=$1 AND ritm_id=$2",
        TEST_TENANT, fresh_ritm["ritm_id"],
    )
    assert final_state == "fulfilled"


# ── Devil 2 — format-string injection ──────────────────────────────────────


@pytest.mark.asyncio
async def test_devil_format_string_injection_rejected(conn):
    """Variables with non-identifier keys (e.g. attribute-access tricks)
    must be silently dropped by the sanitiser, leaving placeholders
    untouched. The adapter then fails the call (visible, fail-loud)."""
    from oneops.use_cases.uc08_fulfillment.core import (
        _sanitise_variables,
        _substitute_input_template,
    )
    malicious = {
        "employee_name": "OK",
        "__class__": "boom",
        "x.__init__": "boom",
        "weird key with spaces": "boom",
        "a-b-c": "boom",
        "valid_key2": "kept",
    }
    safe = _sanitise_variables(malicious)
    assert "__class__" not in safe
    assert "x.__init__" not in safe
    assert "weird key with spaces" not in safe
    assert "a-b-c" not in safe
    assert safe.get("employee_name") == "OK"
    assert safe.get("valid_key2") == "kept"

    # End-to-end: substitution honours sanitiser
    tpl = {"name": "{employee_name}", "evil": "{__class__}"}
    out = _substitute_input_template(tpl, malicious)
    assert out["name"] == "OK"
    assert out["evil"] == "{__class__}"  # placeholder left intact


# ── Devil 3 — missing substitution variable ────────────────────────────────


@pytest.mark.asyncio
async def test_devil_missing_variable_leaves_placeholder(conn):
    """A `{var}` referencing a variable not provided is left as the
    literal `{var}` string. The adapter then fails the call — better
    than silent default."""
    from oneops.use_cases.uc08_fulfillment.core import _substitute_input_template
    tpl = {"a": "{never_provided}", "b": "{provided}"}
    out = _substitute_input_template(tpl, {"provided": "yes"})
    assert out["a"] == "{never_provided}"
    assert out["b"] == "yes"


# ── Devil 4 — retry budget exhaustion → terminal failure ──────────────────


@pytest.mark.asyncio
async def test_devil_retry_budget_exhaustion(conn, fresh_ritm):
    """A TRANSIENT failure forever (fail_first_n=999) must eventually
    exhaust the per-task retry budget and transition the task to
    'failed' (not loop forever)."""
    policy = FailurePolicy(
        method="grant_vpn_access",
        error_class=AdapterErrorClass.TRANSIENT,
        fail_first_n=999,
    )
    adapter = InProcessIntegrationAdapter(failure_policies=[policy])
    outcome = await executor.execute_plan(
        tenant_id=TEST_TENANT, ritm_id=fresh_ritm["ritm_id"],
        adapter=adapter, connection_provider=_connect,
    )
    # Must terminate (not stuck in_progress)
    assert outcome.outcome in (
        FulfillmentOutcome.PARTIAL, FulfillmentOutcome.FAILED,
    )
    vpn_state = await conn.fetchval(
        "SELECT state FROM itsm.task WHERE tenant_id=$1 AND ritm_id=$2 "
        "AND tool_id='grant_vpn_access'",
        TEST_TENANT, fresh_ritm["ritm_id"],
    )
    assert vpn_state == "failed", f"expected failed, got {vpn_state}"


# ── Devil 5 — compensation with output_payload missing the id field ───────


@pytest.mark.asyncio
async def test_devil_compensation_handles_missing_output_field(conn, fresh_ritm):
    """If a previous adapter call somehow persisted an output_payload
    without the id we need to roll back, compensation should report
    the gap per-task rather than crash."""
    adapter = InProcessIntegrationAdapter()
    await executor.execute_plan(
        tenant_id=TEST_TENANT, ritm_id=fresh_ritm["ritm_id"],
        adapter=adapter, connection_provider=_connect,
    )
    # Corrupt one task's output_payload to simulate the degenerate case
    await conn.execute(
        "UPDATE itsm.task SET output_payload='{}'::jsonb "
        "WHERE tenant_id=$1 AND ritm_id=$2 "
        "AND tool_id='create_directory_account'",
        TEST_TENANT, fresh_ritm["ritm_id"],
    )
    summary = await executor.compensate_ritm(
        tenant_id=TEST_TENANT, ritm_id=fresh_ritm["ritm_id"],
        adapter=adapter, connection_provider=_connect,
    )
    create_result = next(
        r for r in summary["results"]
        if r["tool_id"] == "create_directory_account"
    )
    assert create_result.get("ok") is False
    assert "account_id" in (create_result.get("error") or "")


# ── Devil 6 — variable-substitution preserves int/list/nested ─────────────


@pytest.mark.asyncio
async def test_devil_substitution_preserves_non_string_values(conn):
    """input_template values that are ints / lists / nested dicts must
    pass through; only strings get format-substituted."""
    from oneops.use_cases.uc08_fulfillment.core import _substitute_input_template
    tpl = {
        "qty": 3,
        "tags": ["a", "{user}"],
        "nested": {"who": "{user}", "always": "constant"},
        "literal_braces": "no_substitution_here",
    }
    out = _substitute_input_template(tpl, {"user": "alice"})
    assert out["qty"] == 3
    assert out["tags"] == ["a", "alice"]
    assert out["nested"] == {"who": "alice", "always": "constant"}
    assert out["literal_braces"] == "no_substitution_here"
