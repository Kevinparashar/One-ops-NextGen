"""Idempotency store — re-delivery must not repeat a side effect.

NATS is at-least-once (ADR-0005); a tool call can be delivered twice. For an
action tool that is a real hazard. The idempotency store keys a *completed*
tool result by an idempotency key (minted at ingress, carried on the codec
envelope): a repeated key returns the stored result and the tool does not run
again.

Only **successful** results are stored. A failed attempt is not cached — a
transient failure must stay retryable. (A partially-applied side effect on a
failed attempt is the hard case; the tool handler owns its own
internal idempotency for that — this store guards the common whole-or-nothing
case.)

`InMemoryIdempotencyStore` is the P7 implementation; `DragonflyIdempotencyStore`
shares keys across worker processes (env-gated — not exercised without infra).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Protocol

from oneops.toolrunner.models import ToolResult, ToolStatus

# Idempotency keys expire — a key is only relevant for the redelivery window.
DEFAULT_IDEMPOTENCY_TTL_SECONDS = 86_400


class IdempotencyStore(Protocol):
    async def get(self, key: str) -> ToolResult | None:
        """Return the stored result for `key`, or None."""
        ...

    async def put(self, key: str, result: ToolResult, *, ttl_seconds: int) -> None:
        """Store a successful tool result under `key`."""
        ...


class InMemoryIdempotencyStore:
    """Thread-safe in-process idempotency store with lazy TTL expiry."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._store: dict[str, tuple[ToolResult, float]] = {}

    async def get(self, key: str) -> ToolResult | None:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            result, expires_at = entry
            if now >= expires_at:
                del self._store[key]
                return None
            # Flag the replay so callers can see the tool did not re-run.
            return ToolResult(
                tool_id=result.tool_id, status=result.status,
                output=result.output, error=result.error,
                latency_ms=result.latency_ms, from_idempotency_cache=True)

    async def put(
        self, key: str, result: ToolResult, *,
        ttl_seconds: int = DEFAULT_IDEMPOTENCY_TTL_SECONDS,
    ) -> None:
        if result.status is not ToolStatus.SUCCESS:
            return                          # only completed work is cached
        with self._lock:
            self._store[key] = (result, time.monotonic() + ttl_seconds)


class DragonflyIdempotencyStore:
    """Cross-process idempotency store backed by Dragonfly. Env-gated."""

    def __init__(self, client: Any) -> None:
        self._redis = client

    async def get(self, key: str) -> ToolResult | None:
        raw = await self._redis.get(f"idem:{key}")
        if raw is None:
            return None
        doc = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        return ToolResult(
            tool_id=doc["tool_id"], status=ToolStatus(doc["status"]),
            output=doc.get("output"), error=doc.get("error"),
            latency_ms=doc.get("latency_ms", 0), from_idempotency_cache=True)

    async def put(
        self, key: str, result: ToolResult, *,
        ttl_seconds: int = DEFAULT_IDEMPOTENCY_TTL_SECONDS,
    ) -> None:
        if result.status is not ToolStatus.SUCCESS:
            return
        # Only JSON-serialisable output is persisted; a VariableRef output is
        # reduced to its preview for the cross-process cache.
        output = result.output
        if hasattr(output, "is_variable_ref"):
            output = {"variable_ref": output.name, "preview": output.preview}
        doc = json.dumps({
            "tool_id": result.tool_id, "status": result.status.value,
            "output": output, "error": result.error,
            "latency_ms": result.latency_ms})
        await self._redis.set(f"idem:{key}", doc, ex=ttl_seconds)


__all__ = [
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "DragonflyIdempotencyStore",
    "DEFAULT_IDEMPOTENCY_TTL_SECONDS",
]
