"""UC-1 tool handlers — built to the Component Spec (docs/architecture/COMPONENT_SPEC.md).

`get_ticket_details` returns the field snapshot of one ITSM work record.

Spec conformance:
  * C8  — structured output: returns an `EntityDetailsResult` (explicit
          outcome + record view + message), never a free-form dict.
  * C10 — deterministic: a data fetch + a policy filter; no LLM involved.
  * C12 — no static catalogs: field exposure is driven by the registry field
          policy (data), not a hardcoded redaction list in code.
  * C13 — tenant-scoped: tenant_id is taken from the request envelope, never
          from user text; the data layer scopes the read to that tenant.
  * C17 — no silent failure: every path returns an explicit outcome and a
          human-readable message.
  * C21 — pluggable backend: data access goes through `TicketStore`
          (in-memory default, live backend env-gated).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from oneops.observability import get_logger
from oneops.use_cases._shared.field_policy import get_field_policy
from oneops.use_cases._shared.ticket_store import get_ticket_store

_log = get_logger("oneops.use_cases.uc01.handlers")

# Values that carry no information for the model — dropped to protect the
# attention budget (Component Spec C9). A deterministic rule, not a catalogue.
_EMPTY: tuple[Any, ...] = (None, "", [], {})


@dataclass(frozen=True)
class EntityDetailsResult:
    """Structured output of `get_ticket_details` (Component Spec C8).

    `outcome` is the contract's status enum; `record` is the exposable field
    snapshot (None unless `outcome == "found"`); `message` is always set and is
    safe to surface to the user (Component Spec C17)."""

    outcome: str          # "found" | "not_found" | "invalid_request"
    ticket_id: str
    service_id: str
    message: str
    record: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "ticket_id": self.ticket_id,
            "service_id": self.service_id,
            "message": self.message,
            "record": self.record,
        }


def _result(
    outcome: str, ticket_id: str, service_id: str, message: str,
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return EntityDetailsResult(
        outcome=outcome, ticket_id=ticket_id, service_id=service_id,
        message=message, record=record).to_dict()


async def get_ticket_details(
    arguments: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Fetch the field snapshot of one ITSM work record by id.

    `ticket_id` and `service_id` come from the tool arguments; `tenant_id` is
    bound from the request envelope (`context`), never from user text."""
    ticket_id = str(arguments.get("ticket_id") or "").strip()
    service_id = str(arguments.get("service_id") or "").strip()
    tenant_id = str(context.get("tenant_id") or "").strip()
    role = str(context.get("role") or "").strip()

    if not ticket_id:
        return _result("invalid_request", ticket_id, service_id,
                       "A ticket id is required to fetch record details.")
    if not service_id:
        return _result("invalid_request", ticket_id, service_id,
                       "A service module (incident, request, problem, change, "
                       "asset, cmdb_ci) is required.")
    if not tenant_id:
        return _result("invalid_request", ticket_id, service_id,
                       "No tenant scope was supplied for this request.")

    record = await get_ticket_store().get(
        ticket_id=ticket_id, service_id=service_id, tenant_id=tenant_id)

    if record is None:
        _log.info("uc01.get_ticket_details.not_found",
                  ticket_id=ticket_id, service_id=service_id)
        return _result("not_found", ticket_id, service_id,
                       f"No {service_id} with id {ticket_id} was found for "
                       f"this tenant.")

    # Exposure is policy-driven (Component Spec C12): expose only fields whose
    # registry classification ranks below the withhold threshold, then drop
    # internal nested items (e.g. private work_notes) the caller's role may not
    # see, then drop empties to protect the attention budget (C9).
    policy = get_field_policy()
    exposed = policy.expose(record)
    visible = policy.redact_internal_content(exposed, role)
    snapshot = {k: v for k, v in visible.items() if v not in _EMPTY}
    return _result("found", ticket_id, service_id,
                   f"Retrieved {service_id} record {ticket_id}.", snapshot)


__all__ = ["EntityDetailsResult", "get_ticket_details"]
