"""Unit tests for the per-table queue selection."""
from __future__ import annotations

import pytest

from oneops.use_cases.uc05_triage.queue import (
    CLOSED_STATUSES,
    INCIDENT_TRIAGE_FIELDS,
    REQUEST_TRIAGE_FIELDS,
    filter_queue,
    is_in_queue,
    missing_uc5_fields,
    triage_fields_for,
)

# ── Field whitelists ─────────────────────────────────────────────────────────

class TestWhitelists:
    def test_incident_owns_7_fields(self) -> None:
        # service_name dropped 2026-05-29 — not AI-fillable (operator-supplied)
        assert len(INCIDENT_TRIAGE_FIELDS) == 7
        assert "category" in INCIDENT_TRIAGE_FIELDS
        assert "subcategory" in INCIDENT_TRIAGE_FIELDS
        assert "service_name" not in INCIDENT_TRIAGE_FIELDS
        assert "impact" in INCIDENT_TRIAGE_FIELDS
        assert "urgency" in INCIDENT_TRIAGE_FIELDS
        assert "priority" in INCIDENT_TRIAGE_FIELDS
        assert "assignment_group" in INCIDENT_TRIAGE_FIELDS
        assert "assigned_to" in INCIDENT_TRIAGE_FIELDS

    def test_request_owns_4_fields(self) -> None:
        # catalog_item_id dropped 2026-05-29 — not AI-fillable (operator-supplied)
        assert len(REQUEST_TRIAGE_FIELDS) == 4
        assert "catalog_item_id" not in REQUEST_TRIAGE_FIELDS
        assert "subcategory" not in REQUEST_TRIAGE_FIELDS
        assert "service_name" not in REQUEST_TRIAGE_FIELDS
        assert "impact" not in REQUEST_TRIAGE_FIELDS
        assert "urgency" not in REQUEST_TRIAGE_FIELDS

    def test_unknown_service_raises(self) -> None:
        with pytest.raises(ValueError):
            triage_fields_for("problem")


# ── missing_uc5_fields ───────────────────────────────────────────────────────

class TestMissingFields:
    def test_incident_all_null_returns_7(self) -> None:
        row = {f: None for f in INCIDENT_TRIAGE_FIELDS}
        assert set(missing_uc5_fields(row, "incident")) == set(INCIDENT_TRIAGE_FIELDS)
        assert len(missing_uc5_fields(row, "incident")) == 7

    def test_incident_one_null_returns_one(self) -> None:
        row = {f: "x" for f in INCIDENT_TRIAGE_FIELDS}
        row["category"] = None
        assert missing_uc5_fields(row, "incident") == ["category"]

    def test_incident_all_filled_returns_empty(self) -> None:
        row = {f: "x" for f in INCIDENT_TRIAGE_FIELDS}
        assert missing_uc5_fields(row, "incident") == []

    def test_empty_string_counts_as_missing(self) -> None:
        row = {f: "x" for f in INCIDENT_TRIAGE_FIELDS}
        row["category"] = ""
        row["subcategory"] = "   "
        assert "category" in missing_uc5_fields(row, "incident")
        assert "subcategory" in missing_uc5_fields(row, "incident")

    def test_request_all_null_returns_4(self) -> None:
        row = {f: None for f in REQUEST_TRIAGE_FIELDS}
        assert len(missing_uc5_fields(row, "request")) == 4


# ── is_in_queue ─────────────────────────────────────────────────────────────

class TestIsInQueue:
    def _full(self, service_id: str) -> dict:
        return {f: "x" for f in triage_fields_for(service_id)} | {"status": "new"}

    def test_all_filled_not_in_queue(self) -> None:
        assert is_in_queue(self._full("incident"), "incident") is False

    def test_one_null_in_queue(self) -> None:
        row = self._full("incident")
        row["assigned_to"] = None
        assert is_in_queue(row, "incident") is True

    @pytest.mark.parametrize("status", sorted(CLOSED_STATUSES))
    def test_closed_status_excluded_even_with_null_fields(self, status) -> None:
        row = {f: None for f in INCIDENT_TRIAGE_FIELDS} | {"status": status}
        assert is_in_queue(row, "incident") is False

    def test_status_case_insensitive(self) -> None:
        row = {f: None for f in INCIDENT_TRIAGE_FIELDS} | {"status": "CLOSED"}
        assert is_in_queue(row, "incident") is False

    def test_new_status_with_some_filled_in_queue(self) -> None:
        row = {f: "x" for f in INCIDENT_TRIAGE_FIELDS}
        row["impact"] = None
        row["status"] = "new"
        assert is_in_queue(row, "incident") is True

    def test_unknown_status_treated_as_open(self) -> None:
        row = {f: None for f in INCIDENT_TRIAGE_FIELDS} | {"status": "in_progress"}
        assert is_in_queue(row, "incident") is True


# ── filter_queue ─────────────────────────────────────────────────────────────

class TestFilterQueue:
    def test_mix(self) -> None:
        rows = [
            # in queue: some missing
            {"category": None, "subcategory": "x", "service_name": "x",
             "impact": "x", "urgency": "x", "priority": "x",
             "assignment_group": "x", "assigned_to": "x", "status": "new"},
            # not in queue: all filled
            {f: "x" for f in INCIDENT_TRIAGE_FIELDS} | {"status": "new"},
            # not in queue: closed
            {f: None for f in INCIDENT_TRIAGE_FIELDS} | {"status": "closed"},
        ]
        out = filter_queue(rows, "incident")
        assert len(out) == 1
