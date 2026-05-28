"""Tests for G1 — single-flight cache-stampede protection.

`get_or_single_flight` must ensure that N concurrent first-time requests
for the same key trigger the expensive `compute()` exactly ONCE; the
other N-1 wait for the first to populate the cache and read the result.
"""
from __future__ import annotations

import asyncio

import pytest


class _FakeDragonfly:
    """Dict-backed cache with working SET NX semantics for lock testing."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None  # NX: key exists → not set
        self.store[key] = value
        return True

    async def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)
        return 1


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeDragonfly()

    async def _get_client():
        return fake

    monkeypatch.setattr("oneops.adapters.dragonfly_ops.get_redis_client", _get_client)
    return fake


@pytest.mark.asyncio
async def test_single_flight_computes_once_under_concurrency(fake_redis):
    """10 concurrent requests for the same cold key → compute() runs ONCE."""
    from oneops.adapters.dragonfly_ops import get_or_single_flight

    compute_calls = 0

    async def compute():
        nonlocal compute_calls
        compute_calls += 1
        await asyncio.sleep(0.05)  # simulate a slow LLM call
        return {"answer": 42}

    async def one_request():
        return await get_or_single_flight(
            key="sai:test:sf:hot-key",
            keyspace="test",
            compute=compute,
            serialize=lambda v: __import__("orjson").dumps(v),
            deserialize=lambda b: __import__("orjson").loads(b),
            ttl_seconds=60,
            lock_ttl_seconds=10,
            wait_poll_seconds=0.02,
            wait_max_seconds=5.0,
        )

    results = await asyncio.gather(*(one_request() for _ in range(10)))

    # Every caller gets the correct value...
    assert all(r == {"answer": 42} for r in results)
    # ...but compute() ran exactly once — the other 9 waited for the cache.
    assert compute_calls == 1


@pytest.mark.asyncio
async def test_single_flight_cache_hit_skips_compute(fake_redis):
    """A warm key returns immediately without calling compute()."""
    from oneops.adapters.dragonfly_ops import get_or_single_flight
    import orjson

    # Pre-warm the cache.
    fake_redis.store["sai:test:sf:warm"] = orjson.dumps({"answer": 7})

    compute_calls = 0

    async def compute():
        nonlocal compute_calls
        compute_calls += 1
        return {"answer": 999}

    result = await get_or_single_flight(
        key="sai:test:sf:warm",
        keyspace="test",
        compute=compute,
        serialize=orjson.dumps,
        deserialize=orjson.loads,
        ttl_seconds=60,
    )
    assert result == {"answer": 7}
    assert compute_calls == 0


@pytest.mark.asyncio
async def test_single_flight_none_result_not_cached(fake_redis):
    """compute() returning None (error) must NOT be cached — next call retries."""
    from oneops.adapters.dragonfly_ops import get_or_single_flight
    import orjson

    compute_calls = 0

    async def compute():
        nonlocal compute_calls
        compute_calls += 1
        return None  # simulate an error outcome

    for _ in range(2):
        result = await get_or_single_flight(
            key="sai:test:sf:errkey",
            keyspace="test",
            compute=compute,
            serialize=orjson.dumps,
            deserialize=orjson.loads,
            ttl_seconds=60,
        )
        assert result is None
    # None is never cached → both calls re-computed.
    assert compute_calls == 2
