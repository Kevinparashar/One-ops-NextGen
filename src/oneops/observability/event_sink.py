"""Live turn-event side-channel — streams per-step agent/tool activity to a
UI without polluting the (checkpointed, JSON-only) executor state.

A streaming endpoint calls `open_sink(request_id)` to register a queue; the
step runner calls `publish(request_id, event)` as it starts/finishes each
tool; the endpoint drains the queue and forwards events to the browser, then
`close_sink(request_id)`.

Process-local and **best-effort**: if no sink is open for a request (every
non-streaming turn), `publish` is a no-op, so the executor path is unchanged
and never blocked or broken by UI streaming.
"""
from __future__ import annotations

import asyncio
from typing import Any

# request_id → queue of event dicts. Only populated while a streaming
# response for that request is in flight.
_SINKS: dict[str, asyncio.Queue[dict[str, Any]]] = {}


def open_sink(request_id: str) -> asyncio.Queue[dict[str, Any]]:
    """Register and return a fresh event queue for `request_id`."""
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    _SINKS[request_id] = q
    return q


def close_sink(request_id: str) -> None:
    """Drop the sink. Idempotent."""
    _SINKS.pop(request_id, None)


def publish(request_id: str, event: dict[str, Any]) -> None:
    """Best-effort, non-blocking publish. No open sink → no-op.

    Never raises — a UI-streaming hiccup must not affect turn execution.
    """
    if not request_id:
        return
    q = _SINKS.get(request_id)
    if q is None:
        return
    try:
        q.put_nowait(event)
    except Exception:                       # noqa: BLE001 — UI side-channel
        pass


__all__ = ["open_sink", "close_sink", "publish"]
