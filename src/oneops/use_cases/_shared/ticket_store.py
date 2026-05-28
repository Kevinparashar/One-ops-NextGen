"""Ticket data access for UC-1 — a pluggable backend.

A tool handler must never hard-wire a database: unit tests and the no-infra
executor run with zero infrastructure, while production runs against Postgres.
So data access is the `TicketStore` protocol with two interchangeable backends:

  * `InMemoryTicketStore` — deterministic, seeded fixtures, no I/O. The default;
    what unit tests and the no-infra executor run on.
  * `PostgresTicketStore` — the live backend over the `itsm` schema on the
    NextGen-ai Supabase project (`iyoimwkzypbsccqdjhya`). Selected by env var
    `ONEOPS_TICKET_BACKEND=postgres`. Read-only by design: only `SELECT`s
    against the canonical `itsm.<service>` tables, no DDL, no writes — this
    is the post-incident discipline locked by ADR-0004 (the 2026-05-16
    Prisma-against-shared-schema event must never repeat).

Tenant isolation is part of the contract, not an afterthought: `get` takes a
`tenant_id` and a backend MUST scope to it — a record is only ever returned to
its own tenant. The handler passes the tenant from the request envelope, never
from user text (CONTEXT_TOOL_INPUT_BINDING_POLICY).

Concurrency: the Postgres backend uses an `asyncpg.Pool` injected at
construction. Multiple concurrent turns from one or many tenants reuse the
same pool — no module-level mutable state, no per-call connection set-up.
This is also FaaS-safe: cold start opens the pool lazily, every warm
invocation reuses it.
"""
from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable

from oneops.errors import ConfigError, OneOpsError
from oneops.observability import get_logger, get_tracer, histogram, increment

_log = get_logger("oneops.use_cases.ticket_store")
_tracer = get_tracer("oneops.use_cases.ticket_store")


# Service → (table, primary-key column) map. Registry-data shape — adding a
# new service module (e.g. "incident_v2") is a one-line entry, not a code
# change ([[feedback_poc5mw_design_for_1000_ucs_from_day_1]]).
# Verified against the live `itsm` schema on 2026-05-26.
_SERVICE_TABLE_MAP: dict[str, tuple[str, str]] = {
    "incident": ("itsm.incident",  "incident_id"),
    "request":  ("itsm.request",   "request_id"),
    "problem":  ("itsm.problem",   "problem_id"),
    "change":   ("itsm.change",    "change_id"),
    "asset":    ("itsm.asset",     "asset_id"),
    "cmdb_ci":  ("itsm.cmdb_ci",   "ci_id"),
}


def supported_services() -> tuple[str, ...]:
    """The service ids the Postgres store can resolve. Exposed for the
    integrity check + smoke tests."""
    return tuple(_SERVICE_TABLE_MAP)


@runtime_checkable
class TicketStore(Protocol):
    """Fetch one ITSM work record (incident / request / problem / change /
    asset / CMDB CI), scoped to a tenant. Returns `None` when no such record
    exists for that tenant — never another tenant's row, never a guess."""

    async def get(
        self, *, ticket_id: str, service_id: str, tenant_id: str
    ) -> dict[str, Any] | None: ...


class InMemoryTicketStore:
    """Deterministic, in-process `TicketStore` — the no-infrastructure default.

    Records are seeded explicitly with `seed(...)`; nothing is fabricated. The
    `(tenant_id, service_id, ticket_id)` key makes tenant isolation structural:
    a lookup with the wrong tenant simply misses."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], dict[str, Any]] = {}

    def seed(
        self, *, ticket_id: str, service_id: str, tenant_id: str, **fields: Any
    ) -> None:
        """Add or replace one record. `tenant_id` is stored on the row so the
        redaction layer strips it exactly as a live row would carry it."""
        row = {"tenant_id": tenant_id, **fields}
        self._rows[(tenant_id, service_id, ticket_id)] = row

    def clear(self) -> None:
        self._rows.clear()

    async def get(
        self, *, ticket_id: str, service_id: str, tenant_id: str
    ) -> dict[str, Any] | None:
        row = self._rows.get((tenant_id, service_id, ticket_id))
        return dict(row) if row is not None else None


class PostgresTicketStore:
    """Live `TicketStore` over the `itsm.<service>` tables on the NextGen-ai
    Supabase project. Read-only, tenant-scoped, async-pool-backed.

    Construction:
      * Lazy pool: the first `get` call opens an `asyncpg.Pool` against
        `POSTGRES_URL`; subsequent calls reuse it. Pool sizing is taken from
        `POSTGRES_POOL_MIN` / `POSTGRES_POOL_MAX` (defaults 1 / 5).
      * SSL is required (Supabase pooler accepts only encrypted connections).
      * `statement_timeout` is set per-connection to bound any runaway query
        well under the tool runner's timeout — defence in depth.

    Read discipline:
      * One prepared statement per service module (cached on the pool).
      * `WHERE tenant_id = $1 AND <pk> = $2` — both predicates always present.
      * Returns a plain `dict[str, Any]` with JSONB columns already decoded
        into Python dicts/lists by asyncpg.

    Failure modes:
      * Unknown `service_id` → `ConfigError` (it is a caller bug; loud).
      * Connection / query failure → caught at the boundary and re-raised as
        `OneOpsError("ticket_store.postgres")` with the original error chained;
        the row return path stays `dict | None` exactly like the in-memory
        contract.
      * `None` is returned for "no such row for this tenant" — the same
        contract InMemoryTicketStore exposes; the handler distinguishes
        "not found" from "lookup failed" through the exception type only.
    """

    def __init__(
        self,
        *,
        dsn: str | None = None,
        pool: Any | None = None,
        statement_timeout_ms: int = 5_000,
        connect_timeout_s: float = 10.0,
    ) -> None:
        self._dsn = dsn or os.getenv("POSTGRES_URL", "").strip()
        if not self._dsn and pool is None:
            raise ConfigError(
                "PostgresTicketStore needs a DSN (env POSTGRES_URL) or a pool")
        self._pool = pool                              # caller-provided pool wins
        self._owns_pool = pool is None                 # only close pools we created
        self._statement_timeout_ms = statement_timeout_ms
        self._connect_timeout_s = connect_timeout_s
        self._pool_lock: Any = None                    # built lazily to avoid loop binding

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        # Lazy asyncio.Lock so the class is import-safe with no running loop
        # (cold-start: import time has no event loop, first call binds to one).
        import asyncio
        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()
        async with self._pool_lock:
            if self._pool is not None:                 # racing coroutine won
                return self._pool
            import asyncpg
            min_size = int(os.getenv("POSTGRES_POOL_MIN", "1"))
            max_size = int(os.getenv("POSTGRES_POOL_MAX", "5"))
            # Strip query string — asyncpg honours sslmode= via the `ssl=` kwarg.
            dsn = self._dsn.split("?")[0] if self._dsn else self._dsn

            async def _init_conn(conn: Any) -> None:
                # Statement timeout per-connection — bounds any runaway query
                # below the tool runner's outer timeout. This is the
                # operator-visible kill switch against pathological reads.
                await conn.execute(
                    f"SET statement_timeout = {int(self._statement_timeout_ms)}")
                # Force read-only on every connection. A bug that tries to
                # write fails loud at the connection level, never in
                # production data. Defence-in-depth against ADR-0004's
                # incident pattern.
                await conn.execute(
                    "SET default_transaction_read_only = on")
                # JSONB codec — without this, asyncpg returns jsonb columns
                # as raw JSON strings. The handlers + field-label
                # humaniser expect Python dicts / lists (which is what the
                # in-memory store seeds with). Register the codec so the
                # Postgres backend has the SAME shape contract as InMemory.
                import json as _json
                await conn.set_type_codec(
                    "jsonb",
                    schema="pg_catalog",
                    encoder=_json.dumps,
                    decoder=_json.loads,
                    format="text",
                )

            try:
                self._pool = await asyncpg.create_pool(
                    dsn=dsn, ssl="require",
                    min_size=min_size, max_size=max_size,
                    timeout=self._connect_timeout_s,
                    init=_init_conn,
                )
            except Exception as exc:
                raise OneOpsError(
                    "ticket_store.postgres: pool create failed",
                    cause=exc) from exc
            _log.info("ticket_store.postgres.pool_opened",
                      min_size=min_size, max_size=max_size)
            return self._pool

    async def close(self) -> None:
        """Graceful shutdown — close the pool if we created it. Callable from
        FaaS shutdown hooks; idempotent."""
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
            _log.info("ticket_store.postgres.pool_closed")
        self._pool = None

    async def get(
        self, *, ticket_id: str, service_id: str, tenant_id: str,
    ) -> dict[str, Any] | None:
        if not service_id or service_id not in _SERVICE_TABLE_MAP:
            raise ConfigError(
                f"PostgresTicketStore: unknown service_id {service_id!r}; "
                f"supported: {sorted(_SERVICE_TABLE_MAP)}")
        if not ticket_id or not tenant_id:
            # Same contract as InMemory — empty inputs mean "no match".
            return None
        table, pk_col = _SERVICE_TABLE_MAP[service_id]
        # SQL identifiers come from the static map (not user input) — safe to
        # interpolate. Values stay parametrised.
        sql = (
            f"SELECT * FROM {table} "
            f'WHERE tenant_id = $1 AND "{pk_col}" = $2 LIMIT 1'
        )
        pool = await self._ensure_pool()
        with _tracer.start_as_current_span(
            "ticket_store.postgres.get",
            attributes={
                "db.system": "postgresql",
                "db.statement.name": f"itsm.{service_id}.get_by_id",
                "oneops.tenant_id": tenant_id,
                "oneops.service_id": service_id,
                "oneops.entity_id": ticket_id,
                # ticket_id itself is the canonical opaque id; not PII per
                # the data-class map — safe to attribute. The textual
                # title/description never lands on a span here.
            },
        ) as span:
            import time as _t
            t0 = _t.monotonic()
            try:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(sql, tenant_id, ticket_id)
            except Exception as exc:
                span.set_attribute("error", True)
                _log.warning(
                    "ticket_store.postgres.query_failed",
                    service_id=service_id, error=str(exc)[:200])
                # Counter for Postgres-layer failures — feeds an
                # operator dashboard distinct from agent-level errors.
                increment("ai.postgres.errors.total",
                          store="ticket_store", service_id=service_id,
                          reason=type(exc).__name__)
                raise OneOpsError(
                    f"ticket_store.postgres: query failed for "
                    f"{service_id}/{ticket_id}", cause=exc) from exc
            # Latency histogram — paired with the span for p99 alerting.
            histogram("ai.postgres.query.duration_ms",
                      (_t.monotonic() - t0) * 1000.0,
                      store="ticket_store", service_id=service_id)
            span.set_attribute("db.row_found", row is not None)
            if row is None:
                return None
            # asyncpg's Record is dict-like; convert to a plain dict so the
            # handler/field-policy layer sees the same shape the in-memory
            # store returns.
            return dict(row)


_store: TicketStore | None = None


def _build_default() -> TicketStore:
    backend = os.getenv("ONEOPS_TICKET_BACKEND", "memory").strip().lower()
    if backend == "postgres":
        _log.info("ticket_store.backend_selected", backend="postgres")
        return PostgresTicketStore()
    _log.info("ticket_store.backend_selected", backend="memory")
    return InMemoryTicketStore()


def get_ticket_store() -> TicketStore:
    """The process-wide ticket store. In-memory unless `ONEOPS_TICKET_BACKEND`
    selects the live backend."""
    global _store
    if _store is None:
        _store = _build_default()
    return _store


def set_ticket_store(store: TicketStore) -> None:
    """Replace the process-wide store — used by tests and by FaaS wiring to
    inject a seeded or live backend."""
    global _store
    _store = store


__all__ = [
    "TicketStore",
    "InMemoryTicketStore",
    "PostgresTicketStore",
    "get_ticket_store",
    "set_ticket_store",
    "supported_services",
]
