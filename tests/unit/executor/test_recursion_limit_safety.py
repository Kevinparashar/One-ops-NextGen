"""#1 Phase 1 — recursion-limit safety.

The wave⇄run_step loop spends supersteps per wave, so a too-low `recursion_limit`
could abort a legitimate deep plan mid-turn with an opaque error. `_safe_recursion_limit`
floors the configured value at a budget provably sufficient for the configured
generation depth, and never shrinks an operator's larger value. A real
`GraphRecursionError` is also made diagnosable (logged + tagged) rather than opaque.
See docs/planning/scheduler-refactor-scope.md (Phase 1).
"""
from __future__ import annotations

import oneops.executor.graph as g
import oneops.executor.nodes as nodes


def test_below_floor_is_raised_to_floor():
    # 1 is absurdly low → must be floored to a safe value well above it.
    out = g._safe_recursion_limit(1)
    assert out > 1
    assert out >= g._FIXED_SUPERSTEP_OVERHEAD


def test_value_at_or_above_floor_is_unchanged():
    # A generous value is preserved (we never shrink operator headroom).
    assert g._safe_recursion_limit(10_000) == 10_000


def test_default_60_is_safe_for_default_generation_depth(monkeypatch):
    monkeypatch.setattr(nodes, "DEFAULT_MAX_GENERATION_DEPTH", 3)
    # the shipped default (60) should already exceed the floor at depth 3
    floor = g._safe_recursion_limit(0)         # 0 forces "return floor"
    assert floor <= 60


def test_floor_tracks_generation_depth(monkeypatch):
    # Raising the generation-depth budget raises the safe floor — so a deeper
    # generation budget can't silently exceed a fixed limit.
    monkeypatch.setattr(nodes, "DEFAULT_MAX_GENERATION_DEPTH", 3)
    shallow = g._safe_recursion_limit(0)
    monkeypatch.setattr(nodes, "DEFAULT_MAX_GENERATION_DEPTH", 50)
    deep = g._safe_recursion_limit(0)
    assert deep > shallow


def test_floored_value_equals_the_computed_floor(monkeypatch):
    # Deterministic: below-floor input returns EXACTLY the documented formula,
    # so the flooring math is locked (not just ">").
    monkeypatch.setattr(nodes, "DEFAULT_MAX_GENERATION_DEPTH", 3)
    expected = (g._FIXED_SUPERSTEP_OVERHEAD
                + g._SUPERSTEPS_PER_WAVE
                * (g._INITIAL_PLAN_WAVE_ALLOWANCE + 3))
    assert g._safe_recursion_limit(0) == expected
    assert g._safe_recursion_limit(1) == expected      # any sub-floor → floor
