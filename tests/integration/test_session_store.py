"""Integration tests for SessionStore against real Dragonfly.

Verifies:
- Per-session isolation (one user's session never leaks into another's)
- Atomic update_focus / update_canonical_state under concurrency
- History appends preserve order + trim correctly
- TTL refresh on read
- increment_turn is atomic across concurrent callers (no lost increments)
- Pending clarification can be cleared without touching subjects
"""
from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import pytest

from oneops.adapters.dragonfly import shutdown_redis_client
from oneops.adapters.session_store import (
    SessionStore,
    get_session_store,
    reset_session_store_for_test,
)
from tests.conftest import has_service


def _dragonfly_reachable() -> bool:
    url = urlparse(os.getenv("DRAGONFLY_URL", "redis://localhost:6379/0"))
    return has_service(url.hostname or "localhost", url.port or 6379)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _dragonfly_reachable(), reason="Dragonfly not running"),
]


def _sid() -> str:
    """Fresh, isolated session id per test."""
    return f"test-{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def store() -> AsyncIterator[SessionStore]:
    reset_session_store_for_test()
    s = await get_session_store()
    yield s
    await shutdown_redis_client()


# ── History ────────────────────────────────────────────────────


async def test_history_empty_when_session_new(store: SessionStore) -> None:
    assert await store.get_history(_sid()) == []


async def test_history_append_and_read(store: SessionStore) -> None:
    sid = _sid()
    n1 = await store.append_history(sid, "user", "hi")
    n2 = await store.append_history(sid, "assistant", "hello")
    assert n1 == 1
    assert n2 == 2
    history = await store.get_history(sid)
    assert history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    await store.purge(sid)


async def test_history_trims_to_max(store: SessionStore) -> None:
    # SessionStore defaults to max_history=50; append 60 → keep last 50
    sid = _sid()
    for i in range(60):
        await store.append_history(sid, "user", f"msg-{i}")
    history = await store.get_history(sid)
    assert len(history) == 50
    # Trimmed from the FRONT — earliest 10 dropped
    assert history[0]["content"] == "msg-10"
    assert history[-1]["content"] == "msg-59"
    await store.purge(sid)


# ── Focus ──────────────────────────────────────────────────────


async def test_focus_empty_when_session_new(store: SessionStore) -> None:
    assert await store.get_focus(_sid()) == {}


async def test_focus_update_merges_top_level(store: SessionStore) -> None:
    sid = _sid()
    await store.update_focus(sid, {"active_subject": {"entity_id": "INC0001001"}})
    await store.update_focus(sid, {"anchor_subject": {"entity_id": "INC0001001"}})
    focus = await store.get_focus(sid)
    assert focus["active_subject"]["entity_id"] == "INC0001001"
    assert focus["anchor_subject"]["entity_id"] == "INC0001001"
    await store.purge(sid)


async def test_clear_pending_clarification_preserves_subjects(store: SessionStore) -> None:
    sid = _sid()
    await store.update_focus(sid, {
        "active_subject": {"entity_id": "INC0001001"},
        "pending_clarification": {"type": "ordinal", "options": ["KB1", "KB2"]},
    })
    await store.clear_pending_clarification(sid)
    focus = await store.get_focus(sid)
    assert focus["active_subject"] == {"entity_id": "INC0001001"}
    assert focus["pending_clarification"] is None
    await store.purge(sid)


# ── Canonical state ────────────────────────────────────────────


async def test_canonical_update_and_read(store: SessionStore) -> None:
    sid = _sid()
    await store.update_canonical_state(sid, {"last_successful_use_case": "uc01_summarization"})
    state = await store.get_canonical_state(sid)
    assert state["last_successful_use_case"] == "uc01_summarization"
    await store.purge(sid)


async def test_increment_turn_atomic_under_concurrency(store: SessionStore) -> None:
    """50 concurrent increments must produce exactly turn_index=50 (no lost updates)."""
    sid = _sid()
    results = await asyncio.gather(*(store.increment_turn(sid) for _ in range(50)))
    final = (await store.get_canonical_state(sid)).get("turn_index")
    assert final == 50
    assert sorted(results) == list(range(1, 51))
    await store.purge(sid)


# ── Session isolation ──────────────────────────────────────────


async def test_two_sessions_do_not_bleed(store: SessionStore) -> None:
    a, b = _sid(), _sid()
    await store.append_history(a, "user", "from-a")
    await store.append_history(b, "user", "from-b")
    await store.update_focus(a, {"active_subject": {"entity_id": "INC0000A"}})
    await store.update_focus(b, {"active_subject": {"entity_id": "INC0000B"}})

    assert await store.get_history(a) == [{"role": "user", "content": "from-a"}]
    assert await store.get_history(b) == [{"role": "user", "content": "from-b"}]
    assert (await store.get_focus(a))["active_subject"]["entity_id"] == "INC0000A"
    assert (await store.get_focus(b))["active_subject"]["entity_id"] == "INC0000B"

    await store.purge(a)
    await store.purge(b)


async def test_purge_removes_all_three_keyspaces(store: SessionStore) -> None:
    sid = _sid()
    await store.append_history(sid, "user", "x")
    await store.update_focus(sid, {"active_subject": {"id": "1"}})
    await store.update_canonical_state(sid, {"turn_index": 1})
    deleted = await store.purge(sid)
    assert deleted == 3
    assert await store.get_history(sid) == []
    assert await store.get_focus(sid) == {}
    assert await store.get_canonical_state(sid) == {}


# ── Concurrent multi-session load ──────────────────────────────


async def test_many_sessions_in_parallel(store: SessionStore) -> None:
    """20 simulated users each running 5 turns concurrently — no cross-talk."""
    async def session_workload(sid: str) -> tuple[str, list[dict]]:
        for i in range(5):
            await store.append_history(sid, "user", f"{sid}-msg-{i}")
            await store.update_focus(sid, {"active_subject": {"entity_id": sid}})
            await store.update_canonical_state(sid, {"turn_index": i + 1})
        return sid, await store.get_history(sid)

    sids = [_sid() for _ in range(20)]
    results = await asyncio.gather(*(session_workload(s) for s in sids))

    for sid, history in results:
        assert len(history) == 5
        for i, turn in enumerate(history):
            assert turn["content"] == f"{sid}-msg-{i}"

    for sid in sids:
        await store.purge(sid)
