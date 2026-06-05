"""UC-5 triage queue selection — pure functions, no I/O.

Per-table whitelist of fields UC-5 owns (locked 2026-05-29):
  incident: 8 fields
  request:  5 fields (request schema lacks subcategory/service/impact/urgency)

A row belongs in the triage queue iff:
  • status NOT in CLOSED_STATUSES
  • at least one UC-5-owned field is NULL or empty
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

INCIDENT_TRIAGE_FIELDS: tuple[str, ...] = (
    "category", "subcategory",
    "impact", "urgency", "priority",
    "assignment_group", "assigned_to",
)
"""7 fields UC-5 fills on itsm.incident. service_name is operator-supplied
at ticket-creation time — UC-5 tools do not infer it."""

REQUEST_TRIAGE_FIELDS: tuple[str, ...] = (
    "category",
    "priority", "assignment_group", "assigned_to",
)
"""4 fields UC-5 fills on itsm.request. catalog_item_id is operator-supplied
at ticket-creation time — UC-5 tools do not infer it."""

CLOSED_STATUSES: frozenset[str] = frozenset({"closed", "resolved", "cancelled"})
"""Statuses that take a ticket out of triage scope regardless of NULL fields."""


def triage_fields_for(service_id: str) -> tuple[str, ...]:
    """Return the whitelist of UC-5-owned fields for a service.

    These are the GATING fields — a ticket is "in the triage queue" while any
    of them is NULL (`missing_uc5_fields`)."""
    if service_id == "incident":
        return INCIDENT_TRIAGE_FIELDS
    if service_id == "request":
        return REQUEST_TRIAGE_FIELDS
    raise ValueError(f"unsupported service_id: {service_id!r}")


def writable_fields_for(service_id: str) -> tuple[str, ...]:
    """Columns UC-5 may WRITE on apply: the gating triage fields plus the
    `ci_id` enrichment (Step 5 — present on both itsm.incident and
    itsm.request, but NOT a queue-gating field). This is the single source of
    truth for the apply whitelist (both the JSON and Postgres stores) and for
    the proposal→final_values projection."""
    return (*triage_fields_for(service_id), "ci_id")


def missing_uc5_fields(row: Mapping[str, Any], service_id: str) -> list[str]:
    """Return the list of UC-5-owned fields that are NULL or empty on this row."""
    out: list[str] = []
    for f in triage_fields_for(service_id):
        v = row.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            out.append(f)
    return out


def is_in_queue(row: Mapping[str, Any], service_id: str) -> bool:
    """Queue membership rule — closed tickets excluded; otherwise needs ≥1 NULL."""
    status = (row.get("status") or "").strip().lower()
    if status in CLOSED_STATUSES:
        return False
    return len(missing_uc5_fields(row, service_id)) > 0


def filter_queue(
    rows: list[Mapping[str, Any]], service_id: str
) -> list[Mapping[str, Any]]:
    """Filter rows to those that belong in the triage queue."""
    return [r for r in rows if is_in_queue(r, service_id)]
