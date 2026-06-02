"""Data-flow binding — `previous_results` (D1 pure core + D2/D3 wiring).

D1 tests here are pure: `dotted_get` + `_resolve_bindings` exercised directly,
no graph. The end-to-end wiring (a step consuming an upstream step's output
across waves, blocked surfacing) lives in `test_graph.py`.
"""
from __future__ import annotations

from oneops.executor.nodes import _resolve_bindings, dotted_get

# ── dotted_get ─────────────────────────────────────────────────────────────


def test_dotted_get_top_level_and_nested():
    out = {"affected_ci_ids": ["ci1", "ci2"], "summary": {"root_cause": "leak"}}
    assert dotted_get(out, "affected_ci_ids") == ["ci1", "ci2"]
    assert dotted_get(out, "summary.root_cause") == "leak"


def test_dotted_get_missing_segment_returns_none():
    out = {"summary": {"root_cause": "leak"}}
    assert dotted_get(out, "summary.nope") is None
    assert dotted_get(out, "ghost") is None
    assert dotted_get(None, "x") is None
    assert dotted_get(out, "") is None


def test_dotted_get_walks_object_attrs():
    class _O:
        def __init__(self):
            self.score = 72
    assert dotted_get({"obj": _O()}, "obj.score") == 72


# ── _resolve_bindings ────────────────────────────────────────────────────────


def _step(bindings=None, dep_types=None):
    s = {"step_id": "step_2", "agent_id": "uc_b", "depends_on": ["step_1"]}
    if bindings is not None:
        s["parameter_bindings"] = bindings
    if dep_types is not None:
        s["dependency_types"] = dep_types
    return s


def _ok(output):
    return {"step_id": "step_1", "agent_id": "uc_a", "status": "success",
            "output": output}


def test_no_bindings_is_noop():
    bound, status, reason = _resolve_bindings(_step(), {})
    assert (bound, status) == ({}, "ok")


def test_success_binding_maps_value():
    bind = [{"from_step": "step_1", "from_field": "ticket_id",
             "to_param": "id", "required": True}]
    prev = {"step_1": _ok({"ticket_id": "INC0001021"})}
    bound, status, _ = _resolve_bindings(_step(bind), prev)
    assert status == "ok"
    assert bound == {"id": "INC0001021"}


def test_structured_value_passes_intact_not_stringified():
    bind = [{"from_step": "step_1", "from_field": "affected_ci_ids",
             "to_param": "ci_ids", "required": True}]
    prev = {"step_1": _ok({"affected_ci_ids": ["ci1", "ci2", "ci3"]})}
    bound, status, _ = _resolve_bindings(_step(bind), prev)
    assert status == "ok"
    assert bound == {"ci_ids": ["ci1", "ci2", "ci3"]}   # list preserved, typed


def test_hard_dep_failed_blocks():
    bind = [{"from_step": "step_1", "from_field": "x", "to_param": "y"}]
    prev = {"step_1": {"step_id": "step_1", "status": "failed", "output": None}}
    bound, status, reason = _resolve_bindings(_step(bind), prev)
    assert status == "blocked"
    assert "step_1" in reason


def test_hard_dep_missing_result_blocks():
    bind = [{"from_step": "step_1", "from_field": "x", "to_param": "y"}]
    bound, status, reason = _resolve_bindings(_step(bind), {})   # no prev at all
    assert status == "blocked"


def test_soft_dep_failed_is_omitted_not_blocked():
    bind = [{"from_step": "step_1", "from_field": "x", "to_param": "y"}]
    prev = {"step_1": {"step_id": "step_1", "status": "failed", "output": None}}
    bound, status, _ = _resolve_bindings(
        _step(bind, dep_types=[["step_1", "soft"]]), prev)
    assert status == "ok"
    assert bound == {}                                  # best-effort: omitted


def test_required_field_missing_blocks():
    bind = [{"from_step": "step_1", "from_field": "absent",
             "to_param": "y", "required": True}]
    prev = {"step_1": _ok({"present": 1})}
    bound, status, reason = _resolve_bindings(_step(bind), prev)
    assert status == "blocked"
    assert "y" in reason


def test_optional_field_missing_is_omitted():
    bind = [{"from_step": "step_1", "from_field": "absent",
             "to_param": "y", "required": False}]
    prev = {"step_1": _ok({"present": 1})}
    bound, status, _ = _resolve_bindings(_step(bind), prev)
    assert status == "ok"
    assert bound == {}
