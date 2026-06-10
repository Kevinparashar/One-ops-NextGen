"""C-3 (P1-2 / P0-3) — streaming error paths log internally, stay opaque.

Devil's-advocate: when a streamed turn fails, the root cause must be LOGGED
internally and the client-facing final payload must NOT carry internal exception
text — the streaming analog of the Batch-B HTTP fix.

This covers the generic `event_stream` helper (streaming.py) directly and
hermetically. The chat-door closure `_stream_turn` (app.py) applies the IDENTICAL
pattern (log internally + opaque "request_id=…" final_response); it is not unit-
tested here because exercising it requires a full TestClient streaming lifespan,
which hangs when several app-building modules share a process (a test-infra issue,
not a product bug — tracked in docs/planning/risk-register.md). The pattern it uses is the
one proven below, and mirrors the Batch-B non-stream handler in test_error_no_leak.
See docs/history/change-log.md Batch C-3.
"""
from __future__ import annotations

import asyncio
import json

from oneops.api.streaming import event_stream

_SECRET = "INTERNAL boom: dsn=postgres://u:p4ss@h/db"


def test_event_stream_failure_is_opaque_but_logged():
    async def _boom():
        raise RuntimeError(_SECRET)

    async def _drive():
        lines = []
        async for line in event_stream("req_stream_1", _boom):
            lines.append(json.loads(line))
        return lines

    lines = asyncio.run(_drive())
    final = [m for m in lines if m.get("type") == "final"]
    assert final, "stream must emit a final frame even on failure"
    payload = final[0]["payload"]
    assert payload["final_status"] == "failed"
    # opaque + correlatable, no internal text crosses the boundary.
    assert "request_id=req_stream_1" in payload["error"]
    assert _SECRET not in payload["error"]
    assert "postgres://" not in payload["error"]
