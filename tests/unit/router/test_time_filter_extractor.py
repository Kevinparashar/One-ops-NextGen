"""TimeFilterExtractor — adapter behaviour with a stub LLM gateway.

We don't exercise the LLM; we lock the contract between the LLM's JSON
output and the structured `TimeFilter` (or None) we feed downstream.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from typing import Any

from oneops.llm.models import LlmRequest, LlmResponse
from oneops.router.time_filter_extractor import TimeFilterExtractor


class _StubGateway:
    """Minimal LlmGateway shim. Captures the last LlmRequest sent."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.last_request: LlmRequest | None = None

    async def call(self, req: LlmRequest) -> LlmResponse:
        self.last_request = req
        return LlmResponse(
            content=self._content,
            model=req.model,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            latency_ms=1,
        )


class _StubCache:
    """In-memory cache modelled on Dragonfly's get/set API."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ttl: int = 0) -> None:
        self.store[key] = value


def _extract(content: str, message: str, *, cache=None) -> Any:
    gw = _StubGateway(content)
    ex = TimeFilterExtractor(gateway=gw, cache=cache)
    return asyncio.get_event_loop().run_until_complete(
        ex.extract(message=message, tenant_id="T001", user_id="u_demo")
    )


# ── 1. "from last 30 days" → relative_days=30 ───────────────────────────────

def test_relative_window_extracted():
    raw = json.dumps({"time_filter": {"relative_days": 30,
                                       "label": "last 30 days"}})
    tf = _extract(raw, "tickets like INC0001020 from last 30 days")
    assert tf is not None
    assert tf.relative_days == 30
    assert tf.label == "last 30 days"


# ── 2. "since 1 May" → start_date set, no relative_days ─────────────────────

def test_absolute_start_only():
    today = date.today()
    raw = json.dumps({
        "time_filter": {
            "start_date": f"{today.year}-05-01",
            "label": "since 1 May",
        }
    })
    tf = _extract(raw, "similar incidents since 1 May")
    assert tf is not None
    assert tf.start_date is not None
    assert tf.start_date.month == 5
    assert tf.relative_days is None


# ── 3. "show similar tickets" → None ────────────────────────────────────────

def test_no_time_reference_returns_none():
    tf = _extract(json.dumps({"time_filter": None}), "show similar tickets")
    assert tf is None


# ── 4. "since the outage" → None + unresolved_phrase hint ──────────────────

def test_unresolved_phrase_returns_none():
    """The LLM signals it couldn't resolve — we degrade to no filter
    rather than guess. The orchestrator escalates via clarification."""
    raw = json.dumps({"time_filter": None,
                      "unresolved_phrase": "since the outage"})
    tf = _extract(raw, "similar tickets since the outage")
    assert tf is None


# ── 5. Future date rolls back a year (the "since November in January" case) ─

def test_future_anchor_rolled_back():
    """Spec: if the LLM resolved to a date >7 days in the future, validator
    rolls it back a year."""
    far_future = date.today() + timedelta(days=200)
    raw = json.dumps({
        "time_filter": {
            "start_date": far_future.isoformat(),
            "label": "since November",
        }
    })
    tf = _extract(raw, "similar tickets since November")
    assert tf is not None
    assert tf.start_date.year == far_future.year - 1


# ── 6. Malformed JSON degrades to None (no silent guess) ───────────────────

def test_malformed_json_returns_none():
    tf = _extract("definitely not json {{{", "anything")
    assert tf is None


def test_empty_response_returns_none():
    tf = _extract("", "anything")
    assert tf is None


def test_unexpected_shape_returns_none():
    tf = _extract(json.dumps([1, 2, 3]), "anything")
    assert tf is None


# ── 7. Schema violation (LLM hallucinated mutex) degrades to None ──────────

def test_schema_violation_returns_none():
    """LLM emitted both relative_days AND a date — validator rejects, we
    return None instead of half-applying."""
    raw = json.dumps({
        "time_filter": {
            "relative_days": 30,
            "start_date": "2026-01-01",
            "label": "last 30 days",
        }
    })
    tf = _extract(raw, "anything")
    assert tf is None


# ── 8. Empty message short-circuits (no LLM call) ──────────────────────────

def test_empty_message_skips_llm():
    gw = _StubGateway("never called")
    ex = TimeFilterExtractor(gateway=gw)
    asyncio.get_event_loop().run_until_complete(
        ex.extract(message="", tenant_id="T001"))
    assert gw.last_request is None
    asyncio.get_event_loop().run_until_complete(
        ex.extract(message="   ", tenant_id="T001"))
    assert gw.last_request is None


# ── 9. Cache: null + present round-trip ────────────────────────────────────

def test_null_filter_is_cached():
    cache = _StubCache()
    _extract(json.dumps({"time_filter": None}), "no time here", cache=cache)
    assert any(v == "NULL" for v in cache.store.values())


def test_present_filter_is_cached_and_reused():
    cache = _StubCache()
    raw = json.dumps({"time_filter": {"relative_days": 7, "label": "last week"}})
    tf1 = _extract(raw, "tickets from last week", cache=cache)
    assert tf1 is not None

    # Second call with a stub gateway that returns nothing — should still
    # produce the same filter from cache.
    gw = _StubGateway("")
    ex = TimeFilterExtractor(gateway=gw, cache=cache)
    tf2 = asyncio.get_event_loop().run_until_complete(
        ex.extract(message="tickets from last week", tenant_id="T001"))
    assert tf2 is not None
    assert tf2.relative_days == 7
    assert gw.last_request is None  # cache hit — no LLM call


def test_cache_invalidates_across_days():
    """Cache key includes today's date — same phrasing on a new day re-asks
    the LLM (so "last week" anchors to today, not last week's date)."""
    from oneops.router.time_filter_extractor import _cache_key
    a = _cache_key("last week", "2026-01-01")
    b = _cache_key("last week", "2026-01-02")
    assert a != b


# ── 10. LLM gateway error degrades to None ────────────────────────────────

def test_gateway_error_returns_none():
    class _Boom:
        async def call(self, _req):
            raise RuntimeError("LLM is on fire")

    ex = TimeFilterExtractor(gateway=_Boom())
    tf = asyncio.get_event_loop().run_until_complete(
        ex.extract(message="tickets from last week", tenant_id="T001"))
    assert tf is None  # never blows up the caller
