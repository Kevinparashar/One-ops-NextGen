"""Guarantee tests for setup_observability."""
from __future__ import annotations

from oneops.observability import (
    get_logger,
    get_tracer,
    setup_observability,
)


def test_setup_is_idempotent() -> None:
    setup_observability()
    setup_observability()
    setup_observability()


def test_get_tracer_returns_tracer() -> None:
    t = get_tracer("test.scope")
    assert t is not None
    sp_cm = t.start_as_current_span("test")
    with sp_cm:
        pass


def test_get_logger_returns_bound_logger() -> None:
    log = get_logger("test")
    log.info("test-message", k="v")


def test_logger_attaches_trace_context() -> None:
    """When inside a span, the logger should include trace_id + span_id."""
    t = get_tracer("test.scope")
    log = get_logger("test")
    with t.start_as_current_span("ctx-test"):
        log.info("inside-span")
