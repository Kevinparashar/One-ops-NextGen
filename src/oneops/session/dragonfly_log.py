"""DragonflyEventLog — durable conversation log over a Redis-protocol cluster.

Per-session events live as a Redis list at
`oneops:session:log:{tenant_id}:{session_id}`. Each list element is a
protobuf `ConversationEvent` payload (ADR-0001) — same on-disk shape as
`PostgresEventLog`, swap-in compatible.

Why this exists alongside `PostgresEventLog`:

  * The demo / single-process FaaS deployment needs the session log to
    survive a uvicorn restart WITHOUT requiring a Postgres `conversation_events`
    table (which would need DDL on the shared app DB — refused by ADR-0004).
  * Dragonfly is already provisioned for the summary cache + the hot
    window; reusing it as the cold log keeps the infra surface small.
  * Production deployments that need true cross-container durability flip
    `ONEOPS_SESSION_BACKEND` to `postgres+dragonfly` and the same Protocol
    seam routes to `PostgresEventLog` instead — no caller change.

Tenant isolation: every read and write is keyed by `(tenant_id, session_id)`.
There is no query in this module that can return another tenant's events.

Append is `RPUSH`; read is `LRANGE` filtered by `turn_index >= from_turn`
(the protobuf's `turn_index` is the source of truth). Pruning is `LTRIM`
to a max length per session.
"""
from __future__ import annotations

from typing import Any

from oneops.observability import get_logger
from oneops.session.backend import ConversationEvent

_log = get_logger("oneops.session.dragonfly_log")

# Repeated literals → constants (sonar S1192).
_TENANT_ID_AND_SESSION_ID_ARE_MANDATORY = "tenant_id and session_id are mandatory"

_KEY_PREFIX = "oneops:session:log"


def _log_key(tenant_id: str, session_id: str) -> str:
    if not tenant_id or not session_id:
        raise ValueError(_TENANT_ID_AND_SESSION_ID_ARE_MANDATORY)
    return f"{_KEY_PREFIX}:{tenant_id}:{session_id}"


class DragonflyEventLog:
    """Append-only conversation log over a Redis-protocol Dragonfly.

    Args:
        client: an async Redis-protocol client (`redis.asyncio.Redis`,
            `decode_responses=False` so the protobuf bytes round-trip).
        max_events_per_session: hard upper bound on a single session's
            log size (cheap insurance against a runaway loop). 0 = unlimited.
    """

    def __init__(
        self, client: Any, *, max_events_per_session: int = 10_000,
    ) -> None:
        self._redis = client
        self._max = max_events_per_session

    @classmethod
    def from_settings(cls) -> DragonflyEventLog:
        """Build over a client constructed from `DRAGONFLY_URL`. Must use
        `decode_responses=False` because the protobuf payload is raw bytes."""
        import redis.asyncio as aioredis

        from oneops.config import get_settings
        url = getattr(get_settings(), "dragonfly_url",
                      "redis://localhost:6379/0")
        client = aioredis.from_url(url, decode_responses=False)
        return cls(client)

    async def append(
        self, tenant_id: str, session_id: str, event: ConversationEvent,
    ) -> int:
        """Append one event. Returns the new list length (its sequence
        position). The protobuf's own `turn_index` is what the read path
        uses for ordering; the list length is informational."""
        if not tenant_id or not session_id:
            raise ValueError(_TENANT_ID_AND_SESSION_ID_ARE_MANDATORY)
        payload = event.SerializeToString()
        key = _log_key(tenant_id, session_id)
        # Pipeline so the cap-LTRIM happens in the same round-trip as the
        # append — eliminates a race where a concurrent reader could see
        # an over-cap list.
        pipe = self._redis.pipeline()
        pipe.rpush(key, payload)
        if self._max > 0:
            # LTRIM keeps elements within [-max, -1] inclusive: only the
            # newest `max_events_per_session`.
            pipe.ltrim(key, -self._max, -1)
        results = await pipe.execute()
        new_length = int(results[0]) if results else 0
        return new_length

    async def read(
        self, tenant_id: str, session_id: str, *, from_turn: int = 0,
    ) -> list[ConversationEvent]:
        """Every event for the session with `turn_index >= from_turn`."""
        if not tenant_id or not session_id:
            raise ValueError(_TENANT_ID_AND_SESSION_ID_ARE_MANDATORY)
        key = _log_key(tenant_id, session_id)
        raw_payloads: list[bytes] = await self._redis.lrange(key, 0, -1)
        events: list[ConversationEvent] = []
        for raw in raw_payloads:
            ev = ConversationEvent()
            ev.ParseFromString(raw)
            if ev.turn_index >= from_turn:
                events.append(ev)
        return events

    async def prune(
        self, tenant_id: str, *, older_than_unix_ms: int,
    ) -> int:
        """Prune is approximate over a Redis list: we scan keys per tenant
        and rewrite each session list with only the surviving events. For
        the demo this is rare; production runs this off-line."""
        if not tenant_id:
            raise ValueError("tenant_id is mandatory")
        pattern = f"{_KEY_PREFIX}:{tenant_id}:*"
        removed = 0
        async for key in self._redis.scan_iter(pattern):
            raw_payloads: list[bytes] = await self._redis.lrange(key, 0, -1)
            kept: list[bytes] = []
            for raw in raw_payloads:
                ev = ConversationEvent()
                ev.ParseFromString(raw)
                if ev.occurred_at_unix_ms >= older_than_unix_ms:
                    kept.append(raw)
                else:
                    removed += 1
            pipe = self._redis.pipeline()
            pipe.delete(key)
            if kept:
                pipe.rpush(key, *kept)
            await pipe.execute()
        if removed:
            _log.info(
                "session.dragonfly_log.pruned",
                tenant_id=tenant_id, removed=removed)
        return removed


__all__ = ["DragonflyEventLog"]
