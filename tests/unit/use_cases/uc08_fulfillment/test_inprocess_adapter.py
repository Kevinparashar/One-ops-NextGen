"""Production-grade tests for the InProcessIntegrationAdapter.

These tests assert against the `IntegrationAdapter` Protocol — never
against `InProcessIntegrationAdapter` directly. That's the contract guarantee:
once real bindings ship (ad_real.py, okta_real.py, …) the same tests can
be parametrised across implementations.

Coverage:
  1. Protocol conformance — `isinstance(mock, IntegrationAdapter)` holds
  2. Idempotency — same key returns same result, even on failure
  3. Determinism — same inputs produce same ids
  4. Failure injection — DOC-09 §UC-8 scenarios reproduced exactly
  5. Substitution simulation — scenario 8.3
  6. Integration timeout — scenario 8.9
  7. Saga compensation — disable/revoke/release record audit events
  8. Async-safety — concurrent calls with the same key don't duplicate
  9. duration_ms emitted — observability promise honored
"""
from __future__ import annotations

import asyncio

import pytest

from oneops.use_cases.uc08_fulfillment.adapters import (
    FailurePolicy,
    InProcessIntegrationAdapter,
    IntegrationAdapter,
)
from oneops.use_cases.uc08_fulfillment.contracts import AdapterErrorClass

# ── 1. Protocol conformance ─────────────────────────────────────────────────


def test_mock_satisfies_integration_adapter_protocol():
    """The mock must be a structural match for the Protocol — if this
    test breaks, the Protocol drifted or the mock missed a method."""
    mock = InProcessIntegrationAdapter()
    assert isinstance(mock, IntegrationAdapter)


# ── 2. Idempotency ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_account_is_idempotent_on_key():
    """Production-grade guarantee: same idempotency_key + same tenant
    returns the exact same response without re-executing."""
    mock = InProcessIntegrationAdapter()
    r1 = await mock.create_directory_account(
        tenant_id="T001", user_full_name="John Smith",
        email_suggested="john.smith@corp", idempotency_key="key-A",
    )
    r2 = await mock.create_directory_account(
        tenant_id="T001", user_full_name="John Smith",
        email_suggested="john.smith@corp", idempotency_key="key-A",
    )
    assert r1 == r2
    assert r1.result is not None
    assert r1.result.account_id == r2.result.account_id


@pytest.mark.asyncio
async def test_idempotency_is_scoped_per_tenant():
    """Tenant T001's idempotency key MUST NOT collide with T002's."""
    mock = InProcessIntegrationAdapter()
    r_t1 = await mock.create_directory_account(
        tenant_id="T001", user_full_name="A B",
        email_suggested="ab@corp", idempotency_key="shared-key",
    )
    r_t2 = await mock.create_directory_account(
        tenant_id="T002", user_full_name="A B",
        email_suggested="ab@corp", idempotency_key="shared-key",
    )
    # Different tenants → different cache slots → distinct deterministic ids
    assert r_t1.result is not None
    assert r_t2.result is not None
    assert r_t1.result.account_id != r_t2.result.account_id


@pytest.mark.asyncio
async def test_failure_is_also_cached_on_key():
    """If a call fails, the SAME idempotency_key MUST return the SAME
    failure on retry — production-grade Protocol guarantee. Caller must
    use a NEW key to retry."""
    mock = InProcessIntegrationAdapter(failure_policies=[
        FailurePolicy(method="create_directory_account", fail_first_n=999,
                       error_class=AdapterErrorClass.PERMANENT,
                       error_message="boom"),
    ])
    r1 = await mock.create_directory_account(
        tenant_id="T001", user_full_name="A B",
        email_suggested="ab@corp", idempotency_key="failed-key",
    )
    r2 = await mock.create_directory_account(
        tenant_id="T001", user_full_name="A B",
        email_suggested="ab@corp", idempotency_key="failed-key",
    )
    assert r1 == r2
    assert r1.success is False
    assert r1.error_class == AdapterErrorClass.PERMANENT


# ── 3. Determinism ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_different_keys_same_inputs_produce_same_result_id():
    """Determinism: result id derives from inputs, not the idempotency
    key. Two callers asking for the same thing see the same answer."""
    mock = InProcessIntegrationAdapter()
    r1 = await mock.create_directory_account(
        tenant_id="T001", user_full_name="John Smith",
        email_suggested="john.smith@corp", idempotency_key="key-X",
    )
    r2 = await mock.create_directory_account(
        tenant_id="T001", user_full_name="John Smith",
        email_suggested="john.smith@corp", idempotency_key="key-Y",
    )
    assert r1.result is not None
    assert r2.result is not None
    assert r1.result.account_id == r2.result.account_id  # determinism
    assert r1.idempotency_key != r2.idempotency_key       # but distinct keys


# ── 4. Failure injection — DOC-09 §UC-8 scenarios ──────────────────────────


@pytest.mark.asyncio
async def test_transient_then_success_reproduces_scenario_8_2():
    """Scenario 8.2: AD account fails twice with TIMEOUT, succeeds on
    retry 3. Caller uses a FRESH idempotency_key per retry per protocol."""
    mock = InProcessIntegrationAdapter(failure_policies=[
        FailurePolicy(method="create_directory_account", fail_first_n=2,
                       error_class=AdapterErrorClass.TRANSIENT,
                       retry_after_s=1),
    ])
    r1 = await mock.create_directory_account(
        tenant_id="T001", user_full_name="X Y",
        email_suggested="x.y@corp", idempotency_key="retry-1",
    )
    r2 = await mock.create_directory_account(
        tenant_id="T001", user_full_name="X Y",
        email_suggested="x.y@corp", idempotency_key="retry-2",
    )
    r3 = await mock.create_directory_account(
        tenant_id="T001", user_full_name="X Y",
        email_suggested="x.y@corp", idempotency_key="retry-3",
    )
    assert r1.success is False
    assert r1.error_class == AdapterErrorClass.TRANSIENT
    assert r1.retry_after_seconds == 1
    assert r2.success is False
    assert r3.success is True


@pytest.mark.asyncio
async def test_substitution_reproduces_scenario_8_3():
    """Scenario 8.3: T14 out of stock; adapter returns T14s as
    substituted_model; UC-8 then requests substitution approval."""
    mock = InProcessIntegrationAdapter(out_of_stock_models={"T14"})
    r = await mock.order_hardware_asset(
        tenant_id="T001", asset_type="laptop", model_preferred="T14",
        deliver_to="HQ B2/F3", idempotency_key="po-key-1",
    )
    assert r.success is True
    assert r.result is not None
    assert r.result.substituted_model == "T14s"
    assert r.result.po_id.startswith("PO_")


@pytest.mark.asyncio
async def test_in_stock_asset_has_no_substitution():
    """Negative case for 8.3: when stock is available, no substitution."""
    mock = InProcessIntegrationAdapter()    # no out-of-stock list
    r = await mock.order_hardware_asset(
        tenant_id="T001", asset_type="laptop", model_preferred="T14",
        deliver_to="HQ", idempotency_key="po-key-2",
    )
    assert r.success is True
    assert r.result is not None
    assert r.result.substituted_model is None


@pytest.mark.asyncio
async def test_timeout_reproduces_scenario_8_9():
    """Scenario 8.9: integration endpoint down for 2+ hours. UC-8 marks
    the task BLOCKED and continues on unrelated tasks."""
    mock = InProcessIntegrationAdapter(force_timeout_methods={"grant_vpn_access"})
    r = await mock.grant_vpn_access(
        tenant_id="T001", user_id="USR0001", idempotency_key="vpn-1",
    )
    assert r.success is False
    assert r.error_class == AdapterErrorClass.TIMEOUT
    assert r.error_code == "INTEGRATION_TIMEOUT"


@pytest.mark.asyncio
async def test_permanent_failure_can_carry_partial_state_for_compensation():
    """Scenario 8.10: email created but github add failed. The PERMANENT
    failure response carries partial_state so saga rollback knows what
    DID commit."""
    mock = InProcessIntegrationAdapter(failure_policies=[
        FailurePolicy(method="add_to_groups", fail_first_n=999,
                       error_class=AdapterErrorClass.PERMANENT,
                       error_message="github API rejected scope",
                       partial_state={"mailbox_id": "MBX_existing"}),
    ])
    r = await mock.add_to_groups(
        tenant_id="T001", user_id="USR0001",
        groups=("github:org-x",), idempotency_key="add-1",
    )
    assert r.success is False
    assert r.error_class == AdapterErrorClass.PERMANENT
    assert r.partial_state == {"mailbox_id": "MBX_existing"}


# ── 5. Saga compensation audit ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compensation_events_are_recorded():
    """Every compensation call records an audit event so the saga test
    suite can assert that rollback ran when cancellation fires."""
    mock = InProcessIntegrationAdapter()
    # Provision a few things first
    acct = await mock.create_directory_account(
        tenant_id="T001", user_full_name="X Y",
        email_suggested="xy@corp", idempotency_key="a1",
    )
    vpn = await mock.grant_vpn_access(
        tenant_id="T001", user_id="USR0001", idempotency_key="v1",
    )
    assert mock.compensation_log == []
    # Now compensate
    await mock.disable_directory_account(
        tenant_id="T001", account_id=acct.result.account_id,    # type: ignore[union-attr]
        idempotency_key="d1",
    )
    await mock.revoke_vpn_access(
        tenant_id="T001", config_id=vpn.result.config_id,        # type: ignore[union-attr]
        idempotency_key="r1",
    )
    log = mock.compensation_log
    assert len(log) == 2
    assert log[0]["op"] == "disable_directory_account"
    assert log[1]["op"] == "revoke_vpn_access"


@pytest.mark.asyncio
async def test_compensation_is_idempotent_on_key():
    """Same idempotency_key for the same compensation → recorded once."""
    mock = InProcessIntegrationAdapter()
    await mock.disable_directory_account(
        tenant_id="T001", account_id="AD-1", idempotency_key="comp-1",
    )
    await mock.disable_directory_account(
        tenant_id="T001", account_id="AD-1", idempotency_key="comp-1",
    )
    # Audit log records on each call attempt? No — the cached path
    # short-circuits. Production-grade compensation must be idempotent.
    assert len(mock.compensation_log) == 1


# ── 6. Async-safety under concurrent calls ─────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_calls_same_key_do_not_duplicate():
    """Production-grade: a parallel wave that races on the same
    idempotency_key must converge to one cached response, not two."""
    mock = InProcessIntegrationAdapter()
    results = await asyncio.gather(*[
        mock.create_directory_account(
            tenant_id="T001", user_full_name="P Q",
            email_suggested="pq@corp", idempotency_key="race-key",
        )
        for _ in range(10)
    ])
    # All 10 calls return the same response object
    first = results[0]
    for r in results[1:]:
        assert r == first


# ── 7. Observability — duration_ms always present on success ──────────────


@pytest.mark.asyncio
async def test_success_response_carries_duration_ms():
    mock = InProcessIntegrationAdapter()
    r = await mock.create_directory_account(
        tenant_id="T001", user_full_name="X Y",
        email_suggested="xy@corp", idempotency_key="obs-1",
    )
    assert r.duration_ms is not None
    assert r.duration_ms >= 0


# ── 8. Reset clears state between tests ────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_clears_cache_and_compensation_log():
    mock = InProcessIntegrationAdapter()
    await mock.create_directory_account(
        tenant_id="T001", user_full_name="X Y",
        email_suggested="xy@corp", idempotency_key="r-1",
    )
    await mock.disable_directory_account(
        tenant_id="T001", account_id="AD-1", idempotency_key="r-2",
    )
    mock.reset()
    assert mock.compensation_log == []
    # Cache empty: a same-key call now re-executes (and produces same
    # result thanks to determinism, but the cache miss is the signal)
    r = await mock.create_directory_account(
        tenant_id="T001", user_full_name="X Y",
        email_suggested="xy@corp", idempotency_key="r-1",
    )
    assert r.success is True
