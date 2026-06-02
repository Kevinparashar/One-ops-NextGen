"""Plan-assembly tests — dependency expansion, exclusions, multi-sub-query DAG."""
from __future__ import annotations

import pytest

from oneops.registry.models import ExclusionRef
from oneops.router.plan import SubQueryRoute, assemble_plan

from ._factories import make_agent, make_registry


def _route(sq_id, agent_ids, *, depends_on_sq=()):
    return SubQueryRoute(sub_query_id=sq_id, agent_ids=list(agent_ids),
                         depends_on_subqueries=list(depends_on_sq))


# ── single sub-query ─────────────────────────────────────────────────────


def test_single_agent_single_step(tmp_path):
    reg = make_registry(tmp_path, [make_agent("uc_a")])
    plan = assemble_plan([_route("sq1", ["uc_a"])], reg)
    assert len(plan.steps) == 1
    assert plan.steps[0].agent_id == "uc_a"
    assert plan.steps[0].depends_on == ()
    assert plan.is_parallelisable is True


def test_registry_dependency_pulls_in_prerequisite(tmp_path):
    # uc_b depends_on uc_a — selecting uc_b must add uc_a as an upstream step.
    reg = make_registry(tmp_path, [
        make_agent("uc_a"),
        make_agent("uc_b", depends_on=("uc_a",)),
    ])
    plan = assemble_plan([_route("sq1", ["uc_b"])], reg)
    assert plan.agent_ids == ("uc_a", "uc_b")        # prerequisite first
    b_step = next(s for s in plan.steps if s.agent_id == "uc_b")
    a_step = next(s for s in plan.steps if s.agent_id == "uc_a")
    assert a_step.step_id in b_step.depends_on
    assert plan.is_parallelisable is False


def test_empty_routes_raises(tmp_path):
    reg = make_registry(tmp_path, [make_agent("uc_a")])
    with pytest.raises(ValueError, match="no routed sub-queries"):
        assemble_plan([], reg)
    with pytest.raises(ValueError, match="no routed sub-queries"):
        assemble_plan([_route("sq1", [])], reg)       # route with no agents


# ── exclusions ───────────────────────────────────────────────────────────


def test_exclusion_drops_the_lower_priority_agent(tmp_path):
    # uc_x excludes uc_y at priority 10; uc_y excludes uc_x at priority 1.
    # Both selected → uc_x wins, uc_y dropped.
    reg = make_registry(tmp_path, [
        make_agent("uc_x", excludes=(ExclusionRef(agent_id="uc_y", priority=10),)),
        make_agent("uc_y", excludes=(ExclusionRef(agent_id="uc_x", priority=1),)),
    ])
    plan = assemble_plan([_route("sq1", ["uc_x", "uc_y"])], reg)
    assert plan.agent_ids == ("uc_x",)


# ── multi sub-query ──────────────────────────────────────────────────────


def test_independent_subqueries_are_parallelisable(tmp_path):
    reg = make_registry(tmp_path, [make_agent("uc_a"), make_agent("uc_b")])
    plan = assemble_plan([
        _route("sq1", ["uc_a"]),
        _route("sq2", ["uc_b"]),
    ], reg)
    assert len(plan.steps) == 2
    assert plan.is_parallelisable is True            # no cross-SQ dependency


def test_dependent_subquery_steps_wait_on_their_upstream(tmp_path):
    # sq2 depends_on sq1 → sq2's step must depend on sq1's step.
    reg = make_registry(tmp_path, [make_agent("uc_a"), make_agent("uc_b")])
    plan = assemble_plan([
        _route("sq1", ["uc_a"]),
        _route("sq2", ["uc_b"], depends_on_sq=("sq1",)),
    ], reg)
    a_step = next(s for s in plan.steps if s.agent_id == "uc_a")
    b_step = next(s for s in plan.steps if s.agent_id == "uc_b")
    assert a_step.step_id in b_step.depends_on
    assert plan.is_parallelisable is False


def test_subquery_dependency_cycle_is_rejected(tmp_path):
    reg = make_registry(tmp_path, [make_agent("uc_a"), make_agent("uc_b")])
    with pytest.raises(ValueError, match="cycle"):
        assemble_plan([
            _route("sq1", ["uc_a"], depends_on_sq=("sq2",)),
            _route("sq2", ["uc_b"], depends_on_sq=("sq1",)),
        ], reg)


def test_dependent_subquery_runs_after_in_step_order(tmp_path):
    reg = make_registry(tmp_path, [make_agent("uc_a"), make_agent("uc_b")])
    plan = assemble_plan([
        _route("sq2", ["uc_b"], depends_on_sq=("sq1",)),   # listed first…
        _route("sq1", ["uc_a"]),                            # …but depends on sq1
    ], reg)
    # sq1's agent must appear before sq2's in the topologically-ordered plan.
    assert plan.agent_ids.index("uc_a") < plan.agent_ids.index("uc_b")


# ── data-flow bindings (D4: sub-query binding → step-level ParameterBinding) ──


def test_subquery_binding_maps_to_primary_step(tmp_path):
    reg = make_registry(tmp_path, [make_agent("uc_a"), make_agent("uc_b")])
    plan = assemble_plan([
        _route("sq1", ["uc_a"]),
        SubQueryRoute(sub_query_id="sq2", agent_ids=["uc_b"],
                      depends_on_subqueries=["sq1"],
                      bindings=[("sq1", "root_cause", "query")]),
    ], reg)
    by_agent = {s.agent_id: s for s in plan.steps}
    a, b = by_agent["uc_a"], by_agent["uc_b"]
    # The binding lands on uc_b, sourced from uc_a's terminal step, hard dep.
    assert len(b.parameter_bindings) == 1
    pb = b.parameter_bindings[0]
    assert (pb.from_step, pb.from_field, pb.to_param) == (a.step_id, "root_cause", "query")
    assert (a.step_id, "hard") in b.dependency_types
    assert a.step_id in b.depends_on          # ordering edge guaranteed
    assert a.parameter_bindings == ()          # source step carries none


def test_binding_to_unknown_source_is_dropped_not_fatal(tmp_path):
    """A binding naming a non-upstream sub-query is dropped (logged), and the
    plan still assembles — never an unresolvable plan, never a crash."""
    reg = make_registry(tmp_path, [make_agent("uc_a")])
    plan = assemble_plan([
        SubQueryRoute(sub_query_id="sq1", agent_ids=["uc_a"],
                      bindings=[("sq_ghost", "x", "y")]),
    ], reg)
    assert len(plan.steps) == 1
    assert plan.steps[0].parameter_bindings == ()   # bad binding dropped


def test_no_bindings_plan_is_unchanged(tmp_path):
    """Regression: a route with no bindings yields steps with empty binding
    fields (existing plans byte-identical)."""
    reg = make_registry(tmp_path, [make_agent("uc_a")])
    plan = assemble_plan([_route("sq1", ["uc_a"])], reg)
    assert plan.steps[0].parameter_bindings == ()
    assert plan.steps[0].dependency_types == ()
