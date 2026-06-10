"""field_embedder._embed_views — batched embedding (latency, RCA 2026-06-09).

The field-read matcher embeds several message "views". This used to be N
sequential gateway round-trips (the 3-embed / ~1.9s anomaly); it is now ONE
batched `gateway.embed([...])`. These tests assert the batch contract: one
call for N uncached views, per-view cache preserved, order preserved, and
fail-open on a gateway error.
"""
from __future__ import annotations

import pytest

from oneops.use_cases.uc01_summarization import field_embedder as fe

pytestmark = pytest.mark.asyncio


class _RecordingGateway:
    """Records each embed() call's text batch; returns a deterministic vector
    per text so order can be verified."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts, *, model, tenant_id, user_id="", dimensions=None):
        self.calls.append(list(texts))
        # distinct vector per text (first dim = len, so we can tell them apart)
        return [[float(len(t)), 1.0, 2.0] for t in texts]


def _clear_cache():
    fe._msg_cache.clear()


async def test_n_views_embed_in_a_single_call():
    _clear_cache()
    gw = _RecordingGateway()
    views = ["who is the owner", "owner of ticket", "assigned to"]
    out = await fe._embed_views(gw, "m", None, views, "T001", "u")
    # ONE batched call carrying all three views — not three calls
    assert len(gw.calls) == 1
    assert gw.calls[0] == views
    # one vector per view, in order
    assert len(out) == 3
    assert out[0][0] == float(len(views[0]))      # order preserved


async def test_cached_views_skip_the_gateway():
    _clear_cache()
    gw = _RecordingGateway()
    views = ["who is the owner", "assigned to"]
    await fe._embed_views(gw, "m", None, views, "T001", "u")   # warms cache
    await fe._embed_views(gw, "m", None, views, "T001", "u")   # all cached
    assert len(gw.calls) == 1                       # second call hit the cache


async def test_only_uncached_views_are_batched():
    _clear_cache()
    gw = _RecordingGateway()
    await fe._embed_views(gw, "m", None, ["alpha"], "T001", "u")   # cache 'alpha'
    await fe._embed_views(gw, "m", None, ["alpha", "beta"], "T001", "u")
    # second call batches ONLY the uncached 'beta'
    assert gw.calls[1] == ["beta"]


async def test_empty_views_dropped_no_call():
    _clear_cache()
    gw = _RecordingGateway()
    # genuinely empty views are dropped (same as the old per-view path, which
    # returned None for falsy text); whitespace views are normalised upstream.
    out = await fe._embed_views(gw, "m", None, ["", ""], "T001", "u")
    assert out == []
    assert gw.calls == []                           # nothing to embed


async def test_gateway_error_fails_open_to_empty():
    _clear_cache()

    class _BoomGateway:
        async def embed(self, *a, **k):
            raise RuntimeError("gateway down")

    out = await fe._embed_views(_BoomGateway(), "m", None, ["x", "y"], "T", "u")
    assert out == []                                # fail-open, never raises
