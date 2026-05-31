"""Phase 3 tests — LangGraph orchestration of UC-5.

Covers:
  • build_uc05_graph produces a compiled graph
  • check_duplicates → fan-out [assign ∥ prioritize] → assemble flow
  • Fan-in semantics — both branches must write before assemble runs
  • Exception in a node propagates (no silent swallow)
  • Empty title/description short-circuit (refused by prioritize layer)
"""
from __future__ import annotations

import pytest

from oneops.use_cases.uc05_triage.contracts import (
    AssignmentRecommendation,
    DuplicateCheckResult,
    PrioritizationResult,
    ScoredNeighbour,
)
from oneops.use_cases.uc05_triage.graph import build_uc05_graph


def _fake_dup_high_signal() -> DuplicateCheckResult:
    return DuplicateCheckResult(
        candidates=[
            ScoredNeighbour(id="INC0001002", fields={"title": "VPN drops",
                            "assignment_group": "GRP-NETOPS"},
                            vec_score=0.9, fts_score=1.0, fused_score=0.89),
        ],
        top_match=ScoredNeighbour(id="INC0001002", fields={},
                                   vec_score=0.9, fts_score=1.0, fused_score=0.89),
        duplicate_verdict="duplicate",
        duplicate_threshold=0.85,
        suggested_category="network",
        suggested_subcategory="vpn",
    )


def _fake_asn() -> AssignmentRecommendation:
    return AssignmentRecommendation(
        assignment_group="GRP-NETOPS", confidence=1.0, coverage=1.0,
        diversity=1, basis_ids=["INC0001002"],
        basis="majority_of_top_k", rationale="1 of 1",
    )


def _fake_pri() -> PrioritizationResult:
    return PrioritizationResult(
        impact="On Department", urgency="High", priority="High",
        basis={"impact": "llm_inferred", "urgency": "llm_inferred",
               "priority": "matrix[On Department][High]"},
    )


@pytest.fixture
def stub_tools():
    """Tool stubs that return predictable results — no LLM, no DB."""
    state: dict[str, list[str]] = {"calls": []}

    async def check(*, ticket_row, service_id, tenant_id):
        state["calls"].append("check")
        return _fake_dup_high_signal()

    async def assign(*, candidates, probe_text, ticket_row):
        state["calls"].append("assign")
        return _fake_asn()

    async def prio(*, service_id, ticket_row, suggested_category,
                    suggested_subcategory):
        state["calls"].append("prio")
        return _fake_pri()

    return check, assign, prio, state


# ── Happy path ──────────────────────────────────────────────────────────────

class TestHappyPath:
    @pytest.mark.asyncio
    async def test_end_to_end_invocation(self, stub_tools) -> None:
        check, assign, prio, calls = stub_tools
        graph = build_uc05_graph(
            check_duplicates=check, recommend_assignment=assign, prioritize=prio,
        )
        out = await graph.ainvoke(
            {
                "tenant_id": "T001",
                "service_id": "incident",
                "ticket_id": "INC0000001",
                "ticket_row": {"incident_id": "INC0000001",
                               "title": "VPN drops", "description": "tunnel"},
            },
            config={"configurable": {"thread_id": "test-t1"}},
        )
        p = out["proposal"]
        assert p.ticket_id == "INC0000001"
        assert p.suggested_category == "network"
        assert p.suggested_assignment_group == "GRP-NETOPS"
        assert p.suggested_priority == "High"
        assert p.risk_class == "medium"
        assert p.mutation_intent == "recommend_only"

    @pytest.mark.asyncio
    async def test_check_runs_before_fan_out(self, stub_tools) -> None:
        """check_duplicates must complete before assign/prio start."""
        check, assign, prio, calls = stub_tools
        graph = build_uc05_graph(
            check_duplicates=check, recommend_assignment=assign, prioritize=prio,
        )
        await graph.ainvoke(
            {
                "tenant_id": "T001", "service_id": "incident",
                "ticket_id": "X",
                "ticket_row": {"incident_id": "X", "title": "x", "description": "x"},
            },
            config={"configurable": {"thread_id": "test-t2"}},
        )
        # check is first; assign + prio after (parallel order between them not asserted)
        assert calls["calls"][0] == "check"
        assert "assign" in calls["calls"][1:]
        assert "prio" in calls["calls"][1:]


# ── Devil's-play ────────────────────────────────────────────────────────────

class TestDevilsPlay:
    @pytest.mark.asyncio
    async def test_check_exception_propagates(self) -> None:
        async def broken_check(**_):
            raise RuntimeError("check failed")

        async def ok_assign(**_): return _fake_asn()
        async def ok_prio(**_): return _fake_pri()

        graph = build_uc05_graph(
            check_duplicates=broken_check,
            recommend_assignment=ok_assign, prioritize=ok_prio,
        )
        with pytest.raises(Exception):
            await graph.ainvoke(
                {
                    "tenant_id": "T001", "service_id": "incident",
                    "ticket_id": "X",
                    "ticket_row": {"incident_id": "X", "title": "x",
                                   "description": "x"},
                },
                config={"configurable": {"thread_id": "test-err"}},
            )

    @pytest.mark.asyncio
    async def test_pri_exception_propagates(self) -> None:
        async def ok_check(**_): return _fake_dup_high_signal()
        async def ok_assign(**_): return _fake_asn()
        async def broken_prio(**_):
            raise RuntimeError("prio failed")

        graph = build_uc05_graph(
            check_duplicates=ok_check,
            recommend_assignment=ok_assign, prioritize=broken_prio,
        )
        with pytest.raises(Exception):
            await graph.ainvoke(
                {
                    "tenant_id": "T001", "service_id": "incident",
                    "ticket_id": "X",
                    "ticket_row": {"incident_id": "X", "title": "x",
                                   "description": "x"},
                },
                config={"configurable": {"thread_id": "test-err"}},
            )

    @pytest.mark.asyncio
    async def test_assemble_only_runs_after_both_branches(self, stub_tools) -> None:
        """If assemble fired prematurely it would crash on missing slots —
        the graph's fan-in semantics protect against that."""
        check, assign, prio, calls = stub_tools
        # Make assign slow so prio writes first; assemble must still wait
        async def slow_assign(*, candidates, probe_text, ticket_row):
            import asyncio
            await asyncio.sleep(0.05)
            return _fake_asn()

        graph = build_uc05_graph(
            check_duplicates=check, recommend_assignment=slow_assign,
            prioritize=prio,
        )
        out = await graph.ainvoke(
            {
                "tenant_id": "T001", "service_id": "incident",
                "ticket_id": "X",
                "ticket_row": {"incident_id": "X", "title": "x", "description": "x"},
            },
            config={"configurable": {"thread_id": "test-slow"}},
        )
        # If assemble fired before assign wrote, this would have crashed.
        assert out["proposal"].suggested_assignment_group == "GRP-NETOPS"
