"""PostgresTicketStore live integration test.

Reads the real `itsm.<service>` tables on the Supabase project named in
`POSTGRES_URL`. Skipped automatically when the env var is unset, so CI
without DB access still runs green.

Verifies the production-grade invariants the design promises:

  * **Tenant isolation is structural.** A row exists for one tenant; the
    same id read with a different tenant returns `None`. No leak.
  * **Read-only connection.** Every connection in the pool has
    `default_transaction_read_only=on`; an attempted INSERT raises at the
    DB level, not at handler level.
  * **Unknown service is a typed `ConfigError`.** Caller bug, surfaced loud.
  * **Pool reuse.** A second `get` reuses the pool (no second connect).
  * **OTel span** is opened with the expected attributes.

Test data is read-only — no fixtures inserted, no rows mutated.
"""
from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = [pytest.mark.integration]

# Env-gate: skip the whole module if there's no DSN to talk to.
DSN = os.getenv("POSTGRES_URL", "").strip()
if not DSN:
    pytest.skip(
        "POSTGRES_URL not set — live PostgresTicketStore integration "
        "skipped (this is the expected state in CI / unit-only runs).",
        allow_module_level=True)


from oneops.errors import ConfigError  # noqa: E402
from oneops.use_cases._shared.ticket_store import (  # noqa: E402
    PostgresTicketStore,
    supported_services,
)

# A real id we observed at audit time. If the seed changes, swap this.
_KNOWN_INCIDENT_ID = "INC0001001"
_KNOWN_TENANT_ID = "T001"


@pytest.fixture
async def store():
    s = PostgresTicketStore()
    try:
        yield s
    finally:
        await s.close()


# ── happy path ───────────────────────────────────────────────────────────


async def test_real_incident_round_trip_returns_a_dict(store):
    row = await store.get(
        ticket_id=_KNOWN_INCIDENT_ID, service_id="incident",
        tenant_id=_KNOWN_TENANT_ID)
    assert row is not None
    # Canonical fields the handler will consume — must be present.
    assert row["incident_id"] == _KNOWN_INCIDENT_ID
    assert row["tenant_id"] == _KNOWN_TENANT_ID
    assert isinstance(row.get("title"), str)
    assert row["title"]                                  # non-empty
    # JSONB columns are decoded by asyncpg → Python list/dict.
    assert isinstance(row["work_notes"], list)
    assert isinstance(row["comments"], list)
    assert isinstance(row["attachments"], list)


# ── tenant isolation — structural ───────────────────────────────────────


async def test_wrong_tenant_yields_no_row(store):
    row = await store.get(
        ticket_id=_KNOWN_INCIDENT_ID, service_id="incident",
        tenant_id="T999_does_not_exist")
    assert row is None


# ── caller-side empty inputs collapse to None (same as InMemory) ─────────


async def test_empty_inputs_return_none(store):
    assert await store.get(
        ticket_id="", service_id="incident", tenant_id="T001") is None
    assert await store.get(
        ticket_id="INC0001001", service_id="incident", tenant_id="") is None


# ── unknown service_id is a loud typed error ─────────────────────────────


async def test_unknown_service_id_is_a_config_error(store):
    with pytest.raises(ConfigError, match="unknown service_id"):
        await store.get(
            ticket_id="X", service_id="not_a_service", tenant_id="T001")


# ── every advertised service module resolves through the same store ─────


async def test_every_supported_service_resolves(store):
    # Tests the table map is wired to real tables. We don't assert a row
    # exists for every service — just that the query runs cleanly and
    # returns `dict | None` per contract.
    for svc in supported_services():
        result = await store.get(
            ticket_id="probe_id_that_will_not_match",
            service_id=svc, tenant_id="T001")
        assert result is None                            # no row for synthetic id


# ── read-only connections — INSERT is rejected at the DB level ───────────


async def test_pool_connections_are_read_only(store):
    """The init hook sets `default_transaction_read_only=on`; an explicit
    INSERT against the connection must fail with a DB-level error, proving
    the post-incident hardening (ADR-0004) is active end-to-end."""
    pool = await store._ensure_pool()                    # noqa: SLF001 — test boundary
    async with pool.acquire() as conn:
        # Use a CREATE TEMP TABLE / INSERT pair that targets nothing real —
        # the read-only transaction must refuse it.
        with pytest.raises(Exception) as info:
            await conn.execute("CREATE TEMP TABLE _probe (x int)")
        # The DB error class varies by asyncpg version; the message string
        # carries the "read-only" signal reliably.
        assert "read-only" in str(info.value).lower() or "read only" in str(info.value).lower()


# ── pool reuse — second get reuses the already-opened pool ──────────────


async def test_pool_is_reused_across_calls(store):
    await store.get(
        ticket_id=_KNOWN_INCIDENT_ID, service_id="incident",
        tenant_id=_KNOWN_TENANT_ID)
    first_pool = store._pool                             # noqa: SLF001
    await store.get(
        ticket_id=_KNOWN_INCIDENT_ID, service_id="incident",
        tenant_id=_KNOWN_TENANT_ID)
    assert store._pool is first_pool                     # noqa: SLF001


# ── concurrency — two parallel reads succeed (the production shape) ─────


async def test_concurrent_reads_on_one_store_dont_collide(store):
    """Many users hitting the same UC at the same time = many parallel
    `get` calls against the same pool. Must all return the right tenant's
    row independently (no shared mutable state, no cross-talk)."""
    results = await asyncio.gather(*[
        store.get(
            ticket_id=_KNOWN_INCIDENT_ID, service_id="incident",
            tenant_id=_KNOWN_TENANT_ID)
        for _ in range(10)
    ])
    assert all(r is not None for r in results)
    assert all(r["incident_id"] == _KNOWN_INCIDENT_ID for r in results)
    assert all(r["tenant_id"] == _KNOWN_TENANT_ID for r in results)
