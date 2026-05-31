"""DragonflyHotWindow — the production `HotWindow` (Redis-protocol cache).

The hot window for one session is a Redis list at
`oneops:session:hot:{tenant_id}:{session_id}` — tenant in the key, so a
consumer for one tenant can never read another's window (isolation by
construction). Each list element is a protobuf `ConversationEvent` payload.

The list is size-bounded by `LTRIM` on every push and given a sliding TTL, so
an idle session's window expires on its own. A cache miss (key absent) returns
`None` — the store rebuilds the window from the cold log and calls `replace()`.

NOTE: exercised against a real Dragonfly only in the env-gated integration
suite. Unit tests use `InMemoryHotWindow`.
"""
from __future__ import annotations

from typing import Any

from oneops.config import get_settings
from oneops.session.backend import ConversationEvent

_KEY_PREFIX = "oneops:session:hot"


def _hot_key(tenant_id: str, session_id: str) -> str:
    if not tenant_id or not session_id:
        raise ValueError("tenant_id and session_id are mandatory")
    return f"{_KEY_PREFIX}:{tenant_id}:{session_id}"


class DragonflyHotWindow:
    """Bounded, TTL'd hot cache of recent conversation events.

    Args:
        client: an async Redis-protocol client (`redis.asyncio.Redis`).
            Injected so tests and callers control the connection.
        ttl_seconds: sliding expiry refreshed on every write.
    """

    def __init__(self, client: Any, *, ttl_seconds: int | None = None) -> None:
        self._redis = client
        if ttl_seconds is None:
            ttl_seconds = int(getattr(get_settings(), "cache_default_ttl_seconds", 300))
        self._ttl = ttl_seconds

    @classmethod
    def from_settings(cls) -> DragonflyHotWindow:
        """Build a window over a client constructed from `DRAGONFLY_URL`."""
        import redis.asyncio as aioredis

        url = get_settings().dragonfly_url
        return cls(aioredis.from_url(url))

    async def push(self, tenant_id: str, session_id: str,
                  event: ConversationEvent, *, max_events: int) -> None:
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        key = _hot_key(tenant_id, session_id)
        # Only extend an existing window. If the key is absent the window has
        # never been built (or has expired) — do not fabricate a partial one
        # from a single event; leave it a miss so the store rebuilds from cold.
        if not await self._redis.exists(key):
            return
        pipe = self._redis.pipeline()
        pipe.rpush(key, event.SerializeToString())
        pipe.ltrim(key, -max_events, -1)              # keep the newest max_events
        pipe.expire(key, self._ttl)                   # sliding TTL
        await pipe.execute()

    async def window(self, tenant_id: str, session_id: str) -> list[ConversationEvent] | None:
        key = _hot_key(tenant_id, session_id)
        if not await self._redis.exists(key):
            return None                               # genuine miss
        raw = await self._redis.lrange(key, 0, -1)
        await self._redis.expire(key, self._ttl)      # read refreshes the TTL
        return [_parse(b) for b in raw]

    async def replace(self, tenant_id: str, session_id: str,
                      events: list[ConversationEvent]) -> None:
        key = _hot_key(tenant_id, session_id)
        pipe = self._redis.pipeline()
        pipe.delete(key)
        if events:
            pipe.rpush(key, *(e.SerializeToString() for e in events))
            pipe.expire(key, self._ttl)
        await pipe.execute()

    async def evict(self, tenant_id: str, session_id: str) -> None:
        await self._redis.delete(_hot_key(tenant_id, session_id))


def _parse(raw: bytes) -> ConversationEvent:
    event = ConversationEvent()
    event.ParseFromString(raw)
    return event


__all__ = ["DragonflyHotWindow"]
