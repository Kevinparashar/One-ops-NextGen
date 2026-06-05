"""UC-5 B-refactor Phase 2b-ii — triage plan + assemble handler.

Locks (a) the serialised executor plan UC-5 feeds the main executor and (b) the
terminal assemble handler that reconstructs the three typed tool outputs from
`previous_results` and builds the Proposal. Hermetic — no executor, DB, or
gateway; the assemble handler is dependency-free by design.
"""
from __future__ import annotations

import oneops.use_cases.uc05_triage.handlers as h
from oneops.use_cases.uc05_triage.contracts import (
    AssignmentRecommendation,
    DuplicateCheckResult,
    PrioritizationResult,
    ScoredNeighbour,
)
from oneops.use_cases.uc05_triage.plan import (
    STEP_ASSEMBLE,
    STEP_ASSIGN,
    STEP_CHECK,
    STEP_PRIO,
    build_triage_plan,
)

_CTX_IDS = {"tenant_id": "T001", "user_id": "u1", "role": "service_desk_agent"}


# ── plan shape ───────────────────────────────────────────────────────────


def test_plan_is_the_check_fanout_assemble_dag():
    plan = build_triage_plan(service_id="incident", ticket_id="INC0001001")
    by_id = {s["step_id"]: s for s in plan}
    assert set(by_id) == {STEP_CHECK, STEP_ASSIGN, STEP_PRIO, STEP_ASSEMBLE}
    # every step names its tool explicitly (Phase 2b-i) + the same agent
    assert all(s["agent_id"] == "uc05_triage" and s["tool_id"] for s in plan)
    # fan-out: assign + prio depend on check; assemble fans in on all three
    assert by_id[STEP_CHECK]["depends_on"] == []
    assert by_id[STEP_ASSIGN]["depends_on"] == [STEP_CHECK]
    assert by_id[STEP_PRIO]["depends_on"] == [STEP_CHECK]
    assert set(by_id[STEP_ASSEMBLE]["depends_on"]) == {STEP_CHECK, STEP_ASSIGN, STEP_PRIO}


def test_plan_declares_data_flow_bindings():
    plan = build_triage_plan(service_id="incident", ticket_id="INC1")
    by_id = {s["step_id"]: s for s in plan}
    assign_binds = {b["to_param"]: b for b in by_id[STEP_ASSIGN]["parameter_bindings"]}
    assert assign_binds["candidates"]["from_step"] == STEP_CHECK
    assert assign_binds["candidates"]["from_field"] == "candidates"
    prio_binds = {b["to_param"] for b in by_id[STEP_PRIO]["parameter_bindings"]}
    assert prio_binds == {"suggested_category", "suggested_subcategory"}


# ── fixtures for the assemble handler ───────────────────────────────────────


def _neighbour(nid="INC0000002"):
    return ScoredNeighbour(
        id=nid, fields={"assignment_group": "Network"},
        vec_score=0.9, fts_score=1.0, fused_score=0.9)


def _check_result():
    return DuplicateCheckResult(
        candidates=[_neighbour()], top_match=_neighbour(),
        duplicate_verdict="none", duplicate_threshold=0.85,
        suggested_category="Network", suggested_subcategory="VPN")


def _assignment():
    return AssignmentRecommendation(
        assignment_group="Network", confidence=1.0, coverage=1.0, diversity=1,
        basis_ids=["INC0000002"], basis="majority_of_top_k", rationale="1/1")


def _prioritization():
    return PrioritizationResult(
        impact="On Department", urgency="High", priority="High",
        basis={"impact": "llm_inferred", "urgency": "sla_state",
               "priority": "matrix"})


def _prev(check=None, assign=None, prio=None):
    """Build a previous_results map keyed by step_id with status+output."""
    out = {}
    if check is not None:
        out[STEP_CHECK] = {"step_id": STEP_CHECK, "status": "success",
                           "output": check}
    if assign is not None:
        out[STEP_ASSIGN] = {"step_id": STEP_ASSIGN, "status": "success",
                            "output": assign}
    if prio is not None:
        out[STEP_PRIO] = {"step_id": STEP_PRIO, "status": "success",
                          "output": prio}
    return out


def _ctx(prev):
    return {**_CTX_IDS, "previous_results": prev}


# ── assemble: happy path ────────────────────────────────────────────────────


async def test_assemble_builds_proposal_from_three_outputs():
    prev = _prev(check=_check_result().model_dump(),
                 assign=_assignment().model_dump(),
                 prio=_prioritization().model_dump())
    out = await h.assemble_triage_proposal(
        {"service_id": "incident", "ticket_id": "INC0001001"}, _ctx(prev))
    assert out["ticket_id"] == "INC0001001"
    assert out["service_id"] == "incident"
    assert out["suggested_category"] == "Network"
    assert out["suggested_assignment_group"] == "Network"
    assert out["suggested_priority"] == "High"
    assert out["mutation_intent"] == "recommend_only"
    assert "overall_confidence_score" in out and "confidence_tier" in out


# ── assemble: error propagation + fallbacks (no silent failure) ─────────────


async def test_assemble_propagates_check_error():
    prev = {STEP_CHECK: {"step_id": STEP_CHECK, "status": "success",
                         "output": {"outcome": "not_found",
                                    "message": "INC9 not found"}}}
    out = await h.assemble_triage_proposal(
        {"service_id": "incident", "ticket_id": "INC9"}, _ctx(prev))
    assert out["outcome"] == "not_found"


async def test_assemble_uses_empty_assignment_when_assign_missing():
    # assign step absent (e.g. blocked) → "no neighbours" recommendation.
    prev = _prev(check=_check_result().model_dump(),
                 prio=_prioritization().model_dump())
    out = await h.assemble_triage_proposal(
        {"service_id": "incident", "ticket_id": "INC1"}, _ctx(prev))
    assert out["suggested_assignment_group"] is None     # empty-assignment fallback


async def test_assemble_invalid_request_without_ids():
    out = await h.assemble_triage_proposal(
        {"service_id": "incident"}, _ctx(_prev()))       # no ticket_id
    assert out["outcome"] == "invalid_request"


async def test_assemble_upstream_missing_when_prio_absent():
    prev = _prev(check=_check_result().model_dump(),
                 assign=_assignment().model_dump())       # no prio
    out = await h.assemble_triage_proposal(
        {"service_id": "incident", "ticket_id": "INC1"}, _ctx(prev))
    assert out["outcome"] == "upstream_missing"
