"""Registry-resolved tool handlers for UC-8.

The strings in `registries/v2/tools/uc08_fulfillment/*.json` (`handler_ref`)
point at the callables in this module. The dispatcher/runner resolves them
at boot.

Production-grade properties for every handler:
  • Pydantic input validation at the boundary (rule §2.7).
  • Tenant binding from the context dict (never from request arguments).
  • Structured OTel span.
  • Typed return — never bare dict.

Tool handlers for **active orchestration** (create_directory_account,
provision_email_mailbox, etc.) land in Phase 6 alongside the LangGraph
graph. This module hosts the **read-only** and **entry-point** tools we
can ship and test in Phase 5.

Phase 5 ships:
  • fulfill_request          — the catalog-fulfillment entry point
  • get_fulfillment_status   — status-query (chat + UI)
  • load_catalog_template    — read-only catalog lookup
  • check_request_duplicate  — duplicate-detection helper

Phase 6 adds (alongside the orchestrator):
  • decompose_fulfillment_request — LLM-driven plan (8.8 fallback)
  • create_directory_account, provision_email_mailbox, …
  • request_human_approval, rollback_completed_steps, …
"""
from __future__ import annotations

from typing import Any

import structlog
from opentelemetry import trace

from oneops.use_cases.uc08_fulfillment import core as _core
from oneops.use_cases.uc08_fulfillment import db as _db
from oneops.use_cases.uc08_fulfillment.contracts import (
    FulfillmentRequest,
    TriggerType,
)
from oneops.use_cases.uc08_fulfillment.errors import (
    CatalogItemNotFoundError,
    DuplicateRequestError,
    RequestItemNotFoundError,
    RequestNotFoundError,
)

# Telemetry/HTTP literals → constants (sonar S1192).
_ONEOPS_TENANT_ID = "oneops.tenant_id"

_log = structlog.get_logger("oneops.uc08.tools")
_tracer = trace.get_tracer("oneops.uc08.tools")

# Optional connection-provider injection — the API factory sets this so
# tests can override per-test (matches UC-5 pattern in `set_*` factories).
_connection_provider: _db.ConnectionProvider | None = None


def set_connection_provider(cp: _db.ConnectionProvider | None) -> None:
    """Wire a connection provider. None ⇒ default (per-call asyncpg.connect)."""
    global _connection_provider
    _connection_provider = cp


def _principal_from_context(context: dict[str, Any]) -> tuple[str, str, str]:
    """Extract (tenant_id, user_id, role) from the tool-runner context.
    Mirrors UC-2 / UC-5 boundary discipline."""
    tenant_id = str(context.get("tenant_id") or "").strip()
    user_id = str(context.get("user_id") or "").strip()
    role = str(context.get("role") or "").strip()
    if not tenant_id:
        raise ValueError("uc08 tool: missing tenant_id in context")
    if not user_id:
        raise ValueError("uc08 tool: missing user_id in context")
    return tenant_id, user_id, role


# ── Entry point — fulfill_request ──────────────────────────────────────────


async def fulfill_request(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Catalog item fulfillment entry point.

    Both the portal route POST /api/uc08/fulfill and the chat path land
    here. Identical results regardless of trigger (rule §F).

    Arguments expected:
      request_id        — parent SR id (FK)
      catalog_item_id   — catalog template id
      variables         — dict of form field values (default: {})
      requested_for     — user id this fulfilment is for (default: caller)
      quantity          — int (default 1)
      idempotency_key   — caller-supplied retry token (default None)

    Context expected (set by the tool runner):
      tenant_id, user_id, role, trace_id (optional)
      trigger_type — 'portal' | 'chat' (defaults to 'chat')
    """
    tenant_id, user_id, _ = _principal_from_context(context)
    trace_id = context.get("trace_id")
    trigger = context.get("trigger_type") or "chat"

    with _tracer.start_as_current_span(
        "uc08.tool.fulfill_request",
        attributes={
            _ONEOPS_TENANT_ID: tenant_id,
            "oneops.user_id": user_id,
            "uc08.trigger_type": trigger,
        },
    ):
        try:
            req = FulfillmentRequest(
                tenant_id=tenant_id,
                request_id=str(arguments.get("request_id") or "").strip(),
                catalog_item_id=str(arguments.get("catalog_item_id") or "").strip(),
                variables=dict(arguments.get("variables") or {}),
                requested_for=str(
                    arguments.get("requested_for") or user_id).strip(),
                opened_by=user_id,
                quantity=int(arguments.get("quantity") or 1),
                idempotency_key=arguments.get("idempotency_key"),
                trigger_type=TriggerType(trigger),
            )
        except (TypeError, ValueError) as exc:
            return {
                "ok": False,
                "error_code": "UC08_BAD_REQUEST",
                "error": f"invalid request: {exc}",
                "display_text": f"I couldn't start that fulfillment: {exc}",
            }

        try:
            outcome = await _core.fulfill_request(
                req,
                connection_provider=_connection_provider,
                trace_id=trace_id,
                actor=user_id,
            )
        except DuplicateRequestError as exc:
            _log.info("uc08.tool.duplicate_blocked",
                      tenant_id=tenant_id, error=str(exc))
            return {
                "ok": False,
                "error_code": exc.code,
                "error": str(exc),
                "display_text": (
                    f"An open fulfillment request already exists for this "
                    f"user + catalog item. {exc}"
                ),
            }
        except CatalogItemNotFoundError as exc:
            return {
                "ok": False,
                "error_code": exc.code,
                "error": str(exc),
                "display_text": (
                    f"I don't recognise that catalog item "
                    f"({req.catalog_item_id})."
                ),
            }
        except RequestNotFoundError as exc:
            return {
                "ok": False,
                "error_code": exc.code,
                "error": str(exc),
                "display_text": (
                    f"That Service Request id doesn't exist "
                    f"({req.request_id})."
                ),
            }
        return outcome.model_dump(mode="json")


# ── Read-only — get_fulfillment_status (DOC-09 §UC-8 8.6) ────────────────


async def get_fulfillment_status(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Live status for one RITM."""
    tenant_id, _, _ = _principal_from_context(context)
    ritm_id = str(arguments.get("ritm_id") or "").strip()
    if not ritm_id:
        return {
            "ok": False,
            "error_code": "UC08_BAD_REQUEST",
            "error": "ritm_id is required",
        }
    with _tracer.start_as_current_span(
        "uc08.tool.get_fulfillment_status",
        attributes={_ONEOPS_TENANT_ID: tenant_id, "uc08.ritm_id": ritm_id},
    ):
        cp = _connection_provider or _db.default_connection_provider
        conn = await cp()
        try:
            try:
                status = await _db.get_status(
                    tenant_id=tenant_id, ritm_id=ritm_id, conn=conn,
                )
            except RequestItemNotFoundError as exc:
                return {
                    "ok": False,
                    "error_code": exc.code,
                    "error": str(exc),
                    "display_text": f"No fulfillment record found for {ritm_id}.",
                }
            done = status.tasks_by_state.get("done", 0)
            in_prog = status.tasks_by_state.get("in_progress", 0)
            pending = (
                status.tasks_by_state.get("pending", 0)
                + status.tasks_by_state.get("ready", 0)
            )
            failed = status.tasks_by_state.get("failed", 0)
            blocked = status.tasks_by_state.get("blocked", 0)
            display = (
                f"{status.ritm_id} ({status.catalog_item_id}) — "
                f"state={status.state.value}. "
                f"Tasks: {done}/{status.tasks_total} done, "
                f"{in_prog} in progress, {pending} pending"
            )
            if failed:
                display += f", {failed} failed"
            if blocked:
                display += f", {blocked} blocked"
            if status.pending_approvals:
                display += f". Awaiting approval: {', '.join(status.pending_approvals)}"
            display += "."
            payload = status.model_dump(mode="json")
            payload["display_text"] = display
            payload["ok"] = True
            return payload
        finally:
            await conn.close()


# ── Read-only — load_catalog_template ────────────────────────────────────


async def load_catalog_template(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Catalog template lookup."""
    tenant_id, _, _ = _principal_from_context(context)
    catalog_item_id = str(arguments.get("catalog_item_id") or "").strip()
    if not catalog_item_id:
        return {"ok": False, "error_code": "UC08_BAD_REQUEST",
                "error": "catalog_item_id is required"}
    with _tracer.start_as_current_span(
        "uc08.tool.load_catalog_template",
        attributes={_ONEOPS_TENANT_ID: tenant_id,
                    "uc08.catalog_item_id": catalog_item_id},
    ):
        cp = _connection_provider or _db.default_connection_provider
        conn = await cp()
        try:
            try:
                tmpl = await _db.load_catalog_template(
                    tenant_id=tenant_id,
                    catalog_item_id=catalog_item_id, conn=conn,
                )
            except CatalogItemNotFoundError as exc:
                return {"ok": False, "error_code": exc.code, "error": str(exc)}
            payload = tmpl.model_dump(mode="json")
            payload["ok"] = True
            return payload
        finally:
            await conn.close()


# ── Read-only — check_request_duplicate (DOC-09 §UC-8 8.7) ──────────────


async def check_request_duplicate(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Detect open RITM for (requested_for, catalog_item)."""
    tenant_id, _, _ = _principal_from_context(context)
    requested_for = str(arguments.get("requested_for_user_id") or "").strip()
    catalog_item_id = str(arguments.get("catalog_item_id") or "").strip()
    lookback_days = int(arguments.get("lookback_days") or 30)
    if not requested_for or not catalog_item_id:
        return {"ok": False, "error_code": "UC08_BAD_REQUEST",
                "error": "requested_for_user_id and catalog_item_id required"}
    with _tracer.start_as_current_span(
        "uc08.tool.check_request_duplicate",
        attributes={
            _ONEOPS_TENANT_ID: tenant_id,
            "uc08.requested_for": requested_for,
            "uc08.catalog_item_id": catalog_item_id,
        },
    ):
        cp = _connection_provider or _db.default_connection_provider
        conn = await cp()
        try:
            existing = await _db.find_open_duplicate(
                tenant_id=tenant_id, requested_for=requested_for,
                catalog_item_id=catalog_item_id,
                lookback_days=lookback_days, conn=conn,
            )
            return {
                "ok": True,
                "duplicate_found": existing is not None,
                "existing_ritm_id": existing,
            }
        finally:
            await conn.close()


__all__ = [
    "set_connection_provider",
    "fulfill_request",
    "get_fulfillment_status",
    "load_catalog_template",
    "check_request_duplicate",
]
