"""IntegrationAdapter Protocol — the production-grade contract.

Why this exists:
  Fulfillment integrations (AD, Exchange, Okta, VPN, GitHub, procurement,
  notification channel) are external systems. We need:
    1. A single typed interface every integration honors — the Protocol.
    2. A deterministic mock that implements it for tests + demo (`mock.py`).
    3. Real implementations later — same Protocol, no handler edits.

The Protocol is the seam. Swapping mocks for real bindings is a wiring
change in `runner.py`, not a code edit in handlers or orchestration.

Failure model — Production-grade exhaustive taxonomy:

Every integration call returns an `AdapterResponse[T]`. On failure, the
caller reads `error_class` to decide how to react per DOC-09 §UC-8:

  TRANSIENT             → retry up to `max_retries` with exp backoff (8.2)
  PERMANENT             → create manual fallback task + notify (spec rule 2)
  RESOURCE_UNAVAILABLE  → search alternative + substitution approval (8.3)
  UNAUTHORIZED          → escalate to security/operator (spec rule 4)
  TIMEOUT               → mark task BLOCKED + ops alert (8.9)

Idempotency:
  Every method takes an `idempotency_key` and echoes it back. Re-calling
  with the same key returns the same result without side effects. This is
  what makes retries provably safe — the Protocol encodes it.
"""
from __future__ import annotations

from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from oneops.use_cases.uc08_fulfillment.contracts import AdapterErrorClass

# ── Typed result envelopes ───────────────────────────────────────────────────


T = TypeVar("T", bound=BaseModel)


class AdapterResponse(BaseModel, Generic[T]):
    """Production-grade response envelope returned by every adapter call.

    `success=True` → `result` is populated, `error_*` are None.
    `success=False` → `error_class` MUST be set so the caller can route
    the failure to the right reaction path.
    """

    model_config = ConfigDict(extra="forbid")

    success: bool
    idempotency_key: str = Field(min_length=1, max_length=128)
    """Echoed back from the request. The caller verifies this matches what
    it sent — production-grade defense against an adapter swallowing the
    key and producing a duplicate."""

    result: T | None = None
    """Typed result when success=True. None on failure."""

    error_class: AdapterErrorClass | None = None
    """Failure-mode taxonomy. Required when success=False; None when True."""

    error_message: str | None = Field(default=None, max_length=2000)
    error_code: str | None = Field(default=None, max_length=64)

    retry_after_seconds: int | None = Field(default=None, ge=0, le=86400)
    """When error_class=TRANSIENT, tells the retry policy how long to wait.
    None ⇒ caller picks (typically exponential backoff)."""

    partial_state: dict[str, Any] | None = None
    """When error_class=PERMANENT and the operation partially mutated
    external state (e.g., AD account created but group-add failed), this
    captures what DID complete. Used by saga compensation to know what
    to roll back. Empty dict means "nothing was committed externally"."""

    duration_ms: int | None = Field(default=None, ge=0)
    """How long the integration call took. Goes into the task's OTel span
    for SLA observability."""


# ── Per-integration result shapes ────────────────────────────────────────────
# Each is a `frozen` Pydantic model — once an adapter returns it, the value
# is immutable, can be passed through layers safely, hashed for cache keys.


class AccountResult(BaseModel):
    """Identity provider account creation result (AD / Okta / etc.)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    account_id: str = Field(min_length=1, max_length=128)
    login: str = Field(min_length=1, max_length=256)
    """e.g., 'john.smith' or 'john.smith@corp'."""


class MailboxResult(BaseModel):
    """Email mailbox provisioning result (Exchange / O365 / Google)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    mailbox_id: str = Field(min_length=1, max_length=128)
    primary_smtp: str = Field(min_length=1, max_length=320)
    quota_gb: int | None = Field(default=None, ge=1, le=10240)


class VpnGrantResult(BaseModel):
    """VPN access grant result. `config_id` is what the user's client uses."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    config_id: str = Field(min_length=1, max_length=128)
    profile_url: str | None = Field(default=None, max_length=2048)


class GroupMembershipResult(BaseModel):
    """AD / Okta group membership add. Echo of groups added (idempotent
    add — re-running returns the same set without duplicates)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    groups_added: tuple[str, ...] = Field(min_length=1)


class LicenseResult(BaseModel):
    """Software license seat assignment."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    license_id: str = Field(min_length=1, max_length=128)
    product: str = Field(min_length=1, max_length=64)
    expires_at: str | None = Field(default=None, max_length=32)  # ISO-8601


class ProcurementOrderResult(BaseModel):
    """Procurement order placed. PO id + estimated delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    po_id: str = Field(min_length=1, max_length=128)
    estimated_delivery: str | None = Field(default=None, max_length=32)  # ISO-8601
    substituted_model: str | None = Field(default=None, max_length=64)
    """When the asked-for model is out of stock and the adapter chose an
    equivalent, this names it. UC-8 then requests substitution approval
    via `request_human_approval` (scenario 8.3)."""


class GenericTaskResult(BaseModel):
    """For integration calls whose success need not be typed (notify,
    work-item assignment to a team). Returns a token the caller can use
    for status follow-up."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    work_item_id: str | None = Field(default=None, max_length=128)
    detail: str | None = Field(default=None, max_length=2000)


# ── The Protocol — production contract every integration must honor ─────────


@runtime_checkable
class IntegrationAdapter(Protocol):
    """Every fulfillment integration honors this Protocol.

    Mock (`mock.py`) and real (`ad_real.py`, etc.) implementations all
    expose the same signatures. The handler imports against the Protocol
    type — never against a concrete implementation — so swapping is a
    wiring decision, not a code edit.

    Method shape rules:
      • All methods are `async`.
      • All methods are keyword-only after `tenant_id` (positional safety).
      • All methods take `idempotency_key` and echo it in the response.
      • All methods return `AdapterResponse[<TypedResult>]` — never raise
        for business failures (use AdapterErrorClass instead). Raising is
        reserved for programmer-error bugs (validation, type errors).
    """

    # ── Identity / Account ───────────────────────────────────────────────
    async def create_directory_account(
        self, *,
        tenant_id: str,
        user_full_name: str,
        email_suggested: str,
        idempotency_key: str,
    ) -> AdapterResponse[AccountResult]: ...

    # ── Email / Calendar ─────────────────────────────────────────────────
    async def provision_email_mailbox(
        self, *,
        tenant_id: str,
        user_full_name: str,
        primary_smtp: str,
        idempotency_key: str,
    ) -> AdapterResponse[MailboxResult]: ...

    # ── Network / VPN ────────────────────────────────────────────────────
    async def grant_vpn_access(
        self, *,
        tenant_id: str,
        user_id: str,
        idempotency_key: str,
    ) -> AdapterResponse[VpnGrantResult]: ...

    # ── AD groups ────────────────────────────────────────────────────────
    async def add_to_groups(
        self, *,
        tenant_id: str,
        user_id: str,
        groups: tuple[str, ...],
        idempotency_key: str,
    ) -> AdapterResponse[GroupMembershipResult]: ...

    # ── License management ───────────────────────────────────────────────
    async def assign_software_license(
        self, *,
        tenant_id: str,
        user_id: str,
        product: str,
        idempotency_key: str,
    ) -> AdapterResponse[LicenseResult]: ...

    # ── Procurement ──────────────────────────────────────────────────────
    async def order_hardware_asset(
        self, *,
        tenant_id: str,
        asset_type: str,
        model_preferred: str,
        deliver_to: str,
        idempotency_key: str,
    ) -> AdapterResponse[ProcurementOrderResult]: ...

    # ── Notification ─────────────────────────────────────────────────────
    async def notify_milestone(
        self, *,
        tenant_id: str,
        recipient_user_id: str,
        message: str,
        level: str,                    # 'info' | 'warn' | 'error'
        idempotency_key: str,
    ) -> AdapterResponse[GenericTaskResult]: ...

    # ── Saga compensation ────────────────────────────────────────────────
    async def disable_directory_account(
        self, *,
        tenant_id: str,
        account_id: str,
        idempotency_key: str,
    ) -> AdapterResponse[GenericTaskResult]: ...

    async def deprovision_email_mailbox(
        self, *,
        tenant_id: str,
        mailbox_id: str,
        idempotency_key: str,
    ) -> AdapterResponse[GenericTaskResult]: ...

    async def revoke_vpn_access(
        self, *,
        tenant_id: str,
        config_id: str,
        idempotency_key: str,
    ) -> AdapterResponse[GenericTaskResult]: ...

    async def release_software_license(
        self, *,
        tenant_id: str,
        license_id: str,
        idempotency_key: str,
    ) -> AdapterResponse[GenericTaskResult]: ...

    async def cancel_hardware_order(
        self, *,
        tenant_id: str,
        po_id: str,
        idempotency_key: str,
    ) -> AdapterResponse[GenericTaskResult]: ...
