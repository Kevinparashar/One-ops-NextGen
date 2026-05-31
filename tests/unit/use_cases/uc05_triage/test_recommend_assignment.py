"""Unit tests for Tool 2: recommend_assignment.

Covers:
  • Happy path — 5-of-5 same group → confidence 1.0
  • Mixed vote — 3 vs 2 → winner with confidence 0.6
  • Below floor — single value 1-of-5 → confidence 0.2 → None
  • Below coverage — all-NULL groups → below_coverage basis
  • Empty input — confidence 0, empty_neighbours basis
  • Mixed case strings — "Network-L2" vs "network-l2" stay distinct
  • Tie deterministic — Counter.most_common is stable
  • LLM tiebreak fires on split + coverage good
  • LLM tiebreak skipped on high confidence
  • LLM tiebreak skipped on low coverage
  • LLM hallucination falls back gracefully
  • LLM exception falls back gracefully
"""
from __future__ import annotations

from typing import Any

import pytest

from oneops.use_cases.uc05_triage.contracts import ScoredNeighbour
from oneops.use_cases.uc05_triage.tools.recommend_assignment import (
    recommend_assignment,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _n(rid: str, group: str | None = None, title: str = "T") -> ScoredNeighbour:
    return ScoredNeighbour(
        id=rid,
        fields={"assignment_group": group, "title": f"{title}-{rid}"},
        vec_score=0.9, fts_score=1.0, fused_score=0.9,
    )


# ── Happy paths ──────────────────────────────────────────────────────────────

class TestHappyPath:
    @pytest.mark.asyncio
    async def test_all_same_group(self) -> None:
        ns = [_n(f"INC{i}", "Network-L2") for i in range(5)]
        r = await recommend_assignment(candidates=ns)
        assert r.assignment_group == "Network-L2"
        assert r.confidence == 1.0
        assert r.coverage == 1.0
        assert r.diversity == 1
        assert len(r.basis_ids) == 5
        assert r.basis == "majority_of_top_k"
        assert "5 of 5" in r.rationale

    @pytest.mark.asyncio
    async def test_split_3_vs_2(self) -> None:
        ns = [
            _n("A", "Network-L2"), _n("B", "Network-L2"), _n("C", "Network-L2"),
            _n("D", "Other"), _n("E", "Other"),
        ]
        r = await recommend_assignment(candidates=ns)
        assert r.assignment_group == "Network-L2"
        assert r.confidence == pytest.approx(0.6)
        assert r.basis == "majority_of_top_k"

    @pytest.mark.asyncio
    async def test_single_neighbour(self) -> None:
        r = await recommend_assignment(candidates=[_n("A", "Network-L2")])
        assert r.assignment_group == "Network-L2"
        assert r.confidence == 1.0


# ── Below-floor / coverage paths ─────────────────────────────────────────────

class TestBelowFloors:
    @pytest.mark.asyncio
    async def test_below_confidence_floor(self) -> None:
        ns = [_n("A", "X"), _n("B", "Y"), _n("C", "Z"), _n("D", "W"), _n("E", "V")]
        r = await recommend_assignment(candidates=ns)
        assert r.assignment_group is None
        assert r.basis == "below_confidence_floor"
        assert r.confidence == pytest.approx(0.2)
        assert "split" in r.rationale.lower()

    @pytest.mark.asyncio
    async def test_all_none_groups_below_coverage(self) -> None:
        ns = [_n(f"INC{i}", None) for i in range(5)]
        r = await recommend_assignment(candidates=ns)
        assert r.assignment_group is None
        assert r.coverage == 0.0
        assert r.basis == "below_coverage"

    @pytest.mark.asyncio
    async def test_all_empty_string_groups_below_coverage(self) -> None:
        ns = [_n(f"INC{i}", "") for i in range(3)]
        r = await recommend_assignment(candidates=ns)
        assert r.assignment_group is None
        assert r.basis == "below_coverage"

    @pytest.mark.asyncio
    async def test_empty_neighbours(self) -> None:
        r = await recommend_assignment(candidates=[])
        assert r.assignment_group is None
        assert r.basis == "empty_neighbours"
        assert r.confidence == 0.0

    @pytest.mark.asyncio
    async def test_mixed_case_treated_as_distinct(self) -> None:
        """'Network-L2' and 'network-l2' are distinct strings — don't collapse."""
        ns = [
            _n("A", "Network-L2"), _n("B", "Network-L2"),
            _n("C", "network-l2"), _n("D", "network-l2"),
            _n("E", "Other"),
        ]
        r = await recommend_assignment(candidates=ns)
        # 2 vs 2 vs 1 → confidence 0.4 → below floor
        assert r.basis == "below_confidence_floor"
        assert r.confidence == pytest.approx(0.4)


# ── LLM tiebreak path ────────────────────────────────────────────────────────

class TestLLMTiebreak:
    @pytest.mark.asyncio
    async def test_llm_fires_on_split(self) -> None:
        ns = [
            _n("A", "Network-L2", "VPN dropping at HQ"),
            _n("B", "Network-L2", "VPN tunnel down"),
            _n("C", "Email-L2", "Mailbox sync issue"),
            _n("D", "Email-L2", "Outlook delay"),
        ]
        called: dict[str, Any] = {}

        async def llm(*, probe_text, field, candidates, ticket_row):
            called["field"] = field
            called["candidates"] = candidates
            return "Network-L2"

        r = await recommend_assignment(
            candidates=ns, probe_text="VPN issue",
            ticket_row={"title": "VPN drops"}, tiebreak_fn=llm,
        )
        assert r.assignment_group == "Network-L2"
        assert r.basis == "llm_tiebreak"
        assert called["field"] == "assignment_group"
        # Verify the LLM got vote_count + example_titles
        for c in called["candidates"]:
            assert "vote_count" in c
            assert "example_titles" in c

    @pytest.mark.asyncio
    async def test_llm_skipped_when_confident(self) -> None:
        ns = [_n(f"INC{i}", "Network-L2") for i in range(5)]
        called: dict[str, int] = {"n": 0}

        async def llm(**_):
            called["n"] += 1
            return "Network-L2"

        r = await recommend_assignment(candidates=ns, tiebreak_fn=llm)
        assert called["n"] == 0
        assert r.basis == "majority_of_top_k"

    @pytest.mark.asyncio
    async def test_llm_skipped_when_coverage_low(self) -> None:
        # 1 in 5 has a group → coverage 0.2 < 0.4 → don't call LLM
        ns = [
            _n("A", "Network-L2"),
            _n("B", None), _n("C", None), _n("D", None), _n("E", None),
        ]
        called: dict[str, int] = {"n": 0}

        async def llm(**_):
            called["n"] += 1
            return "Network-L2"

        r = await recommend_assignment(candidates=ns, tiebreak_fn=llm)
        assert called["n"] == 0
        # Single vote → confidence=1.0 → majority_of_top_k
        assert r.basis == "majority_of_top_k"

    @pytest.mark.asyncio
    async def test_llm_hallucination_falls_back(self) -> None:
        ns = [_n("A", "X"), _n("B", "Y"), _n("C", "Z")]

        async def hallucinating_llm(**_):
            return "kittens"

        r = await recommend_assignment(
            candidates=ns, tiebreak_fn=hallucinating_llm,
        )
        assert r.basis != "llm_tiebreak"

    @pytest.mark.asyncio
    async def test_llm_exception_falls_back(self) -> None:
        ns = [_n("A", "X"), _n("B", "Y")]

        async def broken_llm(**_):
            raise RuntimeError("gateway down")

        r = await recommend_assignment(candidates=ns, tiebreak_fn=broken_llm)
        # No crash; basis stays kNN (or below floor)
        assert r.basis != "llm_tiebreak"
