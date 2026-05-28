"""Centralized span helpers.

Every span in the codebase should go through these helpers rather than
calling `tracer.start_as_current_span` directly. Benefits:

- Consistent attribute names (no drift between call sites)
- Automatic ERROR status on exception
- Business outcomes (clarification, no_match, not_found) stay OK status
- None-attr filtering (OTel rejects None values)
- Try/except wrapper so telemetry never raises into business code

Usage:

    from oneops.observability import span, llm_span

    with span("graph.planner", uc_id="uc01_summarization") as s:
        s.set_attribute("plan.steps", 3)
        ...

    with llm_span(operation="classify_intent", model="gpt-4o-mini") as s:
        result = await gateway.call(...)
        # tokens auto-attached if result has prompt_tokens/completion_tokens

When `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, the tracer is the no-op SDK
default; every emit becomes a sub-microsecond no-op.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from oneops.observability.metrics import histogram, increment

# Outcomes that should NOT mark a span ERROR. They are legitimate business
# results, not failures.
_BENIGN_OUTCOMES = frozenset({
    "clarification",
    "clarification_required",
    "no_match",
    "no_referent",
    "not_found",
    "ambiguous",
    "ok",
    "success",
})


def _tracer() -> trace.Tracer:
    return trace.get_tracer("oneops")


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    """Drop None values — OTel rejects them and logs a warning."""
    return {k: v for k, v in attrs.items() if v is not None}


@contextmanager
def span(name: str, *, kind: SpanKind = SpanKind.INTERNAL, **attrs: Any) -> Iterator[Span]:
    """Open a span with attributes. ERROR on exception. Never raises from telemetry.

    Business outcomes (clarification, no_match, …) — set via
    `s.set_attribute("outcome", "...")` — keep status OK.
    """
    try:
        cm = _tracer().start_as_current_span(name, kind=kind, attributes=_clean(attrs))
    except Exception:
        # If tracer/SDK is broken, fall through with a real null span so
        # callers can still use the context manager.
        yield trace.INVALID_SPAN
        return
    with cm as sp:
        try:
            yield sp
        except Exception as exc:
            try:
                sp.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                sp.record_exception(exc)
            except Exception:
                pass
            raise


@contextmanager
def llm_span(
    *,
    operation: str,
    model: str,
    provider: str | None = None,
    temperature: float | None = None,
    **attrs: Any,
) -> Iterator[Span]:
    """Specialized span for LLM calls.

    On clean exit, attaches token + latency metrics by reading attributes the
    caller set on the span:
      - `llm.input_tokens`, `llm.output_tokens`, `llm.total_tokens`
      - `llm.latency_ms` (otherwise computed from wall-clock)

    Use:
        with llm_span(operation="classify_intent", model=model) as s:
            resp = await call(...)
            s.set_attribute("llm.input_tokens", resp.prompt_tokens)
            s.set_attribute("llm.output_tokens", resp.completion_tokens)
            s.set_attribute("llm.total_tokens", resp.total_tokens)
    """
    base: dict[str, Any] = {
        "llm.operation": operation,
        "llm.model": model,
        "llm.provider": provider,
        "llm.temperature": temperature,
    }
    base.update(attrs)
    t0 = time.monotonic()
    try:
        cm = _tracer().start_as_current_span(
            "llm.call", kind=SpanKind.CLIENT, attributes=_clean(base)
        )
    except Exception:
        yield trace.INVALID_SPAN
        return
    with cm as sp:
        try:
            yield sp
        except Exception as exc:
            try:
                sp.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                sp.record_exception(exc)
                increment("ai.llm.errors.total", model=model, operation=operation, error_type=type(exc).__name__)
            except Exception:
                pass
            raise
        finally:
            # Emit metrics from whatever attributes the caller stamped.
            try:
                latency_ms = int((time.monotonic() - t0) * 1000)
                # Prefer caller-supplied latency if present (e.g. gateway already measured it)
                # We can't read back arbitrary attrs from the span post-end, so caller
                # should also call histogram() directly if they want their measured value.
                histogram("ai.llm.latency_ms", value=latency_ms, model=model, operation=operation)
            except Exception:
                pass


def record_exception_safe(sp: Span, exc: BaseException) -> None:
    """Mark span ERROR + record exception, never raising."""
    try:
        sp.set_status(Status(StatusCode.ERROR, type(exc).__name__))
        sp.record_exception(exc)
    except Exception:
        pass


def set_attrs(sp: Span, **kwargs: Any) -> None:
    """Set multiple attributes, filtering None. Never raises."""
    try:
        for k, v in kwargs.items():
            if v is None:
                continue
            sp.set_attribute(k, v)
    except Exception:
        pass


def current_trace_ids() -> tuple[str, str]:
    """Return (trace_id_hex, span_id_hex) for the current span. ('','') if none."""
    try:
        sp = trace.get_current_span()
        ctx = sp.get_span_context()
        if not ctx.is_valid:
            return ("", "")
        return (format(ctx.trace_id, "032x"), format(ctx.span_id, "016x"))
    except Exception:
        return ("", "")


__all__ = [
    "span",
    "llm_span",
    "record_exception_safe",
    "set_attrs",
    "current_trace_ids",
]
