"""Integration tests for the Postgres adapter against the real Supabase pool.

Requires POSTGRES_URL to be reachable. Skipped automatically if it isn't.

Concurrency-relevant: BaseRepository acquires from a shared asyncpg.Pool. Each
acquire() hands out a dedicated connection; the pool serializes contention.
We verify 20 concurrent queries succeed.
"""
from __future__ import annotations

import asyncio
import os
from urllib.parse import urlparse

import pytest

from oneops.adapters.postgres import (
    BaseRepository,
    get_pg_pool,
    shutdown_pg_pool,
)
from tests.conftest import has_service


def _postgres_reachable() -> bool:
    url = urlparse(os.getenv("POSTGRES_URL", ""))
    if not url.hostname:
        return False
    # Supabase pooler is on 5432 by default; URL may also specify 6543 for tx-mode
    return has_service(url.hostname, url.port or 5432, timeout=2.0)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _postgres_reachable(), reason="Postgres not reachable"),
]


@pytest.fixture
async def pool():
    p = await get_pg_pool()
    try:
        yield p
    finally:
        await shutdown_pg_pool()


# ── Connection lifecycle ───────────────────────────────────────


async def test_get_pool_is_singleton(pool) -> None:
    again = await get_pg_pool()
    assert again is pool


async def test_pool_serves_simple_query(pool) -> None:
    async with pool.acquire() as conn:
        v = await conn.fetchval("SELECT 1")
        assert v == 1


async def test_pool_reports_server_version(pool) -> None:
    async with pool.acquire() as conn:
        version = await conn.fetchval("SELECT version()")
        # Just verify we got something Postgres-shaped back
        assert isinstance(version, str)
        assert "PostgreSQL" in version


# ── BaseRepository surface ─────────────────────────────────────


async def test_fetchone_returns_dict_or_none(pool) -> None:
    row = await BaseRepository._fetchone("SELECT 1 AS one, 'two'::text AS label")
    assert row == {"one": 1, "label": "two"}
    none = await BaseRepository._fetchone("SELECT 1 WHERE FALSE")
    assert none is None


async def test_fetchall_returns_list_of_dicts(pool) -> None:
    rows = await BaseRepository._fetchall(
        "SELECT g.n AS n FROM generate_series(1, 3) AS g(n) ORDER BY n"
    )
    assert rows == [{"n": 1}, {"n": 2}, {"n": 3}]


async def test_fetchall_empty(pool) -> None:
    rows = await BaseRepository._fetchall("SELECT 1 WHERE FALSE")
    assert rows == []


# ── Concurrency ────────────────────────────────────────────────


async def test_concurrent_queries(pool) -> None:
    """10 parallel queries; pool serves them without contention errors.

    Tuned to 10 to fit the Supabase pooler default per-IP cap.
    """
    async def query(i: int) -> int:
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT $1::int + 1", i)

    results = await asyncio.gather(*(query(i) for i in range(10)))
    assert results == [i + 1 for i in range(10)]


async def test_concurrent_repository_calls(pool) -> None:
    """10 concurrent BaseRepository calls — pool + acquire/release safe.

    Tuned to 10 to fit the Supabase session-mode pooler cap (pool_size: 15).
    For higher concurrency in prod, use the transaction-pooler URL (port 6543)
    which allows ~200+ concurrent sessions.
    """
    async def call(i: int) -> dict:
        return await BaseRepository._fetchone("SELECT $1::int AS n", i)

    results = await asyncio.gather(*(call(i) for i in range(10)))
    assert [r["n"] for r in results] == list(range(10))


# ── Tenant safety primitive (parameterized query) ──────────────


async def test_parameterized_query_blocks_injection_attempt(pool) -> None:
    """asyncpg uses positional binds — injection in a value is just data."""
    suspicious = "1'; DROP TABLE x;--"
    row = await BaseRepository._fetchone(
        "SELECT $1::text AS payload", suspicious
    )
    # Payload is preserved verbatim; the SQL isn't reinterpreted.
    assert row == {"payload": suspicious}
