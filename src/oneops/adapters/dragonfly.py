"""Dragonfly (Redis-compatible) async client.

Per-event-loop connection pool. Concurrency-safe across tasks on the same
loop. A `redis.asyncio.Redis` connection pool is bound to the event loop
that creates it (its `asyncio.Lock`s and `StreamReader/StreamWriter` objects
are loop-bound); reusing that client from a different loop raises
"Event loop is closed".

In production a single persistent event loop hosts the whole service, so
this behaves as a process-wide singleton. In tests, pytest-asyncio creates
a fresh loop per test; each loop transparently gets its own client and
old clients are GC'd by Python (or torn down via `shutdown_redis_client`).

All consumers (session store, response cache, replay cache, single-flight
locks) go through this one helper.

Lifecycle:
    redis = await get_redis_client()
    await shutdown_redis_client()        # tears down the current loop's client
    await shutdown_all_redis_clients()   # tears down every loop's client
"""
from __future__ import annotations

import asyncio
import threading
import weakref

import redis.asyncio as redis

from oneops.config import get_settings
from oneops.errors import CacheUnavailableError
from oneops.observability import get_logger

_log = get_logger("oneops.adapters.dragonfly")

# One client per event loop. The loop is the cache key. We hold a weak ref
# to the loop so that loop garbage-collection drops the entry; this prevents
# a long-running test session from accumulating dead pools.
_clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, redis.Redis] = (
    weakref.WeakKeyDictionary()
)
_lock = threading.Lock()


async def get_redis_client() -> redis.Redis:
    """Get-or-create the Dragonfly client for the current event loop."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is not None:
        return client

    with _lock:
        client = _clients.get(loop)
        if client is not None:
            return client
        settings = get_settings()
        try:
            client = redis.from_url(
                settings.dragonfly_url,
                max_connections=settings.dragonfly_pool_max,
                decode_responses=False,  # we handle bytes ourselves (orjson)
                socket_connect_timeout=5.0,
                socket_keepalive=True,
                health_check_interval=30,
            )
            # Verify connectivity at first use; fail fast on bad config.
            await client.ping()
            _clients[loop] = client
            # Probe what's actually serving us. `INFO server` reports a
            # `dragonfly_version` field when Dragonfly responds; plain
            # Redis omits it. Useful when host port 6379 is bound by a
            # bystander (e.g. oneops-redis-tmp) instead of Dragonfly —
            # the app keeps working (Redis API surface is sufficient)
            # but operators should know the substitution happened.
            kind = "unknown"
            redis_version = ""
            try:
                info = await client.info("server")
                if isinstance(info, dict):
                    if info.get("dragonfly_version"):
                        kind = "dragonfly"
                    elif info.get("redis_version"):
                        kind = "redis"
                    redis_version = str(
                        info.get("dragonfly_version")
                        or info.get("redis_version")
                        or ""
                    )
            except Exception:  # noqa: BLE001 — probe must not block connect
                pass
            _log.info(
                "dragonfly.connected",
                url=settings.dragonfly_url.split("@")[-1],
                pool_max=settings.dragonfly_pool_max,
                loop_id=id(loop),
                kind=kind,
                version=redis_version,
            )
            return client
        except (redis.RedisError, OSError) as e:
            raise CacheUnavailableError(f"cannot connect to Dragonfly: {e}", cause=e) from e


async def shutdown_redis_client() -> None:
    """Close the current loop's Dragonfly client. Idempotent."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — nothing per-loop to close.
        return
    client = _clients.pop(loop, None)
    if client is not None:
        try:
            await client.aclose()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — shutdown must not raise
            _log.warning("dragonfly.shutdown_failed", error=str(exc))


async def shutdown_all_redis_clients() -> None:
    """Close every event-loop-bound client. For full process teardown.

    Only safe to call when no other coroutines on those loops are using the
    clients. Test suites use it in session-scope teardown.
    """
    # Snapshot then drain — avoid mutating WeakKeyDictionary while iterating.
    with _lock:
        items = list(_clients.items())
        _clients.clear()
    for _loop, client in items:
        try:
            await client.aclose()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — shutdown must not raise
            _log.warning("dragonfly.shutdown_all_failed", error=str(exc))


__all__ = [
    "get_redis_client",
    "shutdown_redis_client",
    "shutdown_all_redis_clients",
]
