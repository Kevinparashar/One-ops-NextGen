"""Instrumented Dragonfly operation helpers.

Wraps the bare `redis.asyncio` GET / SET / DEL calls with consistent
metrics emission so every cache layer (UC-1 summary, UC-3 article,
embeddings, session store, …) reports under the same metric names.

Metrics emitted:

  cache.dragonfly.get.total      counter   labels: keyspace
  cache.dragonfly.hit.total      counter   labels: keyspace
  cache.dragonfly.miss.total     counter   labels: keyspace
  cache.dragonfly.set.total      counter   labels: keyspace
  cache.dragonfly.delete.total   counter   labels: keyspace
  cache.dragonfly.error.total    counter   labels: keyspace, op
  cache.dragonfly.latency_ms     histogram labels: keyspace, op

The `keyspace` label is the logical cache name (`"uc01_summary"`,
`"uc03_kb_article"`, `"embed"`, …) — NOT the full Redis key. Keeps
cardinality bounded.

Each helper:
- Uses atomic `SET key value EX ttl` (never `SET` + separate `EXPIRE`).
- Wraps redis errors in try/except — observability never raises into
  business code.
- Returns `None` on miss (GET) or `False` on failure (SET/DEL); callers
  decide degraded behavior.
"""
from __future__ import annotations

import asyncio
import time

from oneops.adapters.dragonfly import get_redis_client
from oneops.observability import get_logger, histogram, increment

_log = get_logger("oneops.adapters.dragonfly_ops")

# Refuse to cache values larger than this. A pathologically large LLM
# output (e.g. a runaway reranker response) should not be stored — it
# bloats Dragonfly memory and slows every GET on that key. 256 KiB is
# generous for any verdict / summary / reranked-candidate-list we cache.
_MAX_VALUE_BYTES = 256 * 1024


async def cache_get(*, key: str, keyspace: str) -> bytes | None:
    """GET wrapper. Returns raw bytes or None on miss / error.

    Caller is responsible for orjson.loads (or whatever decoder). Keeping
    this layer bytes-only lets cache callers handle their own value-shape
    versioning without round-trips through this helper.
    """
    t0 = time.monotonic()
    try:
        client = await get_redis_client()
        raw = await client.get(key)
        latency_ms = int((time.monotonic() - t0) * 1000)
        increment("cache.dragonfly.get.total", keyspace=keyspace)
        histogram("cache.dragonfly.latency_ms", value=latency_ms, keyspace=keyspace, op="get")
        if raw is None:
            increment("cache.dragonfly.miss.total", keyspace=keyspace)
            return None
        increment("cache.dragonfly.hit.total", keyspace=keyspace)
        return raw
    except Exception as exc:  # noqa: BLE001 — cache must never break the request
        increment("cache.dragonfly.error.total", keyspace=keyspace, op="get")
        _log.warning("cache.dragonfly.get_failed", keyspace=keyspace, error=str(exc))
        return None


async def cache_set(*, key: str, value: bytes, ttl_seconds: int, keyspace: str) -> bool:
    """Atomic `SET key value EX ttl`. Returns True on success, False on error.

    Refuses to store values over `_MAX_VALUE_BYTES` — an oversized payload
    is a producer bug; caching it would penalise every future read.
    """
    if len(value) > _MAX_VALUE_BYTES:
        increment("cache.dragonfly.error.total", keyspace=keyspace, op="set_oversized")
        _log.warning(
            "cache.dragonfly.set_value_too_large",
            keyspace=keyspace, size_bytes=len(value), limit=_MAX_VALUE_BYTES,
        )
        return False
    t0 = time.monotonic()
    try:
        client = await get_redis_client()
        await client.set(key, value, ex=max(1, int(ttl_seconds)))
        latency_ms = int((time.monotonic() - t0) * 1000)
        increment("cache.dragonfly.set.total", keyspace=keyspace)
        histogram("cache.dragonfly.latency_ms", value=latency_ms, keyspace=keyspace, op="set")
        return True
    except Exception as exc:  # noqa: BLE001 — cache write must never break the response
        increment("cache.dragonfly.error.total", keyspace=keyspace, op="set")
        _log.warning("cache.dragonfly.set_failed", keyspace=keyspace, error=str(exc))
        return False


async def cache_delete(*, key: str, keyspace: str) -> bool:
    """DEL wrapper. Returns True if delete issued, False on error."""
    t0 = time.monotonic()
    try:
        client = await get_redis_client()
        await client.delete(key)
        latency_ms = int((time.monotonic() - t0) * 1000)
        increment("cache.dragonfly.delete.total", keyspace=keyspace)
        histogram("cache.dragonfly.latency_ms", value=latency_ms, keyspace=keyspace, op="delete")
        return True
    except Exception as exc:  # noqa: BLE001
        increment("cache.dragonfly.error.total", keyspace=keyspace, op="delete")
        _log.warning("cache.dragonfly.delete_failed", keyspace=keyspace, error=str(exc))
        return False


# ── Single-flight (cache-stampede protection) ───────────────────────


async def acquire_lock(*, lock_key: str, ttl_seconds: int, keyspace: str) -> bool:
    """Try to acquire a short-lived lock via SET NX EX. Returns True iff WE
    acquired it (caller should compute + cache + release). False means
    another worker is already computing — caller should wait + re-read.

    The TTL is a safety net: if the lock-holder crashes mid-compute, the
    lock auto-expires so the system never deadlocks.
    """
    try:
        client = await get_redis_client()
        # redis-py: set(..., nx=True, ex=ttl) → returns True iff key was set.
        acquired = await client.set(
            lock_key, b"1", nx=True, ex=max(1, int(ttl_seconds))
        )
        return bool(acquired)
    except Exception as exc:  # noqa: BLE001 — lock failure must not break the request
        increment("cache.dragonfly.error.total", keyspace=keyspace, op="lock")
        _log.warning("cache.dragonfly.lock_failed", keyspace=keyspace, error=str(exc))
        # Fail-open: behave as if we acquired the lock so the caller computes
        # rather than waiting forever. Worst case = a redundant compute.
        return True


async def release_lock(*, lock_key: str, keyspace: str) -> None:
    """Release a single-flight lock. Best-effort; the TTL is the real
    guarantee, so a failed release just means a slightly longer hold."""
    await cache_delete(key=lock_key, keyspace=keyspace)


async def get_or_single_flight(
    *,
    key: str,
    keyspace: str,
    compute,
    serialize,
    deserialize,
    ttl_seconds: int,
    lock_ttl_seconds: int = 30,
    wait_poll_seconds: float = 0.1,
    wait_max_seconds: float = 25.0,
):
    """Cache-with-stampede-protection.

    Flow:
      1. GET key → hit? return deserialized value.
      2. miss → try to acquire `{key}:lock` (SET NX EX).
      3. lock acquired → call `compute()`, `serialize()` + `cache_set`,
         release lock, return the value.
      4. lock NOT acquired (another worker computing) → poll the key until
         it appears or `wait_max_seconds` elapses; on timeout fall back to
         computing ourselves (correctness over a redundant call).

    `compute` is an async callable returning the raw value (or None on
    error — errors are NOT cached, the lock is released, None returned).
    `serialize` turns the raw value into bytes; `deserialize` the reverse.

    Never raises — cache failures degrade to a direct compute.
    """
    # 1. Fast path.
    raw = await cache_get(key=key, keyspace=keyspace)
    if raw is not None:
        try:
            return deserialize(raw)
        except Exception:  # noqa: BLE001 — corrupt entry → treat as miss
            pass

    lock_key = f"{key}:lock"
    we_hold = await acquire_lock(
        lock_key=lock_key, ttl_seconds=lock_ttl_seconds, keyspace=keyspace
    )

    if we_hold:
        try:
            value = await compute()
            if value is not None:
                try:
                    await cache_set(
                        key=key,
                        value=serialize(value),
                        ttl_seconds=ttl_seconds,
                        keyspace=keyspace,
                    )
                except Exception:  # noqa: BLE001 — write failure non-fatal
                    pass
            return value
        finally:
            await release_lock(lock_key=lock_key, keyspace=keyspace)

    # 4. Another worker is computing — wait for the key to appear.
    waited = 0.0
    while waited < wait_max_seconds:
        await asyncio.sleep(wait_poll_seconds)
        waited += wait_poll_seconds
        raw = await cache_get(key=key, keyspace=keyspace)
        if raw is not None:
            try:
                return deserialize(raw)
            except Exception:  # noqa: BLE001
                break  # corrupt — fall through to self-compute
    # Timed out (or corrupt entry) — compute ourselves rather than hang.
    increment("cache.dragonfly.single_flight_timeout.total", keyspace=keyspace)
    return await compute()


__all__ = [
    "cache_get",
    "cache_set",
    "cache_delete",
    "acquire_lock",
    "release_lock",
    "get_or_single_flight",
]
