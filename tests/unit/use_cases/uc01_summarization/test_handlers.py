"""UC-1 `get_ticket_details` handler — built to the Component Spec.

Verifies spec conformance: structured output (C8), schema-driven field
exposure (C12), tenant scoping (C13), and no silent failure (C17).
"""
from __future__ import annotations

import pytest

from oneops.use_cases._shared.ticket_store import (
    InMemoryTicketStore,
    set_ticket_store,
)
from oneops.use_cases.uc01_summarization.handlers import get_ticket_details

_RESULT_KEYS = {"outcome", "ticket_id", "service_id", "message", "record"}


@pytest.fixture
def store() -> InMemoryTicketStore:
    s = InMemoryTicketStore()
    s.seed(
        ticket_id="INC0048213", service_id="incident", tenant_id="tenant-a",
        title="VPN drops every few minutes",
        description="User reports the VPN tunnel resets repeatedly.",
        status="in_progress", priority="P2",
        assignment_group="GRP-NETOPS", assigned_to="USR00003",
        sla_breached=False,                  # boolean False — a real value
        helpful_votes=0,                     # integer 0 — a real value
        notes="",                            # empty — dropped
        work_notes=[
            {"note_id": "WN1", "is_public": True, "text": "public update"},
            {"note_id": "WN2", "is_public": False, "text": "internal triage note"},
        ],
    )
    set_ticket_store(s)
    return s


# ── C8 — structured output ────────────────────────────────────────────────


async def test_output_has_the_declared_structured_shape(store):
    out = await get_ticket_details(
        {"ticket_id": "INC0048213", "service_id": "incident"},
        {"tenant_id": "tenant-a"})
    assert set(out) == _RESULT_KEYS          # exact contract, no stray keys
    assert out["outcome"] == "found"
    assert out["message"]                    # always present


async def test_found_record_carries_the_field_snapshot(store):
    out = await get_ticket_details(
        {"ticket_id": "INC0048213", "service_id": "incident"},
        {"tenant_id": "tenant-a"})
    rec = out["record"]
    assert rec["title"] == "VPN drops every few minutes"
    assert rec["status"] == "in_progress"
    assert rec["sla_breached"] is False      # False kept — it is information
    assert rec["helpful_votes"] == 0         # 0 kept — it is information


async def test_empty_fields_are_dropped_for_attention_budget(store):
    out = await get_ticket_details(
        {"ticket_id": "INC0048213", "service_id": "incident"},
        {"tenant_id": "tenant-a"})
    assert "notes" not in out["record"]      # was "" — dropped


# ── C12 — schema-driven field exposure (no hardcoded redaction list) ──────


async def test_restricted_field_is_withheld_by_policy(store):
    # tenant_id is classified 'restricted' in registries/v2/field_policy.json
    # — the handler holds no redaction list of its own.
    out = await get_ticket_details(
        {"ticket_id": "INC0048213", "service_id": "incident"},
        {"tenant_id": "tenant-a"})
    assert "tenant_id" not in out["record"]


# ── private content — internal work_notes gated by role ──────────────────


async def test_end_user_role_sees_only_public_work_notes(store):
    out = await get_ticket_details(
        {"ticket_id": "INC0048213", "service_id": "incident"},
        {"tenant_id": "tenant-a", "role": "employee"})
    notes = out["record"]["work_notes"]
    assert [n["text"] for n in notes] == ["public update"]


async def test_privileged_role_sees_all_work_notes(store):
    out = await get_ticket_details(
        {"ticket_id": "INC0048213", "service_id": "incident"},
        {"tenant_id": "tenant-a", "role": "service_desk_agent"})
    assert len(out["record"]["work_notes"]) == 2


async def test_unrecognised_role_is_denied_internal_notes(store):
    # Default-deny — an unknown role is treated as an end user.
    out = await get_ticket_details(
        {"ticket_id": "INC0048213", "service_id": "incident"},
        {"tenant_id": "tenant-a", "role": "mystery_role"})
    assert [n["text"] for n in out["record"]["work_notes"]] == ["public update"]


# ── C13 — tenant isolation ────────────────────────────────────────────────


async def test_a_different_tenant_cannot_read_the_record(store):
    out = await get_ticket_details(
        {"ticket_id": "INC0048213", "service_id": "incident"},
        {"tenant_id": "tenant-b"})
    assert out["outcome"] == "not_found"
    assert out["record"] is None


# ── not found ─────────────────────────────────────────────────────────────


async def test_unknown_id_is_an_explicit_not_found(store):
    out = await get_ticket_details(
        {"ticket_id": "INC9999999", "service_id": "incident"},
        {"tenant_id": "tenant-a"})
    assert out["outcome"] == "not_found"
    assert "INC9999999" in out["message"]


async def test_wrong_service_for_an_existing_id_is_not_found(store):
    out = await get_ticket_details(
        {"ticket_id": "INC0048213", "service_id": "change"},
        {"tenant_id": "tenant-a"})
    assert out["outcome"] == "not_found"


# ── C17 — no silent failure: every bad input is an explicit outcome ───────


async def test_missing_ticket_id_is_invalid_request(store):
    out = await get_ticket_details(
        {"service_id": "incident"}, {"tenant_id": "tenant-a"})
    assert out["outcome"] == "invalid_request"
    assert out["message"]


async def test_missing_service_id_is_invalid_request(store):
    out = await get_ticket_details(
        {"ticket_id": "INC0048213"}, {"tenant_id": "tenant-a"})
    assert out["outcome"] == "invalid_request"


async def test_missing_tenant_in_envelope_is_invalid_request(store):
    out = await get_ticket_details(
        {"ticket_id": "INC0048213", "service_id": "incident"}, {})
    assert out["outcome"] == "invalid_request"


async def test_whitespace_arguments_are_treated_as_missing(store):
    out = await get_ticket_details(
        {"ticket_id": "   ", "service_id": "incident"},
        {"tenant_id": "tenant-a"})
    assert out["outcome"] == "invalid_request"
