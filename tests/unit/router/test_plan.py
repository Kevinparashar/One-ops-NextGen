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
