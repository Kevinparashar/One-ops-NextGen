"""Trace-context propagation across service hops (P9).

Within one process, OpenTelemetry propagates the active span context through
`contextvars` automatically — child spans nest under their parent. Across a
**service boundary** (a NATS message, ADR-0005) the context must be carried
explicitly: the sender injects the W3C `traceparent` into the message headers,
the receiver extracts it and continues the same trace.

`inject_trace_headers` / `extract_trace_context` are that pair. They wrap
OpenTelemetry's standard `TraceContextTextMapPropagator`, so the wire format is
W3C Trace Context — interoperable with any OTel-instrumented service.

In single-process (FaaS) mode there is no NATS hop and these are unused; in
microservice mode every `nats_client.publish` injects and every consumer
extracts, so one user request is one unbroken trace across services.
"""
from __future__ import annotations

from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagators.textmap import TextMapPropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_PROPAGATOR: TextMapPropagator = TraceContextTextMapPropagator()


def inject_trace_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    """Return `headers` with the active trace context injected as W3C
    `traceparent` (+ `tracestate`). Call this before publishing a NATS message
    so the receiver can continue the trace."""
    carrier: dict[str, str] = dict(headers or {})
    _PROPAGATOR.inject(carrier)
    return carrier


def extract_trace_context(headers: dict[str, str] | None) -> otel_context.Context:
    """Extract a trace context from inbound message headers. Pass the result
    as the `context=` of the receiver's root span so the remote work attaches
    to the originating trace.

    Returns an empty context (a fresh trace will start) when no `traceparent`
    is present — never raises on a missing/garbled header."""
    return _PROPAGATOR.extract(headers or {})


def current_traceparent() -> str:
    """The active span's W3C `traceparent` string, or '' when not in a span.
    Handy for logging and for tests that assert propagation."""
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier.get("traceparent", "")


def start_consumer_span(tracer: trace.Tracer, name: str,
                        headers: dict[str, str] | None, **attributes: Any):
    """Start a span that continues the trace carried in inbound `headers`.
    The NATS consumer's entry point uses this so a remote hop is one trace."""
    ctx = extract_trace_context(headers)
    return tracer.start_as_current_span(name, context=ctx, attributes=attributes)


__all__ = [
    "inject_trace_headers",
    "extract_trace_context",
    "current_traceparent",
    "start_consumer_span",
]
