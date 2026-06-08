"""C-6 (P1-1) — turn timeouts are env-tunable, defaults unchanged.

The previously hard-coded timeout literals (60 / 65 / 90 s) are now typed Settings
fields. This locks two things: (1) the DEFAULTS equal the old literals, so behaviour
is unchanged unless an operator overrides; (2) env overrides actually flow through.
See docs/history/change-log.md Batch C-6.
"""
from __future__ import annotations

import pytest

from oneops.config import Settings, get_settings


def test_timeout_defaults_match_previous_literals():
    s = Settings()
    assert s.turn_timeout_seconds == 60.0            # was run_turn(..., timeout=60.0)
    assert s.turn_nats_outer_timeout_seconds == 65.0  # was outer wait_for timeout=65.0
    assert s.graph_worker_timeout_seconds == 90.0     # was GraphWorker default_timeout_s


def test_nats_outer_is_not_below_inner_by_default():
    # The outer NATS wrap must allow the inner round-trip to complete.
    s = Settings()
    assert s.turn_nats_outer_timeout_seconds >= s.turn_timeout_seconds


@pytest.mark.parametrize(("env", "field", "expected"), [
    ("TURN_TIMEOUT_SECONDS", "turn_timeout_seconds", 12.5),
    ("TURN_NATS_OUTER_TIMEOUT_SECONDS", "turn_nats_outer_timeout_seconds", 18.0),
    ("GRAPH_WORKER_TIMEOUT_SECONDS", "graph_worker_timeout_seconds", 30.0),
])
def test_env_override_flows_through(monkeypatch, env, field, expected):
    monkeypatch.setenv(env, str(expected))
    get_settings.cache_clear()
    try:
        assert getattr(get_settings(), field) == expected
    finally:
        get_settings.cache_clear()


def test_graph_worker_uses_settings_default(monkeypatch):
    monkeypatch.setenv("GRAPH_WORKER_TIMEOUT_SECONDS", "42.0")
    get_settings.cache_clear()
    try:
        from oneops.workers.graph_worker import GraphWorker
        w = GraphWorker(graph=object())
        assert w._timeout_s == 42.0
        # explicit caller value still wins
        w2 = GraphWorker(graph=object(), default_timeout_s=7.0)
        assert w2._timeout_s == 7.0
    finally:
        get_settings.cache_clear()
