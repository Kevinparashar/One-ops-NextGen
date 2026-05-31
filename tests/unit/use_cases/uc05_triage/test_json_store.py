"""Unit tests for JsonFixtureStore — read + apply + optimistic-lock."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oneops.use_cases.uc05_triage.stores.json_store import JsonFixtureStore


def _make_fixture(tmp: Path) -> Path:
    p = tmp / "demo.json"
    p.write_text(json.dumps({
        "tenant_id": "T001",
        "incidents": [
            {"incident_id": "INC0000001",
             "title": "VPN drops at Mumbai", "description": "...",
             "status": "new", "category": None, "priority": None,
             "triaged_at": None}
        ],
        "requests": [
            {"request_id": "REQ0000001",
             "title": "MacBook for ML engineer", "description": "...",
             "status": "new", "category": None, "priority": None,
             "triaged_at": None}
        ]
    }))
    return p


class TestGetTicket:
    @pytest.mark.asyncio
    async def test_get_incident(self, tmp_path: Path) -> None:
        store = JsonFixtureStore(_make_fixture(tmp_path))
        row = await store.get_ticket(
            service_id="incident", ticket_id="INC0000001", tenant_id="T001",
        )
        assert row["title"] == "VPN drops at Mumbai"

    @pytest.mark.asyncio
    async def test_get_request(self, tmp_path: Path) -> None:
        store = JsonFixtureStore(_make_fixture(tmp_path))
        row = await store.get_ticket(
            service_id="request", ticket_id="REQ0000001", tenant_id="T001",
        )
        assert row["title"] == "MacBook for ML engineer"

    @pytest.mark.asyncio
    async def test_missing_id_raises(self, tmp_path: Path) -> None:
        store = JsonFixtureStore(_make_fixture(tmp_path))
        with pytest.raises(KeyError):
            await store.get_ticket(service_id="incident",
                                    ticket_id="NOPE", tenant_id="T001")

    @pytest.mark.asyncio
    async def test_wrong_tenant_raises(self, tmp_path: Path) -> None:
        store = JsonFixtureStore(_make_fixture(tmp_path))
        with pytest.raises(KeyError):
            await store.get_ticket(service_id="incident",
                                    ticket_id="INC0000001", tenant_id="T999")


class TestApply:
    @pytest.mark.asyncio
    async def test_apply_writes_back_to_json(self, tmp_path: Path) -> None:
        p = _make_fixture(tmp_path)
        store = JsonFixtureStore(p)
        sla_due = datetime(2026, 5, 29, 22, 0, 0, tzinfo=UTC)
        await store.apply(
            service_id="incident", ticket_id="INC0000001", tenant_id="T001",
            final_values={"category": "network", "priority": "High",
                          "assignment_group": "Network-L2"},
            sla_due=sla_due, actor_user_id="tech1",
        )
        data = json.loads(p.read_text())
        row = data["incidents"][0]
        assert row["category"] == "network"
        assert row["priority"] == "High"
        assert row["sla_due"] == sla_due.isoformat()
        assert row["triaged_by"] == "tech1"
        assert row["status"] == "assigned"

    @pytest.mark.asyncio
    async def test_apply_optimistic_lock_already_triaged(self, tmp_path: Path) -> None:
        p = _make_fixture(tmp_path)
        store = JsonFixtureStore(p)
        sla_due = datetime(2026, 5, 29, 22, 0, 0, tzinfo=UTC)
        # First apply succeeds
        await store.apply(
            service_id="incident", ticket_id="INC0000001", tenant_id="T001",
            final_values={"category": "network"}, sla_due=sla_due,
            actor_user_id="tech1",
        )
        # Second apply against the now-triaged row must fail loud
        with pytest.raises(RuntimeError, match="already triaged"):
            await store.apply(
                service_id="incident", ticket_id="INC0000001", tenant_id="T001",
                final_values={"category": "email"}, sla_due=sla_due,
                actor_user_id="tech2",
            )

    @pytest.mark.asyncio
    async def test_apply_missing_id_raises(self, tmp_path: Path) -> None:
        store = JsonFixtureStore(_make_fixture(tmp_path))
        with pytest.raises(KeyError):
            await store.apply(
                service_id="incident", ticket_id="NOPE", tenant_id="T001",
                final_values={"category": "x"},
                sla_due=datetime.now(UTC),
                actor_user_id="tech",
            )

    @pytest.mark.asyncio
    async def test_apply_wrong_tenant_raises(self, tmp_path: Path) -> None:
        store = JsonFixtureStore(_make_fixture(tmp_path))
        with pytest.raises(KeyError):
            await store.apply(
                service_id="incident", ticket_id="INC0000001", tenant_id="T999",
                final_values={"category": "x"},
                sla_due=datetime.now(UTC),
                actor_user_id="tech",
            )
