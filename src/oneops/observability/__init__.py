"""OneOps observability: structured logging + OpenTelemetry tracing.

Single init point for the whole service. Call setup_observability() ONCE at
process startup. Idempotent — repeated calls are no-ops.

Concurrency-safe: the OTEL SDK uses thread-local + async-context-local trace
context; no shared mutable state in our code. structlog uses contextvars for
per-request context binding.
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
from typing import Any

import structlog
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import (
    ParentBased,
    TraceIdRatioBased,
)

from oneops.config import get_settings
from oneops.observability.cache_event import (
    record_cache_delete,
    record_cache_get,
    record_cache_set,
)
from oneops.observability.langfuse_content import (
    langfuse_capture_content_enabled,
    redact_for_span,
    set_langfuse_generation,
    set_langfuse_io,
    set_langfuse_trace,
)
from oneops.observability.metrics import histogram, increment
from oneops.observability.propagation import (
    current_traceparent,
    extract_trace_context,
    inject_trace_headers,
    start_consumer_span,
)
from oneops.observability.safe_attrs import (
    capture_text_enabled,
    safe_hash_text,
    safe_json_attr,
    safe_list_attr,
    safe_text_len,
    set_safe_text_attrs,
)
from oneops.observability.span_helpers import (
    current_trace_ids,
    llm_span,
    record_exception_safe,
    set_attrs,
    span,
)

_init_lock = threading.Lock()
_initialized = False


def _otlp_endpoint_reachable(endpoint: str, timeout_s: float = 0.5) -> bool:
    """Fast TCP probe — True iff host:port accepts a connection within timeout."""
    import socket
    from urllib.parse import urlparse
    try:
        parsed = urlparse(endpoint)
        host = parsed.hostname or "localhost"
        port = parsed.port or (4318 if parsed.scheme in ("http", "https") else 4317)
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except Exception:
        return False


def _otlp_path_accepts(endpoint: str, path: str, timeout_s: float = 0.5) -> bool:
    """HTTP probe — True iff POSTing to `endpoint/path` returns anything OTHER
    than 404. We intentionally accept 4xx/5xx other than 404 because the OTLP
    receiver may reject an empty POST body but is still functional.

    Purpose: distinguish a tracing-only backend (e.g. grafana/tempo accepts
    /v1/traces but 404s /v1/metrics) from a full OTLP receiver (otel-collector
    accepts both). Without this we'd attach a metric exporter that 404s every
    60 seconds, polluting logs without producing any observability value.
    """
    import urllib.error
    import urllib.request
    url = f"{endpoint.rstrip('/')}/{path.lstrip('/')}"
    req = urllib.request.Request(url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s):  # noqa: S310 — bounded local probe
            return True
    except urllib.error.HTTPError as e:
        return e.code != 404
    except Exception:
        return False


def _probe_otlp(otlp_endpoint: str) -> tuple[bool, bool]:
    """Probe the OTLP endpoint → (traces_attached, metrics_attached). Attach
    only the paths the backend accepts (tempo accepts /v1/traces but 404s
    /v1/metrics). Prints a stderr note when unreachable / nothing attachable."""
    if not otlp_endpoint:
        return False, False
    if not _otlp_endpoint_reachable(otlp_endpoint):
        import sys
        print(
            f"[observability] OTLP endpoint {otlp_endpoint} not reachable at "
            f"startup; span + metric export disabled to avoid retry-storm. "
            f"Spans are still emitted in-memory.",
            file=sys.stderr,
        )
        return False, False
    traces = _otlp_path_accepts(otlp_endpoint, "v1/traces")
    metrics = _otlp_path_accepts(otlp_endpoint, "v1/metrics")
    if not traces and not metrics:
        import sys
        print(
            f"[observability] OTLP endpoint {otlp_endpoint} reachable but "
            f"neither /v1/traces nor /v1/metrics accepted POSTs. "
            f"Span + metric export disabled.",
            file=sys.stderr,
        )
    return traces, metrics


def _build_meter_provider(
    resource: Any, metrics_attached: bool, otlp_endpoint: str,
) -> MeterProvider:
    """Meter provider with an OTLP periodic exporter when metrics are
    attachable (interval env-tunable via OTEL_METRIC_EXPORT_INTERVAL_MS,
    default 60s); else an in-memory-only provider (counters still update for
    in-process readers — tests, dev assertions)."""
    if not metrics_attached:
        return MeterProvider(resource=resource)
    metric_exporter = OTLPMetricExporter(
        endpoint=f"{otlp_endpoint.rstrip('/')}/v1/metrics")
    interval = 60_000
    raw_iv = os.getenv("OTEL_METRIC_EXPORT_INTERVAL_MS")
    if raw_iv:
        with contextlib.suppress(ValueError):
            interval = max(1_000, int(raw_iv))
    reader = PeriodicExportingMetricReader(
        metric_exporter, export_interval_millis=interval)
    return MeterProvider(resource=resource, metric_readers=[reader])


def setup_observability() -> None:
    """Initialize OTEL + structlog. Safe to call multiple times."""
    global _initialized
    with _init_lock:
        if _initialized:
            return

        settings = get_settings()

        # ── OTEL trace provider ─────────────────────────────────────
        resource = Resource.create({
            "service.name": settings.otel_service_name,
            "service.version": settings.service_version,
            "deployment.environment": settings.environment,
        })

        sampler = ParentBased(TraceIdRatioBased(settings.otel_traces_sampler_arg))
        provider = TracerProvider(resource=resource, sampler=sampler)

        # OTLP HTTP exporter. Probe the endpoint at startup and ATTACH ONLY
        # what the backend actually accepts:
        #   - traces  → /v1/traces  (every OTLP receiver accepts this)
        #   - metrics → /v1/metrics (otel-collector yes; grafana/tempo NO)
        # We probe each path independently; a tracing-only backend gets the
        # span exporter but no metric exporter — avoids the "404 Not Found"
        # log spam every 60 seconds against tempo.
        otlp_endpoint = settings.otel_exporter_otlp_endpoint
        traces_attached, metrics_attached = _probe_otlp(otlp_endpoint)

        if traces_attached:
            exporter = OTLPSpanExporter(
                endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces"
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)

        # ── OTEL meter provider ─────────────────────────────────────
        meter_provider = _build_meter_provider(
            resource, metrics_attached, otlp_endpoint)
        otel_metrics.set_meter_provider(meter_provider)

        # ── asyncpg auto-instrumentation ────────────────────────────
        # One span per Postgres query (db.query). Closes the OTel gap
        # called out in the architecture map — DB latency is now visible
        # in the trace tree without hand-spanning every repository call.
        # Wrapped in try/except: a missing/incompatible instrumentation
        # package must never block service startup.
        try:
            from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
            AsyncPGInstrumentor().instrument()
        except Exception as exc:  # noqa: BLE001
            import sys
            print(
                f"[observability] asyncpg auto-instrumentation skipped: {exc}",
                file=sys.stderr,
            )

        # ── structlog ───────────────────────────────────────────────
        # Output JSON in prod, console in local for readability.
        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        logging.basicConfig(level=log_level, format="%(message)s")

        processors: list[Any] = [
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _otel_trace_context_processor,
        ]
        if settings.environment == "local":
            processors.append(structlog.dev.ConsoleRenderer(colors=True))
        else:
            processors.append(structlog.processors.JSONRenderer())

        structlog.configure(
            processors=processors,
            wrapper_class=structlog.make_filtering_bound_logger(
                getattr(logging, log_level)
            ),
            context_class=dict,
            cache_logger_on_first_use=True,
        )

        _initialized = True


def _otel_trace_context_processor(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Attach trace_id + span_id to every log line for correlation with traces."""
    span = trace.get_current_span()
    if span and span.get_span_context().is_valid:
        ctx = span.get_span_context()
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger. Auto-initializes observability on first call."""
    if not _initialized:
        setup_observability()
    return structlog.get_logger(name)


def get_tracer(name: str) -> trace.Tracer:
    """Get an OTEL tracer scoped to the given instrumentation name."""
    if not _initialized:
        setup_observability()
    return trace.get_tracer(name)


__all__ = [
    "setup_observability",
    "get_logger",
    "get_tracer",
    "capture_text_enabled",
    "safe_hash_text",
    "safe_text_len",
    "set_safe_text_attrs",
    "safe_json_attr",
    "safe_list_attr",
    "langfuse_capture_content_enabled",
    "redact_for_span",
    "set_langfuse_generation",
    "set_langfuse_io",
    "set_langfuse_trace",
    "increment",
    "histogram",
    "span",
    "llm_span",
    "record_exception_safe",
    "set_attrs",
    "current_trace_ids",
    "record_cache_get",
    "record_cache_set",
    "record_cache_delete",
    "inject_trace_headers",
    "extract_trace_context",
    "current_traceparent",
    "start_consumer_span",
]
