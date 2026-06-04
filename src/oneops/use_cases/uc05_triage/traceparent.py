"""W3C traceparent extraction + injection — used by NATS hops in Phase 4.

The traceparent header carries the current trace context across process
boundaries so the trace tree in Tempo stays unified. Format (W3C TraceContext):

    00-<32-hex-trace-id>-<16-hex-span-id>-<2-hex-flags>

Plain-string operations, no regex (production-hygiene rule, 2026-05-29).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_VERSION = "00"
_FLAGS_SAMPLED = "01"
_FLAGS_NOT_SAMPLED = "00"


def current_traceparent() -> str | None:
    """Return the W3C traceparent for the currently-active OTel span.

    Returns None when there is no active span or the SDK is the no-op default.
    Callers SHOULD attach this to outgoing NATS message headers under the key
    'traceparent'.
    """
    try:
        from opentelemetry import trace
        ctx = trace.get_current_span().get_span_context()
        if not ctx or not ctx.is_valid:
            return None
        trace_id = format(ctx.trace_id, "032x")
        span_id = format(ctx.span_id, "016x")
        flags = _FLAGS_SAMPLED if ctx.trace_flags & 0x01 else _FLAGS_NOT_SAMPLED
        return f"{_VERSION}-{trace_id}-{span_id}-{flags}"
    except Exception:
        return None


def parse_traceparent(value: str | None) -> tuple[str, str, int] | None:
    """Parse a W3C traceparent. Returns (trace_id, span_id, flags) or None.

    Plain string ops — no regex.
    Validates: 4 hyphen-delimited fields, version='00', trace_id 32 lowercase
    hex chars (not all-zero), span_id 16 lowercase hex chars (not all-zero),
    flags 2 hex chars.
    """
    if not value or not isinstance(value, str):
        return None
    parts = value.strip().split("-")
    if len(parts) != 4:
        return None
    version, trace_id, span_id, flags = parts
    if version != _VERSION:
        return None
    if len(trace_id) != 32 or not _is_lower_hex(trace_id) or trace_id == "0" * 32:
        return None
    if len(span_id) != 16 or not _is_lower_hex(span_id) or span_id == "0" * 16:
        return None
    if len(flags) != 2 or not _is_lower_hex(flags):
        return None
    try:
        flags_int = int(flags, 16)
    except ValueError:
        return None
    return trace_id, span_id, flags_int


def extract_from_headers(headers: Mapping[str, Any] | None) -> str | None:
    """Pull the 'traceparent' header from a NATS headers dict (or None)."""
    if not headers:
        return None
    v = headers.get("traceparent")
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    return str(v) if v else None


def _is_lower_hex(s: str) -> bool:
    """True if every char is 0-9 or a-f. No regex."""
    return all("0" <= ch <= "9" or "a" <= ch <= "f" for ch in s)


__all__ = [
    "current_traceparent", "parse_traceparent", "extract_from_headers",
]
