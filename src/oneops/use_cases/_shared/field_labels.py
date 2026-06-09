"""Human-readable field labels — registry-data-driven per ITSM service.

The UC-1 cross-service output contract requires `key_details` to be the
caller-visible fields of the record with **human-readable labels** AND
**human-readable values** — never raw JSON / ISO timestamps / boolean
primitives, never an operational-noise dump.

Field-order contract (matches user-facing spec):

  1. **State fields first** — Status, Priority, Severity, Impact, Urgency
     (what an operator wants to see at a glance).
  2. **Classification** — Category, Subcategory, Service.
  3. **People** — Reported By, Assigned To, Assignment Group, Owner.
  4. **Linked records** — Configuration Item, Linked CIs, Related Problem,
     Related Change, etc. (service-specific keys appear here).
  5. **Timing** — SLA Due, SLA Breached, Created At, Updated At, …
  6. **Description / title** — Title, Description (the long-form fields).
  7. **Conversational threads** — Work Notes, Comments, Attachments (rendered
     as bulleted readable lines, never raw JSON).

The mapping table is data; the function applies it. New services and new
fields are added here as a one-line entry, never as a new code path.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

# Generic labels — apply to every service when the field name matches.
# Order in the dict is the display order when no service-specific entry
# overrides it. Python 3.7+ dicts preserve insertion order. The order
# below is the operator-facing reading order: state → classification →
# people → linked → timing → long-form → threads.
_GENERIC: dict[str, str] = {
    # 1. state
    "status": "Status",
    "state": "State",
    "stage": "Stage",
    "priority": "Priority",
    "severity": "Severity",
    "impact": "Impact",
    "urgency": "Urgency",
    "approval_status": "Approval Status",
    # 2. classification
    "category": "Category",
    "subcategory": "Subcategory",
    "service_name": "Service",
    # 3. people
    "reported_by": "Reported By",
    "requested_by": "Requested By",
    "requested_for": "Requested For",
    "assigned_to": "Assigned To",
    "assignment_group": "Assignment Group",
    "owner": "Owner",
    "approved_by": "Approved By",
    # 5. timing
    "sla_due": "SLA Due",
    "sla_breached": "SLA Breached",
    "planned_start": "Planned Start",
    "planned_end": "Planned End",
    "actual_start": "Actual Start",
    "actual_end": "Actual End",
    "created_at": "Created At",
    "updated_at": "Updated At",
    "resolved_at": "Resolved At",
    "fulfilled_at": "Fulfilled At",
    "location": "Location",
    # 6. long-form
    "title": "Title",
    "description": "Description",
}

# Service-specific labels — keyed by service id. Each entry list is the
# DISPLAY ORDER within its slot (4 = linked records, 7 = threads). The
# numeric prefix (`4:` / `7:`) places the field in the right band.
#   "4:..."  → linked records / IDs (after timing/state)
#   "7:..."  → conversational threads (last, big blobs)
_BY_SERVICE: dict[str, tuple[tuple[str, str, str], ...]] = {
    # (field_name, label, slot)
    "incident": (
        ("incident_id",     "Incident ID",      "4"),
        ("ci_id",           "Configuration Item","4"),
        ("linked_ci_ids",   "Linked CIs",       "4"),
        ("related_problem", "Related Problem",  "4"),
        ("related_change",  "Related Change",   "4"),
        ("work_notes",      "Work Notes",       "7"),
        ("comments",        "Comments",         "7"),
        ("attachments",     "Attachments",      "7"),
    ),
    "request": (
        ("request_id",      "Request ID",       "4"),
        ("catalog_item_id", "Catalog Item",     "4"),
        ("ci_id",           "Configuration Item","4"),
        ("comments",        "Comments",         "7"),
    ),
    "problem": (
        ("problem_id",       "Problem ID",       "4"),
        ("root_cause",       "Root Cause",       "6"),
        ("workaround",       "Workaround",       "6"),
        ("known_error",      "Known Error",      "4"),
        ("related_incidents","Related Incidents","4"),
        ("related_changes",  "Related Changes",  "4"),
    ),
    "change": (
        ("change_id",       "Change ID",        "4"),
        ("change_type",     "Type",             "1"),
        ("risk_level",      "Risk Level",       "1"),
        ("affected_ci",     "Affected CIs",     "4"),
        ("related_problem", "Related Problem",  "4"),
    ),
    "asset": (
        ("asset_id",        "Asset ID",         "4"),
        ("asset_name",      "Asset Name",       "6"),
        ("asset_class",     "Class",            "2"),
        ("subtype",         "Subtype",          "2"),
        ("model",           "Model",            "2"),
        ("vendor",          "Vendor",           "2"),
        ("serial_number",   "Serial Number",    "4"),
        ("linked_ci",       "Linked CI",        "4"),
        ("purchase_date",   "Purchase Date",    "5"),
        ("warranty_expiry", "Warranty Expiry",  "5"),
    ),
    "cmdb_ci": (
        ("ci_id",           "CI ID",            "4"),
        ("ci_name",         "CI Name",          "6"),
        ("ci_type",         "CI Type",          "2"),
        ("environment",     "Environment",      "2"),
        ("criticality",     "Criticality",      "1"),
        ("relationships",   "Relationships",    "4"),
        ("attributes",      "Attributes",       "6"),
    ),
    "knowledge": (
        ("kb_id",           "Article ID",       "4"),
        ("title",           "Title",            "6"),
        ("summary",         "Summary",          "6"),
        ("category",        "Category",         "2"),
        ("audience",        "Audience",         "2"),
        ("tags",            "Tags",             "4"),
        ("views",           "Views",            "5"),
        ("helpful_votes",   "Helpful Votes",    "5"),
        ("related_ci_ids",  "Related CIs",      "4"),
        ("related_incidents","Related Incidents","4"),
        ("created_by",      "Author",           "3"),
    ),
}

# Fields hidden from key_details:
#   * Restricted by classification (tenant_id) — defence in depth.
#   * Internal vectors / timestamps the operator doesn't want to see.
#   * Long-form prose (title, description) — already in the Summary
#     paragraph the LLM generated; repeating them clutters Key Details.
# Version stamp for the render rules in this module. EVERY callsite that
# caches a `humanise_record(...)` output (UC-1 summary cache_aside, future
# UC-3/UC-5 key-details snapshots) MUST include this in its cache key so a
# change to `_HIDDEN`, `_LABELS`, or formatting rules auto-invalidates the
# old entries. Bump on every behavioural change to the renderer. Treat it
# like a database migration number.
#
# Changelog:
#   v1 — initial release
#   v2 — 2026-05-30 — hide search_tsv + content_hash_* (production leak fix)
#   v3 — 2026-06-01 — UC-1 summary render change (compact narrative + dated
#                     bullets; raw key_details list hidden in the UI). Bumped
#                     so every cached summary invalidates to the new format.
HUMANISE_RECORD_VERSION = "v3"


_HIDDEN: frozenset[str] = frozenset({
    # Tenant isolation marker — exposed via the response envelope, not
    # the field grid.
    "tenant_id",
    # Search-substrate columns: FTS vectors + per-chunk hashes that the
    # embedding-refresh worker uses for cache-gating. Operator UI never
    # benefits from seeing raw tsvectors or binary hashes — leaking them
    # makes the summary card look like a database dump.
    "content_tsv", "search_tsv",
    "content_hash", "content_hash_symptom", "content_hash_diagnosis",
    "content_hash_kb",
    # Embedding columns: large float arrays + per-row provenance.
    "embedding", "embedding_model", "embedding_version", "embedded_at",
    # Internal bookkeeping fields.
    "_updated_at",
    # Title + description ride in the narrative paragraph instead of the
    # key-details grid (long-form content has its own slot).
    "title", "description",
})

# Display-slot weight for each generic field. Used to interleave generic
# and service-specific fields by slot, not by source. Lower = earlier.
_GENERIC_SLOTS: dict[str, str] = {
    # 1: state
    "status": "1", "state": "1", "stage": "1", "priority": "1",
    "severity": "1", "impact": "1", "urgency": "1", "approval_status": "1",
    # 2: classification
    "category": "2", "subcategory": "2", "service_name": "2",
    # 3: people
    "reported_by": "3", "requested_by": "3", "requested_for": "3",
    "assigned_to": "3", "assignment_group": "3", "owner": "3",
    "approved_by": "3",
    # 5: timing
    "sla_due": "5", "sla_breached": "5",
    "planned_start": "5", "planned_end": "5",
    "actual_start": "5", "actual_end": "5",
    "created_at": "5", "updated_at": "5",
    "resolved_at": "5", "fulfilled_at": "5",
    "location": "3",
    # 6: long-form
    "title": "6", "description": "6",
}


# ── value formatting ────────────────────────────────────────────────────


# Split date-only vs datetime so each pattern stays simple (sonar S5843). Their
# union matches EXACTLY what the single combined ISO pattern did (the time part
# was just optional). Non-capturing groups — `.match()` is used only as a bool
# gate before datetime.fromisoformat (the authoritative parser).
_ISO_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:[Zz]|[+-]\d{2}:?\d{2})?$"
)


def _is_iso_dateish(value: str) -> bool:
    """True when `value` is an ISO date (YYYY-MM-DD) or datetime string."""
    return bool(_ISO_DATE_ONLY_RE.match(value) or _ISO_DATETIME_RE.match(value))


def _format_datetime(value: Any) -> str:
    """ISO date / datetime / `datetime` object → "Month Day, Year[ HH:MM UTC]".
    Falls back to `str(value)` on parse failure (never raises)."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        return value.strftime("%B %-d, %Y")
    elif isinstance(value, str) and _is_iso_dateish(value):
        try:
            normalised = value.replace("Z", "+00:00").replace("z", "+00:00")
            dt = datetime.fromisoformat(normalised)
        except (ValueError, TypeError):
            return value
    else:
        return str(value)
    has_time = (dt.hour, dt.minute, dt.second) != (0, 0, 0)
    if has_time:
        tz = " UTC" if dt.utcoffset() is not None else ""
        try:
            return dt.strftime("%B %-d, %Y %H:%M") + tz
        except ValueError:                                # Windows fallback
            return dt.strftime(f"%B {dt.day}, %Y %H:%M") + tz
    try:
        return dt.strftime("%B %-d, %Y")
    except ValueError:
        return dt.strftime(f"%B {dt.day}, %Y")


def _is_datelike_field(field: str) -> bool:
    return (
        field.endswith(("_at", "_due", "_start", "_end")) or field == "purchase_date" or field == "warranty_expiry"
    )


def _format_bool(value: bool) -> str:
    return "Yes" if value else "No"


def _format_size(num_bytes: int) -> str:
    """Bytes → human (KB / MB). 0/None → empty."""
    if not num_bytes:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.0f} {unit}" if unit != "B" else f"{num_bytes} B"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def _format_work_note(item: dict[str, Any]) -> str:
    """One work_note dict → "[date by author] text"."""
    ts = item.get("timestamp")
    when = _format_datetime(ts) if ts else ""
    who = item.get("author") or "?"
    role = item.get("author_role")
    role_chip = f" ({role})" if role and role != "agent" else ""
    text = (item.get("text") or "").strip()
    visibility = "" if item.get("is_public", False) else " · internal"
    head = f"{when} · {who}{role_chip}{visibility}".strip(" ·")
    if not head:
        return text
    return f"{head} — {text}" if text else head


def _format_comment(item: dict[str, Any]) -> str:
    """One comment dict → similar shape but no internal-visibility chip
    (every comment is customer-visible by definition)."""
    ts = item.get("timestamp")
    when = _format_datetime(ts) if ts else ""
    who = item.get("author") or "?"
    role = item.get("author_role")
    role_chip = f" ({role})" if role else ""
    text = (item.get("text") or "").strip()
    head = f"{when} · {who}{role_chip}".strip(" ·")
    if not head:
        return text
    return f"{head} — {text}" if text else head


def _format_attachment(item: dict[str, Any]) -> str:
    """One attachment dict → "filename (size, type) uploaded date"."""
    name = item.get("name") or item.get("attachment_id") or "(unnamed)"
    size = _format_size(int(item.get("size_bytes") or 0))
    mime = item.get("mime_type") or ""
    uploaded = item.get("uploaded_at")
    parts = [name]
    extras = [p for p in (size, mime) if p]
    if extras:
        parts.append(f"({', '.join(extras)})")
    if uploaded:
        parts.append(f"uploaded {_format_datetime(uploaded)}")
    return " ".join(parts)


# Per-field formatters for list-of-dict fields (`work_notes`, `comments`,
# `attachments`). Each maps one raw dict to a clean string. Registry-shape:
# adding a new conversational-thread field is one entry here.
_LIST_OF_DICT_FORMATTERS: dict[str, Any] = {
    "work_notes":  _format_work_note,
    "comments":    _format_comment,
    "attachments": _format_attachment,
}


def _format_value(field: str, value: Any) -> Any:
    """Apply the right value-shape transformation for `field`."""
    if value is None:
        return None
    if isinstance(value, bool):
        return _format_bool(value)
    if isinstance(value, (date, datetime)):
        return _format_datetime(value)
    if isinstance(value, str) and _is_datelike_field(field):
        return _format_datetime(value)
    formatter = _LIST_OF_DICT_FORMATTERS.get(field)
    if formatter is not None and isinstance(value, list):
        formatted = [formatter(item) for item in value
                     if isinstance(item, dict)]
        return [s for s in formatted if s]
    return value


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    return bool(isinstance(value, (str, list, tuple, dict, set)) and len(value) == 0)


def _label_for(field: str, service_id: str) -> str:
    for k, label, _slot in _BY_SERVICE.get(service_id, ()):
        if k == field:
            return label
    if field in _GENERIC:
        return _GENERIC[field]
    return _auto_label(field)


def _slot_for(field: str, service_id: str) -> str:
    """Return the display slot for a field (numeric string, lower = earlier).
    `_GENERIC_SLOTS` wins over service-specific service slots; service entries
    not in `_GENERIC_SLOTS` carry their declared slot."""
    if field in _GENERIC_SLOTS:
        return _GENERIC_SLOTS[field]
    for k, _label, slot in _BY_SERVICE.get(service_id, ()):
        if k == field:
            return slot
    return "8"                                      # unknown → tail


def _auto_label(field: str) -> str:
    parts = re.split(r"[_\s]+", str(field))
    return " ".join(
        "ID" if p.lower() == "id" else p.capitalize()
        for p in parts if p
    )


def _detect_service(record: dict[str, Any]) -> str:
    if "incident_id" in record: return "incident"
    if "request_id"  in record: return "request"
    if "problem_id"  in record: return "problem"
    if "change_id"   in record: return "change"
    if "asset_id"    in record: return "asset"
    if "ci_id"       in record and "ci_name" in record: return "cmdb_ci"
    if "kb_id"       in record: return "knowledge"
    return ""


def humanise_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return an ordered dict of `Label → formatted-value` for one ITSM
    record. Order is operationally meaningful:
      1. State (status, priority, severity, impact, urgency)
      2. Classification (category, subcategory, service)
      3. People (reported by, assigned to, group, owner)
      4. Linked records (CI, related problem/change, etc.)
      5. Timing (SLA, created/updated)
      6. Long-form (title, description, root cause, summary)
      7. Conversational threads (work notes, comments, attachments) — last

    All values are pre-formatted into operator-friendly shapes: dates as
    "Month Day, Year[ HH:MM UTC]", booleans as Yes/No, list-of-dicts
    (work_notes / comments / attachments) as lists of human-readable
    strings."""
    service_id = _detect_service(record)
    candidates: list[tuple[str, str, str, Any]] = []   # (slot, sub-order, label, value)
    # Enumerate every field in the record (except hidden + empties);
    # assign each a (slot, sub-order) so the final sort is stable.
    service_field_order = {
        k: i for i, (k, _, _) in enumerate(_BY_SERVICE.get(service_id, ()))
    }
    generic_field_order = {k: i for i, k in enumerate(_GENERIC)}
    for field, raw_value in record.items():
        if field in _HIDDEN:
            continue
        if field.startswith("_"):
            continue
        formatted = _format_value(field, raw_value)
        if _is_empty(formatted):
            continue
        label = _label_for(field, service_id)
        slot = _slot_for(field, service_id)
        # Sub-order: within a slot, generic-fields preserve their _GENERIC
        # order; service-specific fields use their declared order.
        sub_order = f"{service_field_order.get(field, 999):03d}_{generic_field_order.get(field, 999):03d}"
        candidates.append((slot, sub_order, label, formatted))
    candidates.sort(key=lambda x: (x[0], x[1]))
    return {label: value for _slot, _sub, label, value in candidates}


__all__ = ["humanise_record", "HUMANISE_RECORD_VERSION"]
