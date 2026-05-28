"""Postgres adapter — asyncpg connection pool.

One pool per process, shared across all coroutines. asyncpg is documented as
safe for shared-pool concurrent use; each acquire() hands out a dedicated
connection that's returned automatically on context exit.

Lifecycle:
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT ...")
    await shutdown_pg_pool()

Production-grade properties:
- Connection pool with min/max bounds from settings
- OTEL instrumentation via opentelemetry-instrumentation-asyncpg (auto)
- Typed exception mapping: pg errors -> UpstreamError subclasses
- Fail-fast at startup: pool initialization verifies connectivity
- Statement-level convenience methods (_fetchone, _fetchall, _execute)
- No SQL strings here — SQL lives in per-UC repositories
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

import asyncpg

from oneops.config import get_settings
from oneops.errors import UpstreamError
from oneops.observability import get_logger

_log = get_logger("oneops.postgres")

# Per-event-loop pool cache. asyncpg connections are bound to the loop that
# created them; sharing a pool across event loops fails with "Event loop is
# closed" or "different loop". In production one persistent loop hosts the
# service, so this behaves as a single process-wide pool; in pytest-asyncio
# each test loop gets its own pool. WeakKeyDictionary drops dead entries.
import weakref as _weakref

_pools: "_weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncpg.Pool]" = (
    _weakref.WeakKeyDictionary()
)
_lock = threading.Lock()


async def get_pg_pool() -> asyncpg.Pool:
    """Get-or-create the asyncpg pool for the current event loop."""
    loop = asyncio.get_running_loop()
    pool = _pools.get(loop)
    if pool is not None:
        return pool
    with _lock:
        pool = _pools.get(loop)
        if pool is not None:
            return pool
        settings = get_settings()
        try:
            pool = await asyncpg.create_pool(
                dsn=settings.postgres_url,
                min_size=settings.postgres_pool_min,
                max_size=settings.postgres_pool_max,
                command_timeout=30.0,
                # Don't use prepared statements with pgbouncer in transaction-pooling mode:
                statement_cache_size=0,
            )
            # Ping to verify
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
            _pools[loop] = pool
            _log.info(
                "postgres.connected",
                host=settings.postgres_url.split("@")[-1].split("/")[0],
                pool_min=settings.postgres_pool_min,
                pool_max=settings.postgres_pool_max,
                loop_id=id(loop),
            )
            return pool
        except (asyncpg.PostgresError, OSError, ConnectionError) as e:
            raise UpstreamError(f"cannot connect to Postgres: {e}", cause=e) from e


async def shutdown_pg_pool() -> None:
    """Close the current loop's pool. Idempotent."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    pool = _pools.pop(loop, None)
    if pool is not None:
        try:
            await pool.close()
        except Exception as exc:  # noqa: BLE001 — shutdown must not raise
            _log.warning("postgres.shutdown_failed", error=str(exc))


class BaseRepository:
    """Base class for per-UC repositories.

    UCs subclass this in their own folder (e.g. `ITSMRepo`, `KBRepo`). Common code
    never adds SQL — that's why this base only exposes a tiny surface.
    """

    @classmethod
    async def _fetchone(cls, query: str, *args: Any) -> dict[str, Any] | None:
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, *args)
            return dict(row) if row else None

    @classmethod
    async def _fetchall(cls, query: str, *args: Any) -> list[dict[str, Any]]:
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
            return [dict(r) for r in rows]

    @classmethod
    async def _execute(cls, query: str, *args: Any) -> str:
        """Return the asyncpg status string (e.g. 'INSERT 0 1')."""
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            return await conn.execute(query, *args)


__all__ = ["get_pg_pool", "shutdown_pg_pool", "BaseRepository"]
