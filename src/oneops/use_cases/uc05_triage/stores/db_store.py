"""Postgres-backed UC-5 TicketStore — production reads + triage-apply writes.

Satisfies the same `TicketStore` protocol as `JsonFixtureStore` (get_ticket /
list_all / apply) over the real `itsm.incident` / `itsm.request` tables, so
`apply.py`, the queue endpoints, and the executor handlers are backend-agnostic.

Design (production discipline):
  * Lazy async pool over `POSTGRES_URL` (SSL required — Supabase pooler). Pool
    size from POSTGRES_POOL_MIN/MAX (defaults 1/5). Per-connection
    `statement_timeout` bounds runaway queries under the tool-runner timeout.
    jsonb columns decoded to Python dicts/lists (same shape as the JSON store).
  * Unlike the read-only `_shared.PostgresTicketStore`, this pool is read-WRITE
    (apply UPDATEs the triage fields) — so it does NOT set
    `default_transaction_read_only`.
  * apply WHITELISTS the columns it writes to the UC-5-owned triage fields for
    the service (`triage_fields_for`) plus `sla_due`/`status`/`updated_at`. A
    `final_values` key outside that whitelist is rejected loud — the store can
    never be coerced into writing an arbitrary column.
  * Optimistic lock without a dedicated column: the UPDATE is gated on
    `category IS NULL` (the canonical "not yet triaged" sentinel — category is a
    triage field for both services). 0 rows updated → re-check existence to
    distinguish KeyError (no such ticket in tenant) from RuntimeError (already
    triaged, lost the race). Atomic in one statement.
  * Tenant isolation is structural: every query carries `WHERE tenant_id = $1`.

NOTE: the real schema has no `triaged_at`/`triaged_by` columns (the JSON fixture
invented them); the audit trail for who/when is captured in the UC-5 decision
record / spans, not on the ticket row.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from oneops.errors import ConfigError, OneOpsError
from oneops.observability import get_logger, span
from oneops.use_cases.uc05_triage.queue import (
    CLOSED_STATUSES,
    writable_fields_for,
)

_log = get_logger("oneops.use_cases.uc05_triage.stores.db")

# Service → (table, primary-key column). UC-5 owns incident + request only.
_SERVICE_TABLE: dict[str, tuple[str, str]] = {
    "incident": ("itsm.incident", "incident_id"),
    "request": ("itsm.request", "request_id"),
}


class DbStore:
    """Live, tenant-scoped, async-pool-backed TicketStore with triage writes."""

    def __init__(
        self,
        *,
        dsn: str | None = None,
        pool: Any | None = None,
        statement_timeout_ms: int = 8_000,
        connect_timeout_s: float = 10.0,
    ) -> None:
        self._dsn = dsn or os.getenv("POSTGRES_URL", "").strip()
        if not self._dsn and pool is None:
            raise ConfigError(
                "DbStore needs a DSN (env POSTGRES_URL) or an injected pool")
        self._pool = pool
        self._owns_pool = pool is None
        self._statement_timeout_ms = statement_timeout_ms
        self._connect_timeout_s = connect_timeout_s
        self._pool_lock: Any = None

    # ── pool lifecycle ────────────────────────────────────────────────────

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        import asyncio
        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()
        async with self._pool_lock:
            if self._pool is not None:
                return self._pool
            import asyncpg
            min_size = int(os.getenv("POSTGRES_POOL_MIN", "1"))
            max_size = int(os.getenv("POSTGRES_POOL_MAX", "5"))
            dsn = self._dsn.split("?")[0] if self._dsn else self._dsn

            async def _init_conn(conn: Any) -> None:
                await conn.execute(
                    f"SET statement_timeout = {int(self._statement_timeout_ms)}")
                import json as _json
                await conn.set_type_codec(
                    "jsonb", schema="pg_catalog",
                    encoder=_json.dumps, decoder=_json.loads, format="text")

            try:
                self._pool = await asyncpg.create_pool(
                    dsn=dsn, ssl="require",
                    min_size=min_size, max_size=max_size,
                    timeout=self._connect_timeout_s, init=_init_conn)
            except Exception as exc:                              # noqa: BLE001
                raise OneOpsError("uc05.db_store: pool create failed",
                                  cause=exc) from exc
            _log.info("uc05.db_store.pool_opened",
                      min_size=min_size, max_size=max_size)
            return self._pool

    async def close(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
            _log.info("uc05.db_store.pool_closed")
        self._pool = None

    @staticmethod
    def _resolve(service_id: str) -> tuple[str, str]:
        if service_id not in _SERVICE_TABLE:
            raise ConfigError(
                f"DbStore: unsupported service_id {service_id!r}; "
                f"supported: {sorted(_SERVICE_TABLE)}")
        return _SERVICE_TABLE[service_id]

    # ── reads ─────────────────────────────────────────────────────────────

    async def get_ticket(
        self, *, service_id: str, ticket_id: str, tenant_id: str
    ) -> Mapping[str, Any]:
        """Fetch one ticket row, tenant-scoped. Raises KeyError if not found."""
        table, pk = self._resolve(service_id)
        if not ticket_id or not tenant_id:
            raise KeyError(ticket_id)
        with span("uc05.store.get_ticket",
                  **{"oneops.tenant_id": tenant_id,
                     "uc05.service_id": service_id,
                     "uc05.ticket_id": ticket_id, "uc05.store": "postgres"}):
            pool = await self._ensure_pool()
            try:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        f'SELECT * FROM {table} '
                        f'WHERE tenant_id = $1 AND "{pk}" = $2 LIMIT 1',
                        tenant_id, ticket_id)
            except Exception as exc:                              # noqa: BLE001
                raise OneOpsError("uc05.db_store: get_ticket failed",
                                  cause=exc) from exc
            if row is None:
                raise KeyError(ticket_id)
            return dict(row)

    async def list_all(
        self, *, service_id: str, tenant_id: str
    ) -> list[dict[str, Any]]:
        """All non-closed tickets for a service in this tenant (the queue then
        applies `is_in_queue`/`missing_uc5_fields` in Python — same contract as
        the JSON store). Server-side closed-status filter bounds the result."""
        table, _pk = self._resolve(service_id)
        with span("uc05.store.list_all",
                  **{"oneops.tenant_id": tenant_id,
                     "uc05.service_id": service_id, "uc05.store": "postgres"}):
            pool = await self._ensure_pool()
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        f'SELECT * FROM {table} '
                        f'WHERE tenant_id = $1 AND COALESCE(status, $2) <> ALL($3::text[])',
                        tenant_id, "", list(CLOSED_STATUSES))
            except Exception as exc:                              # noqa: BLE001
                raise OneOpsError("uc05.db_store: list_all failed",
                                  cause=exc) from exc
            return [dict(r) for r in rows]

    # ── write (triage apply) ──────────────────────────────────────────────

    async def apply(
        self,
        *,
        service_id: str,
        ticket_id: str,
        tenant_id: str,
        final_values: Mapping[str, Any],
        sla_due: datetime,
        actor_user_id: str,
        now: datetime | None = None,
    ) -> None:
        """Persist the approved triage values. Whitelisted columns only.
        Optimistic-locked on `category IS NULL`. KeyError if no such ticket;
        RuntimeError if it was already triaged (lost the race)."""
        table, pk = self._resolve(service_id)
        allowed = set(writable_fields_for(service_id))
        bad = [k for k in final_values if k not in allowed]
        if bad:
            raise ValueError(
                f"DbStore.apply: refusing to write non-writable columns {bad} "
                f"for {service_id}; allowed: {sorted(allowed)}")
        when = now or datetime.now(UTC)

        with span("uc05.store.apply",
                  **{"oneops.tenant_id": tenant_id,
                     "oneops.user_id": actor_user_id,
                     "uc05.service_id": service_id,
                     "uc05.ticket_id": ticket_id, "uc05.store": "postgres"}):
            set_parts: list[str] = []
            args: list[Any] = []
            i = 1
            for col, val in final_values.items():
                set_parts.append(f'"{col}" = ${i}'); args.append(val); i += 1
            set_parts.append(f"sla_due = ${i}"); args.append(sla_due); i += 1
            set_parts.append(f"status = ${i}"); args.append("assigned"); i += 1
            set_parts.append(f"updated_at = ${i}"); args.append(when); i += 1
            tenant_ph = i; args.append(tenant_id); i += 1
            pk_ph = i; args.append(ticket_id); i += 1
            sql = (
                f"UPDATE {table} SET {', '.join(set_parts)} "
                f'WHERE tenant_id = ${tenant_ph} AND "{pk}" = ${pk_ph} '
                f"AND category IS NULL")
            pool = await self._ensure_pool()
            try:
                async with pool.acquire() as conn:
                    res = await conn.execute(sql, *args)
                    affected = int(str(res).split()[-1]) if res else 0
                    if affected == 0:
                        exists = await conn.fetchval(
                            f'SELECT 1 FROM {table} '
                            f'WHERE tenant_id = $1 AND "{pk}" = $2',
                            tenant_id, ticket_id)
                        if exists:
                            raise RuntimeError(
                                f"{ticket_id} already triaged (optimistic-lock "
                                f"conflict: category no longer NULL)")
                        raise KeyError(ticket_id)
            except (KeyError, RuntimeError, ValueError):
                raise
            except Exception as exc:                              # noqa: BLE001
                raise OneOpsError("uc05.db_store: apply failed",
                                  cause=exc) from exc


__all__ = ["DbStore"]
