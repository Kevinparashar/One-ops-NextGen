"""Integration test — SessionEventStore over real Postgres + Dragonfly.

GATED. This test connects to a live Postgres and a live Dragonfly. It runs
ONLY when the operator explicitly opts in:

    RUN_SESSION_INTEGRATION=1 \
    SESSION_TEST_POSTGRES_URL=postgresql://.../oneops_test \
    SESSION_TEST_DRAGONFLY_URL=redis://localhost:6379/15 \
    .venv/bin/python -m pytest tests/integration/test_session_store_pg.py

Prerequisites the operator must satisfy first:
  * a **dedicated test database** (never a shared/production DB);
  * `migrations/0001_conversation_events.sql` applied to it;
  * a Dragonfly/Redis instance reachable at the test URL.

Without `RUN_SESSION_INTEGRATION=1` the whole module is skipped at collection
time — pytest never opens a connection. The unit suite
(`tests/unit/session/test_store.py`) gives full logic coverage against the
in-memory backends; this test verifies the live wire-up only.
"""
from __future__ import annotations

import os
import time
import uuid

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_SESSION_INTEGRATION") != "1",
        reason="set RUN_SESSION_INTEGRATION=1 (+ test DSNs) to run — needs a "
               "dedicated test Postgres and a Dragonfly instance",
    ),
]


def _event(turn: int, content: str):
    from oneops.session.backend import ConversationEvent
    return ConversationEvent(
        session_id="s", turn_role="user", content=content, turn_index=turn,
        occurred_at_unix_ms=int(time.time() * 1000))


@pytest.fixture
async def store():
    """A SessionEventStore over the real Postgres log + Dragonfly window,
    pointed at the operator-supplied test DSNs."""
    import redis.asyncio as aioredis

    from oneops.session.backend import ConversationEvent  # noqa: F401
    from oneops.session.dragonfly_window import DragonflyHotWindow
    from oneops.session.postgres_log import PostgresEventLog
    from oneops.session.store import RetentionPolicy, SessionEventStore

    pg_dsn = os.environ["SESSION_TEST_POSTGRES_URL"]
    df_url = os.environ["SESSION_TEST_DRAGONFLY_URL"]

    # PostgresEventLog uses the shared adapter pool; the adapter reads
    # POSTGRES_URL, so the operator points that env at the test DB for the run.
    redis_client = aioredis.from_url(df_url)
    yield SessionEventStore(
        PostgresEventLog(), DragonflyHotWindow(redis_client),
        RetentionPolicy(hot_window_events=5, cold_retention_days=1),
    ), pg_dsn
    await redis_client.aclose()


async def test_append_recent_replay_round_trip(store):
    s, _ = store
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    session = f"s-{uuid.uuid4().hex[:8]}"
    for t in range(1, 8):
        await s.append(tenant, session, _event(t, f"msg-{t}"))

    recent = await s.recent(tenant, session)
    assert [e.turn_index for e in recent] == [3, 4, 5, 6, 7]   # window bound = 5

    full = await s.replay(tenant, session)
    assert [e.turn_index for e in full] == [1, 2, 3, 4, 5, 6, 7]


async def test_two_tenants_are_isolated_on_real_backends(store):
    s, _ = store
    session = f"s-{uuid.uuid4().hex[:8]}"
    ta, tb = f"t-{uuid.uuid4().hex[:8]}", f"t-{uuid.uuid4().hex[:8]}"
    await s.append(ta, session, _event(1, "from-A"))
    await s.append(tb, session, _event(1, "from-B"))

    assert [e.content for e in await s.replay(ta, session)] == ["from-A"]
    assert [e.content for e in await s.replay(tb, session)] == ["from-B"]


async def test_hot_window_miss_rebuilds_from_cold(store):
    s, _ = store
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    session = f"s-{uuid.uuid4().hex[:8]}"
    await s.append(tenant, session, _event(1, "one"))
    await s.append(tenant, session, _event(2, "two"))
    # Evict the hot window, then read — the store must rebuild from Postgres.
    await s._hot.evict(tenant, session)              # noqa: SLF001 — test reaches in deliberately
    rebuilt = await s.recent(tenant, session)
    assert [e.content for e in rebuilt] == ["one", "two"]


async def test_retention_prune_on_real_postgres(store):
    s, _ = store
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    session = f"s-{uuid.uuid4().hex[:8]}"
    old = int(time.time() * 1000) - 5 * 86_400_000
    from oneops.session.backend import ConversationEvent
    await s.append(tenant, session, ConversationEvent(
        session_id=session, turn_role="user", content="old",
        turn_index=1, occurred_at_unix_ms=old))
    await s.append(tenant, session, _event(2, "fresh"))

    removed = await s.apply_retention(tenant)
    assert removed == 1
    assert [e.content for e in await s.replay(tenant, session)] == ["fresh"]
