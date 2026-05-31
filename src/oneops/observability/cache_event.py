"""Cache observability — events on the current span, plus metrics.

Cache GET/SET operations are sub-millisecond. Wrapping each in its own
span would pollute the trace tree with hundreds of tiny entries. Instead
we attach events to whatever span is currently active and emit metrics.

Never raises.

Standard event names:
  cache.get      attrs: cache.name, cache.hit, cache.key_hash, cache.latency_ms
  cache.set      attrs: cache.name, cache.key_hash, cache.payload_size, cache.ttl_seconds
  cache.delete   attrs: cache.name, cache.key_hash

Standard metrics:
  ai.cache.hits.total      {cache_name}
  ai.cache.misses.total    {cache_name}
  ai.cache.writes.total    {cache_name}
  ai.cache.deletes.total   {cache_name}
  ai.cache.stale_reads.total {cache_name}
"""
from __future__ import annotations

import contextlib
from typing import Any

from opentelemetry import trace

from oneops.observability.metrics import histogram, increment


def record_cache_get(
    *,
    cache_name: str,
    hit: bool,
    key_hash: str = "",
    latency_ms: int | None = None,
    stale: bool = False,
    **extra: Any,
) -> None:
    """Emit cache.get event on current span + counter."""
    try:
        sp = trace.get_current_span()
        attrs: dict[str, Any] = {
            "cache.name": cache_name,
            "cache.hit": hit,
            "cache.stale": stale,
        }
        if key_hash:
            attrs["cache.key_hash"] = key_hash
        if latency_ms is not None:
            attrs["cache.latency_ms"] = latency_ms
        for k, v in extra.items():
            if v is None:
                continue
            attrs[f"cache.{k}"] = v
        sp.add_event("cache.get", attributes=attrs)
    except Exception:
        pass

    try:
        if hit and stale:
            increment("ai.cache.stale_reads.total", cache_name=cache_name)
        elif hit:
            increment("ai.cache.hits.total", cache_name=cache_name)
        else:
            increment("ai.cache.misses.total", cache_name=cache_name)
        if latency_ms is not None:
            histogram("ai.cache.latency_ms", value=latency_ms, cache_name=cache_name, operation="get")
    except Exception:
        pass


def record_cache_set(
    *,
    cache_name: str,
    key_hash: str = "",
    payload_size: int | None = None,
    ttl_seconds: int | None = None,
    latency_ms: int | None = None,
    **extra: Any,
) -> None:
    """Emit cache.set event on current span + counter."""
    try:
        sp = trace.get_current_span()
        attrs: dict[str, Any] = {"cache.name": cache_name}
        if key_hash:
            attrs["cache.key_hash"] = key_hash
        if payload_size is not None:
            attrs["cache.payload_size"] = payload_size
        if ttl_seconds is not None:
            attrs["cache.ttl_seconds"] = ttl_seconds
        if latency_ms is not None:
            attrs["cache.latency_ms"] = latency_ms
        for k, v in extra.items():
            if v is None:
                continue
            attrs[f"cache.{k}"] = v
        sp.add_event("cache.set", attributes=attrs)
    except Exception:
        pass

    try:
        increment("ai.cache.writes.total", cache_name=cache_name)
        if latency_ms is not None:
            histogram("ai.cache.latency_ms", value=latency_ms, cache_name=cache_name, operation="set")
    except Exception:
        pass


def record_cache_delete(*, cache_name: str, key_hash: str = "") -> None:
    """Emit cache.delete event + counter."""
    try:
        sp = trace.get_current_span()
        attrs = {"cache.name": cache_name}
        if key_hash:
            attrs["cache.key_hash"] = key_hash
        sp.add_event("cache.delete", attributes=attrs)
    except Exception:
        pass

    with contextlib.suppress(Exception):
        increment("ai.cache.deletes.total", cache_name=cache_name)


__all__ = ["record_cache_get", "record_cache_set", "record_cache_delete"]
