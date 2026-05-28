"""Integration tests for the Dragonfly adapter.

Requires a running Dragonfly at $DRAGONFLY_URL. Skipped automatically when down.
Verifies concurrency safety: 50 concurrent set/get round-trips.
"""
from __future__ import annotations

import asyncio
import os
from urllib.parse import urlparse

import pytest

from oneops.adapters.dragonfly import get_redis_client, shutdown_redis_client
from tests.conftest import has_service


def _dragonfly_reachable() -> bool:
    url = urlparse(os.getenv("DRAGONFLY_URL", "redis://localhost:6379/0"))
    return has_service(url.hostname or "localhost", url.port or 6379)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _dragonfly_reachable(), reason="Dragonfly not running"),
]


@pytest.mark.asyncio
async def test_get_redis_client_is_singleton() -> None:
    a = await get_redis_client()
    b = await get_redis_client()
    assert a is b
    await shutdown_redis_client()


@pytest.mark.asyncio
async def test_ping() -> None:
    client = await get_redis_client()
    try:
        assert await client.ping() is True
    finally:
        await shutdown_redis_client()


@pytest.mark.asyncio
async def test_concurrent_writes_and_reads() -> None:
    """50 concurrent set/get pairs. Verifies the pool serves them without errors."""
    client = await get_redis_client()
    try:
        async def round_trip(i: int) -> tuple[int, str]:
            key = f"oneops:test:concurrent:{i}"
            await client.set(key, f"v{i}".encode())
            got = await client.get(key)
            await client.delete(key)
            return i, got.decode()

        results = await asyncio.gather(*(round_trip(i) for i in range(50)))
        for i, v in results:
            assert v == f"v{i}"
    finally:
        await shutdown_redis_client()
