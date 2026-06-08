"""Shared NDJSON live-activity streaming helper.

Lets the bespoke UC endpoints (UC-2/5/8 — which run their tools OUTSIDE the
executor and keep their own result UIs) emit the SAME live "agent + tool"
activity stream as the chat/fast-path doors, per docs/architecture/CONVENTIONS.md
"Live activity stream".

`event_stream(request_id, run_final)` opens an event sink keyed by
`request_id`, runs `run_final()` (a coroutine that does the real work,
publishing `tool_start`/`tool_done` via `event_sink.publish` and returning
the final response payload), and yields NDJSON lines:

    {"type":"turn_start","request_id":...}
    {"type":"tool_start","agent_id":...,"tool_id":...,"action":...}
    {"type":"tool_done","agent_id":...,"tool_id":...,"status":...,"latency_ms":...}
    {"type":"final","payload": <the endpoint's normal response dict>}

The `final` payload is whatever `run_final` returns (each UC's existing
response shape) so the frontend keeps rendering its own result view.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from oneops.observability import get_logger
from oneops.observability.event_sink import close_sink, open_sink, publish

_log = get_logger("oneops.api.streaming")


def _line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, default=str) + "\n"


async def event_stream(request_id: str, run_final: Callable[[], Awaitable[dict[str, Any]]]):
    """Yield NDJSON live events while `run_final()` executes, then the final."""
    q = open_sink(request_id)
    task = asyncio.ensure_future(run_final())
    try:
        yield _line({"type": "turn_start", "request_id": request_id})
        while True:
            getter = asyncio.ensure_future(q.get())
            done, _pending = await asyncio.wait(
                {getter, task}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield _line(getter.result())
                continue
            getter.cancel()
            while not q.empty():
                yield _line(q.get_nowait())
            try:
                payload = task.result()
            except Exception as exc:                       # noqa: BLE001
                # Root cause is logged internally; the client payload stays
                # opaque (+ request_id for correlation) — no internal exception
                # text crosses the streaming boundary (P0-3 / Batch C-3).
                _log.warning("oneops.api.streaming.run_final_failed",
                             request_id=request_id, error=str(exc)[:200])
                payload = {"final_status": "failed",
                           "error": f"stream failed (request_id={request_id})"}
            yield _line({"type": "final", "payload": payload})
            break
    finally:
        close_sink(request_id)
        if not task.done():
            task.cancel()


async def publish_tool(
    request_id: str, *, agent_id: str, tool_id: str, action: str,
    run: Callable[[], Awaitable[Any]],
) -> Any:
    """Publish tool_start, run `run()`, publish tool_done (status+latency),
    return its result. The bespoke endpoints wrap their single core tool call
    in this so the live panel shows the real agent + tool + timing."""
    publish(request_id, {"type": "tool_start", "agent_id": agent_id,
                         "tool_id": tool_id, "action": action})
    t0 = time.monotonic()
    status = "success"
    try:
        return await run()
    except Exception:                                      # noqa: BLE001
        status = "failed"
        raise
    finally:
        publish(request_id, {"type": "tool_done", "agent_id": agent_id,
                             "tool_id": tool_id, "status": status,
                             "latency_ms": int((time.monotonic() - t0) * 1000)})


__all__ = ["event_stream", "publish_tool"]
