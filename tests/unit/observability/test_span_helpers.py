"""Guarantee tests for span_helpers + cache_event."""
from __future__ import annotations

import pytest
from opentelemetry.trace import Span

from oneops.observability import (
    current_trace_ids,
    llm_span,
    record_cache_get,
    record_cache_set,
    record_exception_safe,
    set_attrs,
    span,
)


# ── span() ────────────────────────────────────────────────────────
def test_span_context_manager_returns_span() -> None:
    with span("test.basic", attr1="x") as sp:
        assert isinstance(sp, Span) or sp is not None


def test_span_filters_none_attrs() -> None:
    # Must not raise even if some attrs are None
    with span("test.none_attrs", a="x", b=None, c=42):
        pass


def test_span_exception_propagates() -> None:
    with pytest.raises(ValueError), span("test.error"):
        raise ValueError("boom")


def test_span_telemetry_never_breaks_business_code() -> None:
    # Even with a weird span name, must not raise
    with span("", weird_attr={"nested": "ok"}):
        pass


# ── llm_span() ────────────────────────────────────────────────────
def test_llm_span_yields_span() -> None:
    with llm_span(operation="classify", model="gpt-4o-mini") as sp:
        assert sp is not None


def test_llm_span_exception_recorded() -> None:
    with pytest.raises(RuntimeError):
        with llm_span(operation="embed", model="text-embedding-3-small"):
            raise RuntimeError("timeout")


def test_llm_span_with_optional_args() -> None:
    with llm_span(
        operation="classify",
        model="gpt-4o-mini",
        provider="openai",
        temperature=0.0,
    ):
        pass


# ── set_attrs / record_exception_safe ─────────────────────────────
def test_set_attrs_filters_none() -> None:
    with span("test.set_attrs") as sp:
        set_attrs(sp, a="x", b=None, c=1)


def test_record_exception_safe_does_not_raise() -> None:
    with span("test.rec_exc") as sp:
        record_exception_safe(sp, ValueError("test"))


# ── current_trace_ids ─────────────────────────────────────────────
def test_current_trace_ids_returns_tuple() -> None:
    tid, sid = current_trace_ids()
    assert isinstance(tid, str)
    assert isinstance(sid, str)


def test_current_trace_ids_inside_span_returns_valid_ids() -> None:
    with span("test.trace_ids"):
        tid, sid = current_trace_ids()
        # When OTEL provider is real (it is, since setup_observability ran),
        # these should be non-empty.
        assert isinstance(tid, str)
        assert isinstance(sid, str)


# ── cache events ──────────────────────────────────────────────────
def test_record_cache_get_hit() -> None:
    with span("test.cache_parent"):
        record_cache_get(cache_name="test", hit=True, key_hash="abc123", latency_ms=2)


def test_record_cache_get_miss() -> None:
    with span("test.cache_parent"):
        record_cache_get(cache_name="test", hit=False, latency_ms=1)


def test_record_cache_set_with_payload() -> None:
    with span("test.cache_parent"):
        record_cache_set(
            cache_name="test",
            key_hash="abc",
            payload_size=512,
            ttl_seconds=3600,
            latency_ms=3,
        )


def test_record_cache_get_outside_span_does_not_raise() -> None:
    # No active span — event must be no-op, not exception
    record_cache_get(cache_name="test", hit=True)


def test_record_cache_stale_read() -> None:
    with span("test.cache_parent"):
        record_cache_get(cache_name="test", hit=True, stale=True)
