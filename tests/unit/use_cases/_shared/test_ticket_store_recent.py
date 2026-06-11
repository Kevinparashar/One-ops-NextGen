"""S1 — `list_recent_for_user`: the data behind contextual replies
('my last ticket' / 'recent ones').

Drives the in-memory backend (the no-infra contract every other store mirrors):
user-scoping (only the caller's own records), recency ordering, cross-service
merge, the per-service owner-column union (reporter/requester/assignee), the
limit cap, and tenant isolation.
"""
from __future__ import annotations

import pytest

from oneops.use_cases._shared.ticket_store import (
    InMemoryTicketStore,
    recency_services,
)

pytestmark = pytest.mark.asyncio


def _store() -> InMemoryTicketStore:
    s = InMemoryTicketStore()
    # incident: owner cols reported_by / assigned_to
    s.seed(ticket_id="INC0000001", service_id="incident", tenant_id="T001",
           reported_by="U1", title="VPN drops", status="open",
           updated_at="2026-06-01T10:00:00")
    s.seed(ticket_id="INC0000002", service_id="incident", tenant_id="T001",
           assigned_to="U1", title="Wifi flaky", status="open",
           updated_at="2026-06-03T10:00:00")          # most recent for U1
    s.seed(ticket_id="INC0000009", service_id="incident", tenant_id="T001",
           reported_by="U2", title="Someone else", status="open",
           updated_at="2026-06-09T10:00:00")          # not U1's
    # request: owner cols requested_by / requested_for / assigned_to
    s.seed(ticket_id="REQ0000001", service_id="request", tenant_id="T001",
           requested_for="U1", title="New laptop", status="pending_approval",
           updated_at="2026-06-02T10:00:00")
    # other tenant — must never surface for T001
    s.seed(ticket_id="INC0000050", service_id="incident", tenant_id="T999",
           reported_by="U1", title="Other tenant", status="open",
           updated_at="2026-06-08T10:00:00")
    return s


async def test_returns_only_callers_records_recency_first() -> None:
    recents = await _store().list_recent_for_user(
        tenant_id="T001", user_id="U1")
    ids = [r["ticket_id"] for r in recents]
    # U1's three records, most-recent first; U2's and T999's excluded.
    assert ids == ["INC0000002", "REQ0000001", "INC0000001"]
    assert all(r["service_id"] in recency_services() for r in recents)
    # compact candidate shape — label fields present, sort key stripped.
    top = recents[0]
    assert top["title"] == "Wifi flaky" and top["status"] == "open"
    assert "_recency" not in top


async def test_owner_column_union_matches_any_party_role() -> None:
    s = _store()
    # reporter (INC1), assignee (INC2), requested_for (REQ1) all count as "mine".
    ids = {r["ticket_id"]
           for r in await s.list_recent_for_user(tenant_id="T001", user_id="U1")}
    assert ids == {"INC0000001", "INC0000002", "REQ0000001"}


async def test_limit_caps_results() -> None:
    one = await _store().list_recent_for_user(
        tenant_id="T001", user_id="U1", limit=1)
    assert len(one) == 1 and one[0]["ticket_id"] == "INC0000002"


async def test_service_filter_restricts_to_requested() -> None:
    only_req = await _store().list_recent_for_user(
        tenant_id="T001", user_id="U1", services=("request",))
    assert [r["ticket_id"] for r in only_req] == ["REQ0000001"]


async def test_unknown_user_or_tenant_returns_empty() -> None:
    s = _store()
    assert await s.list_recent_for_user(tenant_id="T001", user_id="NOBODY") == []
    assert await s.list_recent_for_user(tenant_id="T404", user_id="U1") == []
