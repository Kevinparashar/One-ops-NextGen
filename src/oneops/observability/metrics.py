"""Metric helpers — counters + histograms.

Instruments are cached per-name and reused across calls (cheap). When the
MeterProvider is the no-op default (OTEL exporter unset), every emit is a
sub-microsecond no-op. Never raises into business code.

Standard metrics emitted by the codebase:

  ai.llm.tokens.input.total   counter   {model, operation, provider}
  ai.llm.tokens.output.total  counter   {model, operation, provider}
  ai.llm.tokens.total         counter   {model, operation, provider}
  ai.llm.latency_ms           histogram {model, operation, provider}
  ai.llm.errors.total         counter   {model, operation, error_type}
  ai.cache.hits.total         counter   {cache_name}
  ai.cache.misses.total       counter   {cache_name}
  ai.cache.writes.total       counter   {cache_name}
  ai.cache.stale_reads.total  counter   {cache_name}
  ai.cache.latency_ms         histogram {cache_name, operation}
"""
from __future__ import annotations

import threading
from typing import Any

from opentelemetry import metrics as otel_metrics

_lock = threading.Lock()
_counters: dict[str, Any] = {}
_histograms: dict[str, Any] = {}


def _meter() -> otel_metrics.Meter:
    return otel_metrics.get_meter("oneops")


def _get_counter(name: str) -> Any:
    if name in _counters:
        return _counters[name]
    with _lock:
        if name in _counters:
            return _counters[name]
        try:
            c = _meter().create_counter(name)
        except Exception:
            c = None
        _counters[name] = c
        return c


def _get_histogram(name: str) -> Any:
    if name in _histograms:
        return _histograms[name]
    with _lock:
        if name in _histograms:
            return _histograms[name]
        try:
            h = _meter().create_histogram(name)
        except Exception:
            h = None
        _histograms[name] = h
        return h


def increment(name: str, value: int | float = 1, **labels: Any) -> None:
    """Add `value` to a counter. None labels are filtered. Never raises."""
    c = _get_counter(name)
    if c is None:
        return
    try:
        clean = {k: v for k, v in labels.items() if v is not None}
        c.add(value, attributes=clean if clean else None)
    except Exception:
        pass


def histogram(name: str, value: int | float, **labels: Any) -> None:
    """Record one observation on a histogram. Never raises."""
    h = _get_histogram(name)
    if h is None:
        return
    try:
        clean = {k: v for k, v in labels.items() if v is not None}
        h.record(value, attributes=clean if clean else None)
    except Exception:
        pass


def _reset_for_tests() -> None:
    """Clear instrument cache (only for unit tests that swap meter providers)."""
    with _lock:
        _counters.clear()
        _histograms.clear()


__all__ = ["increment", "histogram"]
