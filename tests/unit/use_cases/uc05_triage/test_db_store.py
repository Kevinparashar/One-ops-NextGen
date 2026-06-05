"""UC-5 Postgres DbStore — production read/apply contract (hermetic).

Exercises the store with an injected fake asyncpg pool (no real DB): tenant-scoped
get_ticket, closed-status-filtered list_all, and the triage apply — column
whitelist, optimistic-lock SQL, and the KeyError/RuntimeError outcomes.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from oneops.errors import ConfigError
from oneops.use_cases.uc05_triage.stores.db_store import DbStore


class _FakeConn:
    def __init__(self, *, fetchrow=None, fetch=None, fetchval=None,
                 execute="UPDATE 1"):
        self._fetchrow, self._fetch = fetchrow, fetch or []
        self._fetchval, self._execute = fetchval, execute
        self.calls: list[tuple[str, str, tuple]] = []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args)); return self._fetchrow

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args)); return self._fetch

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args)); return self._fetchval

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args)); return self._execute


class _FakePool:
    def __init__(self, conn): self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Cm:
            async def __aenter__(self): return conn
            async def __aexit__(self, *a): return False
        return _Cm()


def _store(conn):
    return DbStore(pool=_FakePool(conn))


# ── get_ticket ───────────────────────────────────────────────────────────


async def test_get_ticket_returns_row_tenant_scoped():
    conn = _FakeConn(fetchrow={"incident_id": "INC1", "category": None})
    out = await _store(conn).get_ticket(
        service_id="incident", ticket_id="INC1", tenant_id="T001")
    assert out["incident_id"] == "INC1"
    _, sql, args = conn.calls[0]
    assert "itsm.incident" in sql and "tenant_id = $1" in sql
    assert args == ("T001", "INC1")


async def test_get_ticket_missing_raises_keyerror():
    conn = _FakeConn(fetchrow=None)
    with pytest.raises(KeyError):
        await _store(conn).get_ticket(
            service_id="incident", ticket_id="INC9", tenant_id="T001")


async def test_unsupported_service_id_is_configerror():
    with pytest.raises(ConfigError):
        await _store(_FakeConn()).get_ticket(
            service_id="problem", ticket_id="P1", tenant_id="T001")


# ── list_all ─────────────────────────────────────────────────────────────


async def test_list_all_filters_closed_and_scopes_tenant():
    conn = _FakeConn(fetch=[{"request_id": "SR1"}, {"request_id": "SR2"}])
    out = await _store(conn).list_all(service_id="request", tenant_id="T001")
    assert [r["request_id"] for r in out] == ["SR1", "SR2"]
    _, sql, args = conn.calls[0]
    assert "itsm.request" in sql and "<> ALL" in sql
    assert args[0] == "T001"


# ── apply: whitelist + optimistic lock ─────────────────────────────────────

_SLA = datetime(2026, 6, 5, 12, 0, tzinfo=UTC)


async def test_apply_rejects_non_triage_columns():
    with pytest.raises(ValueError, match="non-writable"):
        await _store(_FakeConn()).apply(
            service_id="incident", ticket_id="INC1", tenant_id="T001",
            final_values={"status": "hacked", "title": "x"},   # not triage cols
            sla_due=_SLA, actor_user_id="u1")


async def test_apply_happy_path_builds_whitelisted_update():
    conn = _FakeConn(execute="UPDATE 1")
    await _store(conn).apply(
        service_id="incident", ticket_id="INC1", tenant_id="T001",
        final_values={"category": "network", "priority": "High"},
        sla_due=_SLA, actor_user_id="u1")
    kind, sql, args = conn.calls[0]
    assert kind == "execute"
    assert sql.startswith("UPDATE itsm.incident SET")
    assert '"category" = $1' in sql and '"priority" = $2' in sql
    assert "sla_due = $3" in sql and "status = $4" in sql
    assert "category IS NULL" in sql            # optimistic lock
    assert args[0] == "network" and args[1] == "High"
    assert args[2] == _SLA and args[3] == "assigned"
    assert args[-2] == "T001" and args[-1] == "INC1"


async def test_apply_zero_rows_existing_row_is_optimistic_conflict():
    # UPDATE 0 + the row exists ⇒ already triaged (lost the race).
    conn = _FakeConn(execute="UPDATE 0", fetchval=1)
    with pytest.raises(RuntimeError, match="already triaged"):
        await _store(conn).apply(
            service_id="incident", ticket_id="INC1", tenant_id="T001",
            final_values={"category": "network"}, sla_due=_SLA,
            actor_user_id="u1")


async def test_apply_zero_rows_no_row_is_keyerror():
    conn = _FakeConn(execute="UPDATE 0", fetchval=None)
    with pytest.raises(KeyError):
        await _store(conn).apply(
            service_id="incident", ticket_id="INC9", tenant_id="T001",
            final_values={"category": "network"}, sla_due=_SLA,
            actor_user_id="u1")
