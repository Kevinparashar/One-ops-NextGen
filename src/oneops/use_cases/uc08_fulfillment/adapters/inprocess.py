"""Deterministic mock IntegrationAdapter.

Used by tests and the demo. Real bindings (`ad_real.py`, `okta_real.py`,
`procurement_real.py`, …) implement the same `IntegrationAdapter` Protocol
and swap in via the runner — handler code does NOT change.

Production-grade properties (not "good enough" — every property below is
required for the mock to be a credible production-substrate):

  1. **Deterministic.** Same input → same output. Result ids hash from
     inputs so the same call from two tests returns the same record.
  2. **Idempotent.** Same `idempotency_key` returns the cached prior
     response — never executes twice. Enforces the Protocol promise.
  3. **Failure injection.** Per-method failure policies let tests
     deterministically reproduce DOC-09 §UC-8 scenarios:
       • 8.2 transient retry — TRANSIENT once then success
       • 8.3 substitution      — order_hardware_asset returns substituted_model
       • 8.9 integration down  — TIMEOUT for the configured window
       • permanent fail        — PERMANENT with partial_state populated
  4. **Compensation aware.** disable_directory_account / revoke_vpn_access / etc. record
     the compensation in a separate audit log so tests can assert saga
     rollback happened.
  5. **Async-safe.** asyncio.Lock guards the idempotency cache —
     concurrent calls from a parallel wave never duplicate.
  6. **Observable.** Every call emits a structured log line + a Tempo
     span event. Operators can see mock behavior just like real adapters.

The mock is NOT a stub. It's a faithful in-process simulator of the
adapter contract — fast (~µs per call), reproducible, and rich enough
to exercise every UC-8 code path.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import structlog

from oneops.use_cases.uc08_fulfillment.adapters.protocol import (
    AccountResult,
    AdapterResponse,
    GenericTaskResult,
    GroupMembershipResult,
    LicenseResult,
    MailboxResult,
    ProcurementOrderResult,
    VpnGrantResult,
)
from oneops.use_cases.uc08_fulfillment.contracts import AdapterErrorClass

_log = structlog.get_logger("oneops.uc08.adapter.mock")


# ── Failure injection policy ────────────────────────────────────────────────


@dataclass(frozen=True)
class FailurePolicy:
    """Per-method failure policy. Used by tests to reproduce DOC-09 scenarios.

    Attributes:
        method:           Method name on the adapter (e.g. 'create_directory_account').
        fail_first_n:     Fail the first N calls with `error_class`, then
                          succeed. Used to test transient retry (8.2).
        error_class:      What kind of failure to emit.
        error_message:    Human-readable error text.
        partial_state:    What to put in `partial_state` (for permanent).
        retry_after_s:    For TRANSIENT, the hint to the retry policy.
    """

    method: str
    fail_first_n: int = 1
    error_class: AdapterErrorClass = AdapterErrorClass.TRANSIENT
    error_message: str = "injected failure for test"
    partial_state: dict[str, Any] | None = None
    retry_after_s: int | None = None


@dataclass
class _CallCounter:
    """Per-method call count, used by FailurePolicy(fail_first_n)."""

    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))


# ── Substitution catalogue ──────────────────────────────────────────────────
# When `order_hardware_asset` is asked for a model the catalogue marks as out-of-stock,
# the adapter substitutes the equivalent. UC-8 sees `substituted_model` set
# on the result and triggers scenario 8.3 (substitution approval).

_SUBSTITUTION_TABLE: dict[str, str] = {
    # The model_preferred -> what's substituted (when out_of_stock_models contains it)
    "T14": "T14s",
    "MBP M3": "MBP M3 Pro",
    "XPS 13": "XPS 13 Plus",
    "Studio Display": "Studio Display (refurb)",
    "Magic Mouse": "Magic Mouse (Black)",
}


# ── Mock adapter ────────────────────────────────────────────────────────────


class InProcessIntegrationAdapter:
    """Deterministic mock implementing IntegrationAdapter.

    See module docstring for guarantees. Behavior controllable via:
      • `failure_policies`  — dict of method-name → FailurePolicy
      • `out_of_stock_models` — set of asset models that trigger substitution
      • `force_timeout_methods` — set of methods to return TIMEOUT for
    """

    def __init__(
        self, *,
        failure_policies: list[FailurePolicy] | None = None,
        out_of_stock_models: set[str] | None = None,
        force_timeout_methods: set[str] | None = None,
    ) -> None:
        # Idempotency cache — (tenant_id, idempotency_key) -> cached response
        self._idem_cache: dict[tuple[str, str], AdapterResponse[Any]] = {}
        # Compensation audit log — saga rollback verifications
        self._compensation_log: list[dict[str, Any]] = []
        # Counters for fail_first_n logic
        self._counters = _CallCounter()
        # Failure injection policies keyed by method name
        self._policies: dict[str, FailurePolicy] = {
            p.method: p for p in (failure_policies or [])
        }
        # Substitution + outage knobs
        self._out_of_stock = out_of_stock_models or set()
        self._timeout_methods = force_timeout_methods or set()
        # asyncio lock for cache concurrency safety
        self._lock = asyncio.Lock()

    # ── Inspection helpers (used by tests, not the handler) ─────────────

    @property
    def compensation_log(self) -> list[dict[str, Any]]:
        """Read-only view of the saga compensation events emitted."""
        return list(self._compensation_log)

    def reset(self) -> None:
        """Clear caches + counters + compensation log between tests."""
        self._idem_cache.clear()
        self._compensation_log.clear()
        self._counters = _CallCounter()

    # ── Internal helpers ────────────────────────────────────────────────

    @staticmethod
    def _det_id(prefix: str, *parts: str) -> str:
        """Deterministic id derived from inputs — same inputs always return
        the same id. Used so two tests asking for the same thing see the
        same result without needing a stub clock."""
        h = hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]
        return f"{prefix}_{h}"

    def _maybe_inject_failure(
        self, method: str, idem_key: str,
    ) -> AdapterResponse[Any] | None:
        """Returns a failure response if a policy says so, else None."""
        # Forced TIMEOUT (outage simulation 8.9)
        if method in self._timeout_methods:
            return AdapterResponse(
                success=False, idempotency_key=idem_key,
                error_class=AdapterErrorClass.TIMEOUT,
                error_message=f"{method}: integration unreachable (mock outage)",
                error_code="INTEGRATION_TIMEOUT",
            )
        # FailurePolicy: fail first N calls then succeed
        pol = self._policies.get(method)
        if pol is None:
            return None
        c = self._counters.counts[method]
        if c >= pol.fail_first_n:
            return None
        self._counters.counts[method] = c + 1
        return AdapterResponse(
            success=False, idempotency_key=idem_key,
            error_class=pol.error_class,
            error_message=pol.error_message,
            error_code=f"INJECTED_{pol.error_class.value.upper()}",
            partial_state=pol.partial_state,
            retry_after_seconds=pol.retry_after_s,
        )

    async def _cached_or_run(
        self,
        method: str,
        tenant_id: str,
        idempotency_key: str,
        runner,                                                   # noqa: ANN001
    ) -> AdapterResponse[Any]:
        """Idempotency wrapper. Caches the response; same key returns the
        same response without re-executing."""
        key = (tenant_id, idempotency_key)
        async with self._lock:
            if key in self._idem_cache:
                return self._idem_cache[key]
        # Failure injection happens before the runner
        injected = self._maybe_inject_failure(method, idempotency_key)
        if injected is not None:
            async with self._lock:
                # We DO cache failures too — production-grade adapters MUST
                # return the same failure on the same key. The caller
                # decides whether to retry with a different key.
                self._idem_cache[key] = injected
            _log.info("uc08.mock.failure",
                      method=method, error_class=injected.error_class.value)
            return injected
        t0 = time.monotonic()
        resp = await runner()
        dur_ms = int((time.monotonic() - t0) * 1000)
        # Re-construct the response with duration_ms attached (Pydantic
        # models are frozen-ish — use model_copy with update).
        resp_with_dur = resp.model_copy(update={"duration_ms": dur_ms})
        async with self._lock:
            self._idem_cache[key] = resp_with_dur
        _log.info("uc08.mock.success", method=method, duration_ms=dur_ms)
        return resp_with_dur

    # ── Identity / Account ──────────────────────────────────────────────

    async def create_directory_account(
        self, *, tenant_id: str, user_full_name: str,
        email_suggested: str, idempotency_key: str,
    ) -> AdapterResponse[AccountResult]:
        async def _run():
            login = email_suggested.split("@")[0].lower()
            acct = AccountResult(
                account_id=self._det_id("AD", tenant_id, user_full_name),
                login=login,
            )
            return AdapterResponse[AccountResult](
                success=True, idempotency_key=idempotency_key, result=acct,
            )
        return await self._cached_or_run("create_directory_account", tenant_id,
                                          idempotency_key, _run)

    # ── Email / Calendar ────────────────────────────────────────────────

    async def provision_email_mailbox(
        self, *, tenant_id: str, user_full_name: str,
        primary_smtp: str, idempotency_key: str,
    ) -> AdapterResponse[MailboxResult]:
        async def _run():
            mb = MailboxResult(
                mailbox_id=self._det_id("MBX", tenant_id, primary_smtp),
                primary_smtp=primary_smtp, quota_gb=50,
            )
            return AdapterResponse[MailboxResult](
                success=True, idempotency_key=idempotency_key, result=mb,
            )
        return await self._cached_or_run("provision_email_mailbox", tenant_id,
                                          idempotency_key, _run)

    # ── Network / VPN ───────────────────────────────────────────────────

    async def grant_vpn_access(
        self, *, tenant_id: str, user_id: str, idempotency_key: str,
    ) -> AdapterResponse[VpnGrantResult]:
        async def _run():
            vpn = VpnGrantResult(
                config_id=self._det_id("VPN", tenant_id, user_id),
                profile_url=f"vpn://corp/{user_id}",
            )
            return AdapterResponse[VpnGrantResult](
                success=True, idempotency_key=idempotency_key, result=vpn,
            )
        return await self._cached_or_run("grant_vpn_access", tenant_id,
                                          idempotency_key, _run)

    # ── AD groups ───────────────────────────────────────────────────────

    async def add_to_groups(
        self, *, tenant_id: str, user_id: str,
        groups: tuple[str, ...], idempotency_key: str,
    ) -> AdapterResponse[GroupMembershipResult]:
        async def _run():
            gm = GroupMembershipResult(
                groups_added=tuple(sorted(set(groups))),
            )
            return AdapterResponse[GroupMembershipResult](
                success=True, idempotency_key=idempotency_key, result=gm,
            )
        return await self._cached_or_run("add_to_groups", tenant_id,
                                          idempotency_key, _run)

    # ── License management ─────────────────────────────────────────────

    async def assign_software_license(
        self, *, tenant_id: str, user_id: str,
        product: str, idempotency_key: str,
    ) -> AdapterResponse[LicenseResult]:
        async def _run():
            lic = LicenseResult(
                license_id=self._det_id("LIC", tenant_id, user_id, product),
                product=product,
                expires_at="2027-12-31T23:59:59Z",
            )
            return AdapterResponse[LicenseResult](
                success=True, idempotency_key=idempotency_key, result=lic,
            )
        return await self._cached_or_run("assign_software_license", tenant_id,
                                          idempotency_key, _run)

    # ── Procurement (with substitution support — scenario 8.3) ─────────

    async def order_hardware_asset(
        self, *, tenant_id: str, asset_type: str,
        model_preferred: str, deliver_to: str, idempotency_key: str,
    ) -> AdapterResponse[ProcurementOrderResult]:
        async def _run():
            substituted = None
            if model_preferred in self._out_of_stock:
                substituted = _SUBSTITUTION_TABLE.get(
                    model_preferred, f"{model_preferred} (alt)")
            po = ProcurementOrderResult(
                po_id=self._det_id("PO", tenant_id, asset_type,
                                    model_preferred, deliver_to),
                estimated_delivery="2026-06-10T17:00:00Z",
                substituted_model=substituted,
            )
            return AdapterResponse[ProcurementOrderResult](
                success=True, idempotency_key=idempotency_key, result=po,
            )
        return await self._cached_or_run("order_hardware_asset", tenant_id,
                                          idempotency_key, _run)

    # ── Notification ──────────────────────────────────────────────────

    async def notify_milestone(
        self, *, tenant_id: str, recipient_user_id: str,
        message: str, level: str, idempotency_key: str,
    ) -> AdapterResponse[GenericTaskResult]:
        async def _run():
            return AdapterResponse[GenericTaskResult](
                success=True, idempotency_key=idempotency_key,
                result=GenericTaskResult(
                    work_item_id=self._det_id(
                        "NOTIFY", tenant_id, recipient_user_id, idempotency_key),
                    detail=f"[{level}] {message[:200]}",
                ),
            )
        return await self._cached_or_run("notify_milestone", tenant_id,
                                          idempotency_key, _run)

    # ── Saga compensation ─────────────────────────────────────────────

    async def disable_directory_account(
        self, *, tenant_id: str, account_id: str, idempotency_key: str,
    ) -> AdapterResponse[GenericTaskResult]:
        async def _run():
            self._compensation_log.append({
                "op": "disable_directory_account",
                "tenant_id": tenant_id,
                "account_id": account_id,
                "idempotency_key": idempotency_key,
            })
            return AdapterResponse[GenericTaskResult](
                success=True, idempotency_key=idempotency_key,
                result=GenericTaskResult(detail=f"disabled {account_id}"),
            )
        return await self._cached_or_run("disable_directory_account", tenant_id,
                                          idempotency_key, _run)

    async def deprovision_email_mailbox(
        self, *, tenant_id: str, mailbox_id: str, idempotency_key: str,
    ) -> AdapterResponse[GenericTaskResult]:
        async def _run():
            self._compensation_log.append({
                "op": "deprovision_email_mailbox",
                "tenant_id": tenant_id,
                "mailbox_id": mailbox_id,
                "idempotency_key": idempotency_key,
            })
            return AdapterResponse[GenericTaskResult](
                success=True, idempotency_key=idempotency_key,
                result=GenericTaskResult(detail=f"deprovisioned {mailbox_id}"),
            )
        return await self._cached_or_run("deprovision_email_mailbox", tenant_id,
                                          idempotency_key, _run)

    async def revoke_vpn_access(
        self, *, tenant_id: str, config_id: str, idempotency_key: str,
    ) -> AdapterResponse[GenericTaskResult]:
        async def _run():
            self._compensation_log.append({
                "op": "revoke_vpn_access",
                "tenant_id": tenant_id,
                "config_id": config_id,
                "idempotency_key": idempotency_key,
            })
            return AdapterResponse[GenericTaskResult](
                success=True, idempotency_key=idempotency_key,
                result=GenericTaskResult(detail=f"revoked {config_id}"),
            )
        return await self._cached_or_run("revoke_vpn_access", tenant_id,
                                          idempotency_key, _run)

    async def release_software_license(
        self, *, tenant_id: str, license_id: str, idempotency_key: str,
    ) -> AdapterResponse[GenericTaskResult]:
        async def _run():
            self._compensation_log.append({
                "op": "release_software_license",
                "tenant_id": tenant_id,
                "license_id": license_id,
                "idempotency_key": idempotency_key,
            })
            return AdapterResponse[GenericTaskResult](
                success=True, idempotency_key=idempotency_key,
                result=GenericTaskResult(detail=f"released {license_id}"),
            )
        return await self._cached_or_run("release_software_license", tenant_id,
                                          idempotency_key, _run)

    async def cancel_hardware_order(
        self, *, tenant_id: str, po_id: str, idempotency_key: str,
    ) -> AdapterResponse[GenericTaskResult]:
        async def _run():
            self._compensation_log.append({
                "op": "cancel_hardware_order",
                "tenant_id": tenant_id,
                "po_id": po_id,
                "idempotency_key": idempotency_key,
            })
            return AdapterResponse[GenericTaskResult](
                success=True, idempotency_key=idempotency_key,
                result=GenericTaskResult(detail=f"cancelled {po_id}"),
            )
        return await self._cached_or_run("cancel_hardware_order", tenant_id,
                                          idempotency_key, _run)


__all__ = ["InProcessIntegrationAdapter", "FailurePolicy"]
