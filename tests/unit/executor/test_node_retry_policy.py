"""#5 — per-node RetryPolicy on the LLM decision nodes (route, control_gate).

Two things are locked here:
1. SEMANTICS: a RetryPolicy scoped `retry_on=UpstreamError` retries a transient
   upstream blip but does NOT retry a logic error (so bugs fail fast, infra blips
   get a second chance). This is the exact scoping graph.py uses.
2. WIRING: the compiled executor graph attaches a retry policy to `route` and
   `control_gate` (idempotent read/decide nodes) and NOT to the action-capable
   `run_step` node.
See docs/change-log.md #5.
"""
from __future__ import annotations

from operator import add
from typing import Annotated, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from oneops.errors import ConfigError, UpstreamError

# Fast policy (tiny interval) with the SAME scoping graph.py uses, so the
# semantics test runs in milliseconds rather than seconds of backoff.
_FAST = RetryPolicy(max_attempts=3, initial_interval=0.001,
                    max_interval=0.005, retry_on=UpstreamError)


class _S(TypedDict):
    seen: Annotated[list, add]


def _graph(exc: Exception, fail_times: int):
    calls = {"n": 0}

    def work(_state):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise exc
        return {"seen": [calls["n"]]}

    g: StateGraph = StateGraph(_S)
    g.add_node("work", work, retry_policy=_FAST)
    g.add_edge(START, "work")
    g.add_edge("work", END)
    return g.compile(), calls


# ── semantics ────────────────────────────────────────────────────────────────


def test_transient_upstream_error_is_retried_then_succeeds():
    graph, calls = _graph(UpstreamError("transient blip"), fail_times=2)
    out = graph.invoke({"seen": []})
    assert calls["n"] == 3          # 2 transient failures + 1 success
    assert out["seen"] == [3]


def test_logic_error_is_not_retried():
    # A non-upstream error (e.g. a bug) must fail fast — no wasted retries.
    graph, calls = _graph(ValueError("logic bug"), fail_times=5)
    with pytest.raises(ValueError):
        graph.invoke({"seen": []})
    assert calls["n"] == 1          # tried once, not retried


def test_config_error_is_not_retried():
    # ConfigError is a OneOpsError but NOT an UpstreamError → not retried.
    graph, calls = _graph(ConfigError("missing config"), fail_times=5)
    with pytest.raises(ConfigError):
        graph.invoke({"seen": []})
    assert calls["n"] == 1


def test_exhausting_attempts_reraises():
    # More transient failures than attempts → the error finally propagates.
    graph, calls = _graph(UpstreamError("still down"), fail_times=10)
    with pytest.raises(UpstreamError):
        graph.invoke({"seen": []})
    assert calls["n"] == 3          # capped at max_attempts


# ── wiring: the real executor graph attaches retry to the right nodes ────────


def _has_retry(node) -> bool:
    rp = getattr(node, "retry_policy", None)
    return bool(rp)  # tuple/list is truthy when a policy is attached


class _StubRouter:
    async def route(self, *a, **k):  # never invoked — we only compile the graph
        raise AssertionError("router.route should not be called in a compile-only test")


def test_executor_graph_wires_retry_on_decision_nodes_only(tmp_path):
    from oneops.executor.graph import build_executor_graph
    from oneops.registry.service import RegistryService
    from oneops.registry.store import FileBackend

    reg = RegistryService(FileBackend(tmp_path))      # empty registry is fine to compile
    graph = build_executor_graph(_StubRouter(), reg)
    nodes = graph.nodes
    # LLM decision nodes (idempotent) carry retry…
    assert _has_retry(nodes["route"]), "route should have a RetryPolicy"
    assert _has_retry(nodes["control_gate"]), "control_gate should have a RetryPolicy"
    # …the action-capable node does NOT (avoid double-executing writes on retry).
    assert not _has_retry(nodes["run_step"]), "run_step must NOT auto-retry"
