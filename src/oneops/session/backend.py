"""Session-store backend contracts + in-memory implementations.

A session's conversation is an **append-only event log**. The store splits
that into two concerns behind two Protocols:

  * `EventLog`  — the durable, append-only cold log (Postgres in production).
  * `HotWindow` — the bounded hot cache of the most recent events (Dragonfly).

`SessionEventStore` (store.py) composes one of each. The in-memory
implementations here are *real* implementations of those Protocols — used for
unit tests and local dev. They are not mocks of the system under test: the
system under test is `SessionEventStore`'s composition logic, and these
backends are genuine dependencies it drives exactly as it drives the live ones.

Tenant isolation is *by construction*: every method takes `tenant_id` as a
mandatory first argument, and every storage key is composed from it. There is
no code path that reads a session without naming its tenant.
"""
from __future__ import annotations

import threading
from typing import Protocol

from oneops.codec import messages as msg

# An event is the protobuf ConversationEvent contract (ADR-0001).
ConversationEvent = msg.ConversationEvent


def _key(tenant_id: str, session_id: str) -> tuple[str, str]:
    if not tenant_id:
        raise ValueError("tenant_id is mandatory — no session access is tenant-less")
    if not session_id:
        raise ValueError("session_id is mandatory")
    return (tenant_id, session_id)


# ── Protocols ────────────────────────────────────────────────────────────


class EventLog(Protocol):
    """The durable, append-only cold log. The system of record."""

    async def append(self, tenant_id: str, session_id: str,
                      event: ConversationEvent) -> int:
        """Append one event. Returns its monotonic per-session sequence number."""
        ...

    async def read(self, tenant_id: str, session_id: str, *,
                    from_turn: int = 0) -> list[ConversationEvent]:
        """Every event for the session with `turn_index >= from_turn`, in order."""
        ...

    async def prune(self, tenant_id: str, *, older_than_unix_ms: int) -> int:
        """Delete events older than the cutoff. Returns the count removed."""
        ...


class HotWindow(Protocol):
    """The bounded hot cache of the most recent events for a session."""

    async def push(self, tenant_id: str, session_id: str,
                   event: ConversationEvent, *, max_events: int) -> None:
        """Append to the hot window, trimming to the newest `max_events`."""
        ...

    async def window(self, tenant_id: str, session_id: str) -> list[ConversationEvent] | None:
        """The cached window, oldest-first. `None` = cache miss (rebuild from cold)."""
        ...

    async def replace(self, tenant_id: str, session_id: str,
                      events: list[ConversationEvent]) -> None:
        """Replace the whole window — used to rebuild the cache after a miss."""
        ...

    async def evict(self, tenant_id: str, session_id: str) -> None:
        """Drop the session's hot window."""
        ...


# ── In-memory implementations ────────────────────────────────────────────


class InMemoryEventLog:
    """Thread-safe in-memory `EventLog`. Real implementation; not a mock."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # (tenant, session) -> list[(seq, event)]
        self._log: dict[tuple[str, str], list[tuple[int, ConversationEvent]]] = {}
        self._seq = 0

    async def append(self, tenant_id: str, session_id: str,
                      event: ConversationEvent) -> int:
        k = _key(tenant_id, session_id)
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._log.setdefault(k, []).append((seq, _copy(event)))
            return seq

    async def read(self, tenant_id: str, session_id: str, *,
                   from_turn: int = 0) -> list[ConversationEvent]:
        k = _key(tenant_id, session_id)
        with self._lock:
            return [_copy(e) for _, e in self._log.get(k, [])
                    if e.turn_index >= from_turn]

    async def prune(self, tenant_id: str, *, older_than_unix_ms: int) -> int:
        if not tenant_id:
            raise ValueError("tenant_id is mandatory")
        removed = 0
        with self._lock:
            for k, entries in self._log.items():
                if k[0] != tenant_id:
                    continue
                kept = [(s, e) for s, e in entries
                        if e.occurred_at_unix_ms >= older_than_unix_ms]
                removed += len(entries) - len(kept)
                self._log[k] = kept
        return removed


class InMemoryHotWindow:
    """Thread-safe in-memory `HotWindow`. Real implementation; not a mock."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: dict[tuple[str, str], list[ConversationEvent]] = {}

    async def push(self, tenant_id: str, session_id: str,
                  event: ConversationEvent, *, max_events: int) -> None:
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        k = _key(tenant_id, session_id)
        with self._lock:
            win = self._cache.get(k)
            if win is None:
                # No window cached — do not fabricate a partial one from a
                # single event; leave it a miss so the store rebuilds from cold.
                return
            win.append(_copy(event))
            del win[:-max_events]                       # keep newest max_events

    async def window(self, tenant_id: str, session_id: str) -> list[ConversationEvent] | None:
        k = _key(tenant_id, session_id)
        with self._lock:
            win = self._cache.get(k)
            return None if win is None else [_copy(e) for e in win]

    async def replace(self, tenant_id: str, session_id: str,
                      events: list[ConversationEvent]) -> None:
        k = _key(tenant_id, session_id)
        with self._lock:
            self._cache[k] = [_copy(e) for e in events]

    async def evict(self, tenant_id: str, session_id: str) -> None:
        k = _key(tenant_id, session_id)
        with self._lock:
            self._cache.pop(k, None)


def _copy(event: ConversationEvent) -> ConversationEvent:
    """Defensive copy — callers never mutate stored protobuf objects."""
    clone = ConversationEvent()
    clone.CopyFrom(event)
    return clone


__all__ = [
    "ConversationEvent",
    "EventLog",
    "HotWindow",
    "InMemoryEventLog",
    "InMemoryHotWindow",
]
