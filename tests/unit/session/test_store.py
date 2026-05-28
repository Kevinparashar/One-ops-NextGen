"""Unit tests for SessionEventStore.

Run against the in-memory `EventLog` / `HotWindow` backends — real
implementations of those Protocols, not mocks. The system under test is the
store's composition logic (durable-first ordering, cache-miss rebuild,
retention, tenant isolation); the backends are genuine dependencies it drives
exactly as it drives the Postgres / Dragonfly ones.
"""
from __future__ import annotations

import time

import pytest

from oneops.session import (
    InMemoryEventLog,
    InMemoryHotWindow,
    RetentionPolicy,
    SessionEventStore,
)
from oneops.session.backend import ConversationEvent

pytestmark = pytest.mark.asyncio


def _event(turn: int, role: str = "user", content: str = "", *,
           occurred_at_ms: int | None = None) -> ConversationEvent:
    return ConversationEvent(
        session_id="s-1", turn_role=role, content=content or f"msg-{turn}",
        turn_index=turn,
        occurred_at_unix_ms=occurred_at_ms if occurred_at_ms is not None
        else int(time.time() * 1000))


def _store(*, hot_window_events: int = 40, cold_retention_days: int = 90) -> SessionEventStore:
    return SessionEventStore(
        InMemoryEventLog(), InMemoryHotWindow(),
        RetentionPolicy(hot_window_events=hot_window_events,
                        cold_retention_days=cold_retention_days))


# ── append ───────────────────────────────────────────────────────────────


async def test_append_returns_monotonic_sequence():
    store = _store()
    s1 = await store.append("tenant-a", "s-1", _event(1))
    s2 = await store.append("tenant-a", "s-1", _event(2))
    assert s2 > s1


async def test_append_writes_to_cold_log():
    """Durable-first: after append, the event is in the cold log (replay)."""
    store = _store()
    await store.append("tenant-a", "s-1", _event(1, content="hello"))
    history = await store.replay("tenant-a", "s-1")
    assert [e.content for e in history] == ["hello"]


# ── recent / hot window ──────────────────────────────────────────────────


async def test_recent_rebuilds_from_cold_on_miss():
    """First read is a hot miss — the window is rebuilt from the cold log."""
    store = _store()
    for t in range(1, 4):
        await store.append("tenant-a", "s-1", _event(t))
    window = await store.recent("tenant-a", "s-1")
    assert [e.turn_index for e in window] == [1, 2, 3]


async def test_recent_is_served_from_hot_window_after_warmup():
    store = _store()
    await store.append("tenant-a", "s-1", _event(1))
    await store.recent("tenant-a", "s-1")            # miss → rebuilds + caches
    # Append again — push extends the now-existing hot window.
    await store.append("tenant-a", "s-1", _event(2))
    window = await store.recent("tenant-a", "s-1")   # hot hit
    assert [e.turn_index for e in window] == [1, 2]


async def test_recent_window_is_size_bounded():
    store = _store(hot_window_events=3)
    for t in range(1, 6):                            # 5 events, window holds 3
        await store.append("tenant-a", "s-1", _event(t))
    window = await store.recent("tenant-a", "s-1")
    assert [e.turn_index for e in window] == [3, 4, 5]


async def test_recent_on_empty_session_returns_empty():
    store = _store()
    assert await store.recent("tenant-a", "s-new") == []


# ── replay ───────────────────────────────────────────────────────────────


async def test_replay_returns_full_history_regardless_of_window_bound():
    store = _store(hot_window_events=2)              # tiny hot window
    for t in range(1, 6):
        await store.append("tenant-a", "s-1", _event(t))
    full = await store.replay("tenant-a", "s-1")
    assert [e.turn_index for e in full] == [1, 2, 3, 4, 5]   # all 5, not 2


async def test_replay_from_turn_filters():
    store = _store()
    for t in range(1, 6):
        await store.append("tenant-a", "s-1", _event(t))
    tail = await store.replay("tenant-a", "s-1", from_turn=3)
    assert [e.turn_index for e in tail] == [3, 4, 5]


# ── retention ────────────────────────────────────────────────────────────


async def test_apply_retention_prunes_events_past_the_horizon():
    store = _store(cold_retention_days=1)
    now = int(time.time() * 1000)
    old = now - 5 * 86_400_000                       # 5 days old — past the 1-day horizon
    await store.append("tenant-a", "s-1", _event(1, occurred_at_ms=old))
    await store.append("tenant-a", "s-1", _event(2, occurred_at_ms=now))

    removed = await store.apply_retention("tenant-a")
    assert removed == 1
    survivors = await store.replay("tenant-a", "s-1")
    assert [e.turn_index for e in survivors] == [2]  # only the recent event remains


async def test_apply_retention_keeps_everything_within_horizon():
    store = _store(cold_retention_days=90)
    now = int(time.time() * 1000)
    await store.append("tenant-a", "s-1", _event(1, occurred_at_ms=now))
    assert await store.apply_retention("tenant-a") == 0


# ── tenant isolation ─────────────────────────────────────────────────────


async def test_two_tenants_sharing_a_session_id_are_isolated():
    """Same session_id, different tenants — neither sees the other's events."""
    store = _store()
    await store.append("tenant-a", "s-shared", _event(1, content="from-A"))
    await store.append("tenant-b", "s-shared", _event(1, content="from-B"))

    a = await store.replay("tenant-a", "s-shared")
    b = await store.replay("tenant-b", "s-shared")
    assert [e.content for e in a] == ["from-A"]
    assert [e.content for e in b] == ["from-B"]


async def test_retention_prunes_only_the_named_tenant():
    store = _store(cold_retention_days=1)
    old = int(time.time() * 1000) - 5 * 86_400_000
    await store.append("tenant-a", "s-1", _event(1, occurred_at_ms=old))
    await store.append("tenant-b", "s-1", _event(1, occurred_at_ms=old))

    await store.apply_retention("tenant-a")
    assert await store.replay("tenant-a", "s-1") == []
    # tenant-b's equally-old event is untouched — retention is per-tenant.
    assert len(await store.replay("tenant-b", "s-1")) == 1


async def test_tenant_id_is_mandatory():
    store = _store()
    with pytest.raises(ValueError, match="tenant_id"):
        await store.append("", "s-1", _event(1))
    with pytest.raises(ValueError, match="tenant_id"):
        await store.recent("", "s-1")
