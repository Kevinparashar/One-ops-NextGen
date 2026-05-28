"""PostgresEventLog — the durable, append-only cold log (production `EventLog`).

Schema: `migrations/0001_conversation_events.sql`. The table is append-only —
this class issues exactly one `INSERT` (append), `SELECT`s (read), and a
retention `DELETE` (prune). There is no `UPDATE` path: a conversation event,
once written, is immutable.

Tenant isolation is by construction — every statement filters on `tenant_id`,
which is the leading column of the primary read index. There is no query here
that can return another tenant's rows.

Events are stored as the protobuf `ConversationEvent` payload (ADR-0001) in a
`BYTEA` column — the on-disk shape is the same contract as the on-wire shape.

NOTE: this module is only exercised against a real Postgres in the env-gated
integration suite. Unit tests use `InMemoryEventLog`.
"""
from __future__ import annotations

from oneops.adapters.postgres import get_pg_pool
from oneops.observability import get_logger
from oneops.session.backend import ConversationEvent

_log = get_logger("oneops.session.postgres_log")

_INSERT = """
    INSERT INTO conversation_events
        (tenant_id, session_id, turn_index, turn_role, event_bytes, occurred_at_ms)
    VALUES ($1, $2, $3, $4, $5, $6)
    RETURNING seq
"""

_SELECT = """
    SELECT event_bytes
    FROM conversation_events
    WHERE tenant_id = $1 AND session_id = $2 AND turn_index >= $3
    ORDER BY seq ASC
"""

_PRUNE = """
    DELETE FROM conversation_events
    WHERE tenant_id = $1 AND occurred_at_ms < $2
"""


class PostgresEventLog:
    """Durable append-only conversation log backed by Postgres."""

    async def append(self, tenant_id: str, session_id: str,
                      event: ConversationEvent) -> int:
        if not tenant_id or not session_id:
            raise ValueError("tenant_id and session_id are mandatory")
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            seq = await conn.fetchval(
                _INSERT, tenant_id, session_id, int(event.turn_index),
                event.turn_role, event.SerializeToString(),
                int(event.occurred_at_unix_ms),
            )
        return int(seq)

    async def read(self, tenant_id: str, session_id: str, *,
                   from_turn: int = 0) -> list[ConversationEvent]:
        if not tenant_id or not session_id:
            raise ValueError("tenant_id and session_id are mandatory")
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SELECT, tenant_id, session_id, int(from_turn))
        events: list[ConversationEvent] = []
        for row in rows:
            ev = ConversationEvent()
            ev.ParseFromString(row["event_bytes"])
            events.append(ev)
        return events

    async def prune(self, tenant_id: str, *, older_than_unix_ms: int) -> int:
        if not tenant_id:
            raise ValueError("tenant_id is mandatory")
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            # asyncpg returns a status string like "DELETE 42".
            status = await conn.execute(_PRUNE, tenant_id, int(older_than_unix_ms))
        removed = int(status.split()[-1]) if status and status[-1].isdigit() else 0
        if removed:
            _log.info("session.postgres_log.pruned", tenant_id=tenant_id, removed=removed)
        return removed


__all__ = ["PostgresEventLog"]
