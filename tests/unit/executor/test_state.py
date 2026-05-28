"""ExecutorState tests — the step-results reducer and plan serialisation."""
from __future__ import annotations

from oneops.executor.state import merge_step_results, serialise_plan
from oneops.router.plan import PlanStep, RoutePlan


def test_merge_combines_two_partial_lists():
    left = [{"step_id": "step_1", "status": "success"}]
    right = [{"step_id": "step_2", "status": "success"}]
    merged = merge_step_results(left, right)
    assert {r["step_id"] for r in merged} == {"step_1", "step_2"}


def test_merge_dedups_by_step_id_right_wins():
    left = [{"step_id": "step_1", "status": "failed"}]
    right = [{"step_id": "step_1", "status": "success"}]
    merged = merge_step_results(left, right)
    assert len(merged) == 1
    assert merged[0]["status"] == "success"


def test_merge_handles_none_inputs():
    assert merge_step_results(None, None) == []
    assert merge_step_results(None, [{"step_id": "s"}]) == [{"step_id": "s"}]
    assert merge_step_results([{"step_id": "s"}], None) == [{"step_id": "s"}]


def test_merge_keeps_anonymous_results():
    merged = merge_step_results([{"status": "x"}], [{"status": "y"}])
    assert len(merged) == 2                  # no step_id → both kept


def test_serialise_plan_from_route_plan():
    plan = RoutePlan(steps=(
        PlanStep(step_id="step_1", agent_id="uc_a",
                 parameters=(("k", "v"),), depends_on=()),
        PlanStep(step_id="step_2", agent_id="uc_b", depends_on=("step_1",)),
    ))
    out = serialise_plan(plan)
    assert out == [
        {"step_id": "step_1", "agent_id": "uc_a",
         "parameters": {"k": "v"}, "depends_on": []},
        {"step_id": "step_2", "agent_id": "uc_b",
         "parameters": {}, "depends_on": ["step_1"]},
    ]
