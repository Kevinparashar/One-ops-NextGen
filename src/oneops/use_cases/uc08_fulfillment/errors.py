"""Typed exception hierarchy for UC-8 (rule §2.7: no silent failures).

Every error path the handler can take has a typed class. Catching code
discriminates by exception type, not by parsing error strings. Each error
extends `OneOpsError` so the API boundary maps it to a deterministic HTTP
status code.

These typed errors are raised by the fulfilment engine and surfaced through
the conversational boundary (UC-8 is chat-only as of 2026-06-09 — the bespoke
REST routes were removed). Each extends `OneOpsError` so any boundary maps it
deterministically:

    CatalogItemNotFoundError · RequestNotFoundError · RequestItemAlreadyExistsError
    InvalidPlanError · DuplicateRequestError · AdapterInvocationError
    FulfillmentPersistenceError · FulfillmentTimeoutError

Every raise site logs a structured event (`uc08.error.<class>`) so post-
mortems don't depend on string matching.
"""
from __future__ import annotations

from oneops.errors import OneOpsError


class FulfillmentError(OneOpsError):
    """Base for any UC-8-layer fault. Never raised directly."""

    code = "UC08_ERROR"


# ── Lookup failures (404) ────────────────────────────────────────────────────


class CatalogItemNotFoundError(FulfillmentError):
    """Caller named a `catalog_item_id` that doesn't exist for this tenant."""

    code = "UC08_CATALOG_ITEM_NOT_FOUND"


class RequestNotFoundError(FulfillmentError):
    """Caller named a `request_id` (parent SR) that doesn't exist."""

    code = "UC08_REQUEST_NOT_FOUND"


class RequestItemNotFoundError(FulfillmentError):
    """Status query references a `ritm_id` we don't have."""

    code = "UC08_RITM_NOT_FOUND"


# ── Conflict failures (409) ──────────────────────────────────────────────────


class RequestItemAlreadyExistsError(FulfillmentError):
    """Two creates collided on the same idempotency_key.

    The caller's retry SHOULD use the existing ritm_id (which is included
    in the error payload) rather than insert a duplicate."""

    code = "UC08_RITM_DUPLICATE"


class DuplicateRequestError(FulfillmentError):
    """A live RITM already exists for the same (requested_for, catalog_item)
    inside the lookback window (DOC-09 §UC-8 scenario 8.7).

    Caller receives the existing ritm_id and a soft block — no new RITM
    is created."""

    code = "UC08_DUPLICATE_REQUEST"


# ── Validation failures (422) ───────────────────────────────────────────────


class InvalidPlanError(FulfillmentError):
    """The decomposition step produced a plan that violates structural
    invariants (cycle, missing dep, empty task list, etc.). This is a
    BUG — the LLM or the template loader emitted something invalid.

    Production-grade: we never silently coerce; we raise so the audit
    record can be inspected."""

    code = "UC08_INVALID_PLAN"


class InvalidTemplateError(FulfillmentError):
    """A catalog template fails CatalogTemplate validation. Config bug —
    the catalog item row in itsm.catalog_item is malformed."""

    code = "UC08_INVALID_TEMPLATE"


# ── External-system failures ────────────────────────────────────────────────


class AdapterInvocationError(FulfillmentError):
    """An integration adapter raised an UNEXPECTED exception (network
    error, programmer bug). This is distinct from a structured
    AdapterResponse(success=False), which is handled in-band.

    Mapped to HTTP 502 — the platform is fine; an upstream is broken."""

    code = "UC08_ADAPTER_FAILURE"


class FulfillmentTimeoutError(FulfillmentError):
    """The whole fulfillment workflow exceeded its deadline. Distinct
    from a per-adapter TIMEOUT (which is handled in-band via
    AdapterErrorClass)."""

    code = "UC08_TIMEOUT"


# ── Persistence failures (500) ──────────────────────────────────────────────


class FulfillmentPersistenceError(FulfillmentError):
    """A DB write failed in a way that we can't recover from. The handler
    rolls back its open transaction before raising."""

    code = "UC08_PERSISTENCE_FAILURE"


__all__ = [
    "FulfillmentError",
    "CatalogItemNotFoundError",
    "RequestNotFoundError",
    "RequestItemNotFoundError",
    "RequestItemAlreadyExistsError",
    "DuplicateRequestError",
    "InvalidPlanError",
    "InvalidTemplateError",
    "AdapterInvocationError",
    "FulfillmentTimeoutError",
    "FulfillmentPersistenceError",
]
