"""Unit tests for Tool 1: check_duplicate_candidates (Bundle A + LLM tiebreak).

Covers:
  • Duplicate verdict (top match >= threshold)
  • No-duplicate path (top match below threshold)
  • Per-field FieldSuggestion shape (confidence + coverage + diversity +
    basis_ids + basis + rationale)
  • Probe ticket filtered out of its own results
  • Loud failure on unknown service_id
  • Empty title/description → empty result, no crash
  • LLM tiebreak fires on contested fields when coverage clears floor
  • LLM tiebreak skipped when confidence is high
  • LLM tiebreak skipped when tiebreak_fn=None (production safe default)
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from oneops.use_cases.uc05_triage.contracts import (
    FieldSuggestion,
    ScoredNeighbour,
)
from oneops.use_cases.uc05_triage.retrieval.schema_loader import reset_cache
from oneops.use_cases.uc05_triage.tools.check_duplicates import (
    DEFAULT_TOP_K,
    check_duplicate_candidates,
)

# ── Fakes ────────────────────────────────────────────────────────────────────

class _FakeConn:
    def __init__(self, by_sub: dict[str, list[dict]]) -> None:
        self._by_sub = by_sub

    async def fetch(self, query: str, *args: Any) -> list[dict]:
        for needle, rows in self._by_sub.items():
            if needle in query:
                return [dict(r) for r in rows]
        return []


async def _ok_embed(text: str, *, tenant_id: str = "", user_id: str = "") -> list[float]:
    return [0.1] * 1536


def _inc_row(rid: str, **fields: Any) -> dict:
    base = {
        "id": rid,
        "title": f"T-{rid}",
        "description": "d",
        "category": "network",
        "subcategory": "vpn",
        "service_name": "Corporate VPN",
        "ci_id": "CI001",
        "assignment_group": "Network-L2",
        "status": "open",
        "created_at": datetime.now(UTC),
        "fts_score": 1.0,
        "vec_score": 0.9,
    }
    base.update(fields)
    return base


def _req_row(rid: str, **fields: Any) -> dict:
    base = {
        "id": rid,
        "title": f"T-{rid}",
        "description": "d",
        "category": "hardware",
        "catalog_item_id": "CAT0042",
        "ci_id": None,
        "assignment_group": "Hardware-Fulfilment",
        "status": "open",
        "created_at": datetime.now(UTC),
        "fts_score": 1.0,
        "vec_score": 0.9,
    }
    base.update(fields)
    return base


# ── Incident path (Bundle A field provenance) ────────────────────────────────

class TestIncidentPath:
    def setup_method(self) -> None:
        reset_cache()

    @pytest.mark.asyncio
    async def test_high_similarity_yields_duplicate_with_full_suggestion(self) -> None:
        now = datetime.now(UTC)
        neighbours = [
            _inc_row("INC0001001", category="network", subcategory="vpn",
                     service_name="Corporate VPN", ci_id="CI001", created_at=now),
            _inc_row("INC0001002", category="network", subcategory="vpn",
                     service_name="Corporate VPN", ci_id="CI001", created_at=now),
            _inc_row("INC0001003", category="network", subcategory="vpn",
                     service_name="Corporate VPN", ci_id="CI001", created_at=now),
        ]
        conn = _FakeConn({"ts_rank_cd": neighbours, "<=>": neighbours})
        ticket = {
            "incident_id": "INC0001175",
            "title": "VPN dropping at Mumbai",
            "description": "Constant disconnects",
            "ci_id": "CI001", "service_name": "Corporate VPN",
        }
        r = await check_duplicate_candidates(
            service_id="incident", tenant_id="t1",
            ticket_row=ticket, embed_fn=_ok_embed, conn=conn,
            duplicate_threshold=0.85, now=now,
        )
        assert r.duplicate_verdict == "duplicate"
        assert r.suggested_category == "network"
        # Bundle A: rich provenance
        fs_cat = r.field_suggestions["category"]
        assert isinstance(fs_cat, FieldSuggestion)
        assert fs_cat.value == "network"
        assert fs_cat.confidence == 1.0
        assert fs_cat.coverage == 1.0
        assert fs_cat.diversity == 1
        assert len(fs_cat.basis_ids) == 3
        assert fs_cat.basis == "majority_of_top_k"
        assert "3 of 3" in fs_cat.rationale or "similar" in fs_cat.rationale

    @pytest.mark.asyncio
    async def test_mixed_vote_below_floor_without_llm(self) -> None:
        """No tiebreak_fn passed → kNN result returned with below_confidence_floor basis."""
        now = datetime.now(UTC)
        # 2 network, 2 email, 1 storage = 0.4 confidence for the winner
        neighbours = [
            _inc_row("A", category="network", created_at=now),
            _inc_row("B", category="network", created_at=now),
            _inc_row("C", category="email", created_at=now),
            _inc_row("D", category="email", created_at=now),
            _inc_row("E", category="storage", created_at=now),
        ]
        conn = _FakeConn({"ts_rank_cd": neighbours, "<=>": neighbours})
        r = await check_duplicate_candidates(
            service_id="incident", tenant_id="t1",
            ticket_row={"title": "ambiguous", "description": "?"},
            embed_fn=_ok_embed, conn=conn, now=now,
        )
        fs_cat = r.field_suggestions["category"]
        assert fs_cat.confidence == 0.4
        assert fs_cat.diversity == 3
        assert fs_cat.basis == "below_confidence_floor"
        assert "split" in fs_cat.rationale.lower()

    @pytest.mark.asyncio
    async def test_empty_field_coverage_zero(self) -> None:
        now = datetime.now(UTC)
        neighbours = [
            _inc_row("A", subcategory=None, created_at=now),
            _inc_row("B", subcategory="", created_at=now),
            _inc_row("C", subcategory=None, created_at=now),
        ]
        conn = _FakeConn({"ts_rank_cd": neighbours, "<=>": neighbours})
        r = await check_duplicate_candidates(
            service_id="incident", tenant_id="t1",
            ticket_row={"title": "x", "description": "y"},
            embed_fn=_ok_embed, conn=conn, now=now,
        )
        fs_sub = r.field_suggestions["subcategory"]
        assert fs_sub.value is None
        assert fs_sub.coverage == 0.0
        assert fs_sub.basis == "empty_neighbours"


# ── LLM tiebreaker path ──────────────────────────────────────────────────────

class TestLLMTiebreak:
    def setup_method(self) -> None:
        reset_cache()

    @pytest.mark.asyncio
    async def test_llm_chooses_from_split_vote(self) -> None:
        """When confidence is split AND coverage is good, LLM picks the winner."""
        now = datetime.now(UTC)
        # 2 network, 2 email — perfectly split, both above coverage floor
        neighbours = [
            _inc_row("A", category="network", created_at=now),
            _inc_row("B", category="network", created_at=now),
            _inc_row("C", category="email", created_at=now),
            _inc_row("D", category="email", created_at=now),
        ]
        conn = _FakeConn({"ts_rank_cd": neighbours, "<=>": neighbours})

        llm_called_with: dict[str, Any] = {}

        async def fake_llm(*, probe_text, field, candidates, ticket_row):
            llm_called_with["field"] = field
            llm_called_with["candidates"] = candidates
            return "network"  # LLM picks network

        r = await check_duplicate_candidates(
            service_id="incident", tenant_id="t1",
            ticket_row={"title": "VPN", "description": "drops"},
            embed_fn=_ok_embed, conn=conn,
            tiebreak_fn=fake_llm, now=now,
        )
        fs_cat = r.field_suggestions["category"]
        assert fs_cat.value == "network"
        assert fs_cat.basis == "llm_tiebreak"
        assert "LLM" in fs_cat.rationale
        assert "semantic fit" in fs_cat.rationale
        assert llm_called_with["field"] == "category"
        values_passed = {c["value"] for c in llm_called_with["candidates"]}
        assert values_passed >= {"network", "email"}
        # Each candidate carries vote_count + example_titles so the LLM has grounding
        for c in llm_called_with["candidates"]:
            assert "vote_count" in c
            assert "example_titles" in c

    @pytest.mark.asyncio
    async def test_llm_skipped_when_confidence_high(self) -> None:
        """5/5 same → no tiebreak call."""
        now = datetime.now(UTC)
        neighbours = [_inc_row(c, category="network", created_at=now)
                      for c in "ABCDE"]
        conn = _FakeConn({"ts_rank_cd": neighbours, "<=>": neighbours})
        called: dict[str, int] = {"n": 0}

        async def counting_llm(**_):
            called["n"] += 1
            return "network"

        r = await check_duplicate_candidates(
            service_id="incident", tenant_id="t1",
            ticket_row={"title": "x", "description": "y"},
            embed_fn=_ok_embed, conn=conn,
            tiebreak_fn=counting_llm, now=now,
        )
        assert called["n"] == 0
        assert r.field_suggestions["category"].basis == "majority_of_top_k"

    @pytest.mark.asyncio
    async def test_llm_skipped_when_coverage_below_floor(self) -> None:
        """If only 1 of 5 had the field, don't bother LLM — corpus too sparse."""
        now = datetime.now(UTC)
        neighbours = [
            _inc_row("A", subcategory="vpn", created_at=now),
            _inc_row("B", subcategory=None, created_at=now),
            _inc_row("C", subcategory=None, created_at=now),
            _inc_row("D", subcategory=None, created_at=now),
            _inc_row("E", subcategory=None, created_at=now),
        ]
        conn = _FakeConn({"ts_rank_cd": neighbours, "<=>": neighbours})
        called: dict[str, int] = {"n": 0}

        async def counting_llm(**_):
            called["n"] += 1
            return "vpn"

        r = await check_duplicate_candidates(
            service_id="incident", tenant_id="t1",
            ticket_row={"title": "x", "description": "y"},
            embed_fn=_ok_embed, conn=conn,
            tiebreak_fn=counting_llm, now=now,
        )
        # coverage = 0.2 < COVERAGE_MIN_FOR_LLM (0.4) → no LLM call
        assert called["n"] == 0
        # Single vote → confidence=1.0 → majority_of_top_k basis
        assert r.field_suggestions["subcategory"].basis == "majority_of_top_k"

    @pytest.mark.asyncio
    async def test_llm_returns_invalid_value_falls_back(self) -> None:
        """If LLM returns a value not in candidates, treat as failed tiebreak."""
        now = datetime.now(UTC)
        neighbours = [
            _inc_row("A", category="network", created_at=now),
            _inc_row("B", category="email", created_at=now),
            _inc_row("C", category="storage", created_at=now),
        ]
        conn = _FakeConn({"ts_rank_cd": neighbours, "<=>": neighbours})

        async def hallucinating_llm(**_):
            return "kittens"  # not in candidates

        r = await check_duplicate_candidates(
            service_id="incident", tenant_id="t1",
            ticket_row={"title": "x", "description": "y"},
            embed_fn=_ok_embed, conn=conn,
            tiebreak_fn=hallucinating_llm, now=now,
        )
        # Falls back to kNN winner (first of equally-split → 'network'),
        # basis stays kNN, not llm_tiebreak
        assert r.field_suggestions["category"].basis != "llm_tiebreak"

    @pytest.mark.asyncio
    async def test_llm_exception_falls_back_gracefully(self) -> None:
        now = datetime.now(UTC)
        neighbours = [
            _inc_row("A", category="network", created_at=now),
            _inc_row("B", category="email", created_at=now),
        ]
        conn = _FakeConn({"ts_rank_cd": neighbours, "<=>": neighbours})

        async def broken_llm(**_):
            raise RuntimeError("LLM gateway down")

        r = await check_duplicate_candidates(
            service_id="incident", tenant_id="t1",
            ticket_row={"title": "x", "description": "y"},
            embed_fn=_ok_embed, conn=conn,
            tiebreak_fn=broken_llm, now=now,
        )
        # No crash; falls back to kNN
        assert r.field_suggestions["category"].basis != "llm_tiebreak"


# ── Request path ──────────────────────────────────────────────────────────────

class TestRequestPath:
    def setup_method(self) -> None:
        reset_cache()

    @pytest.mark.asyncio
    async def test_request_aggregates_spec_fields(self) -> None:
        """Per UC-5 spec (2026-05-29 PM): request aggregates category +
        assigned_to + ci_id. catalog_item_id no longer in aggregation_targets."""
        now = datetime.now(UTC)
        neighbours = [
            _req_row("SR0002001", category="hardware",
                     assignment_group="Hardware-Fulfilment",
                     assigned_to="alice@corp", ci_id="CI0001",
                     created_at=now),
            _req_row("SR0002002", category="hardware",
                     assignment_group="Hardware-Fulfilment",
                     assigned_to="alice@corp", ci_id="CI0001",
                     created_at=now),
            _req_row("SR0002003", category="software",
                     assignment_group="Apps-L2",
                     assigned_to="bob@corp", ci_id="CI0002",
                     created_at=now),
        ]
        conn = _FakeConn({"ts_rank_cd": neighbours, "<=>": neighbours})
        r = await check_duplicate_candidates(
            service_id="request", tenant_id="t1",
            ticket_row={"request_id": "SR0002100",
                        "title": "New laptop", "description": "for new joiner"},
            embed_fn=_ok_embed, conn=conn, now=now,
        )
        # Spec-aligned shortcuts
        assert r.suggested_category == "hardware"
        assert r.suggested_assigned_to == "alice@corp"  # 2 of 3
        assert r.suggested_ci_id == "CI0001"            # 2 of 3
        # Dropped fields are not on the model anymore — must not be accessible
        assert not hasattr(r, "suggested_catalog_item_id")
        assert not hasattr(r, "suggested_service_name")
        fs_cat = r.field_suggestions["category"]
        assert fs_cat.confidence == pytest.approx(2 / 3, rel=1e-3)
        assert fs_cat.coverage == 1.0
        assert fs_cat.diversity == 2


# ── Probe self-filter, loud failure, empty input ─────────────────────────────

class TestSafety:
    def setup_method(self) -> None:
        reset_cache()

    @pytest.mark.asyncio
    async def test_probe_filtered_from_own_results(self) -> None:
        now = datetime.now(UTC)
        self_row = _inc_row("INC0001175", created_at=now)
        other = _inc_row("INC0001001", created_at=now)
        conn = _FakeConn({"ts_rank_cd": [self_row, other],
                          "<=>": [self_row, other]})
        r = await check_duplicate_candidates(
            service_id="incident", tenant_id="t1",
            ticket_row={"incident_id": "INC0001175",
                        "title": "VPN", "description": "drops",
                        "ci_id": "CI001", "service_name": "Corporate VPN"},
            embed_fn=_ok_embed, conn=conn, now=now,
        )
        assert all(c.id != "INC0001175" for c in r.candidates)

    @pytest.mark.asyncio
    async def test_unknown_service_raises(self) -> None:
        from oneops.use_cases.uc05_triage.retrieval.schema_loader import (
            RetrievalSchemaError,
        )
        with pytest.raises(RetrievalSchemaError):
            await check_duplicate_candidates(
                service_id="problem", tenant_id="t1",
                ticket_row={"title": "x", "description": "y"},
                embed_fn=_ok_embed, conn=_FakeConn({}),
            )

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self) -> None:
        r = await check_duplicate_candidates(
            service_id="incident", tenant_id="t1",
            ticket_row={"title": "", "description": ""},
            embed_fn=_ok_embed, conn=_FakeConn({}),
        )
        assert r.duplicate_verdict == "none"
        assert r.candidates == []
        # Per spec-aligned aggregation_targets (incident): category, subcategory, assigned_to, ci_id
        for col in ("category", "subcategory", "assigned_to", "ci_id"):
            assert r.field_suggestions[col].basis == "empty_neighbours"


# ── Spec-aligned devil's-play: assigned_to + ci_id paths ────────────────────

class TestSpecAlignedAggregations:
    """UC-5 spec (2026-05-29 PM): aggregation_targets now include assigned_to
    + ci_id. These tests probe the realigned behaviour."""

    def setup_method(self) -> None:
        reset_cache()

    @pytest.mark.asyncio
    async def test_assigned_to_majority_vote(self) -> None:
        now = datetime.now(UTC)
        # 4 of 5 say alice, 1 says bob → confidence 0.8
        neighbours = [
            _inc_row("A", assigned_to="alice@corp", created_at=now),
            _inc_row("B", assigned_to="alice@corp", created_at=now),
            _inc_row("C", assigned_to="alice@corp", created_at=now),
            _inc_row("D", assigned_to="alice@corp", created_at=now),
            _inc_row("E", assigned_to="bob@corp", created_at=now),
        ]
        conn = _FakeConn({"ts_rank_cd": neighbours, "<=>": neighbours})
        r = await check_duplicate_candidates(
            service_id="incident", tenant_id="t1",
            ticket_row={"title": "VPN", "description": "drops"},
            embed_fn=_ok_embed, conn=conn, now=now,
        )
        fs = r.field_suggestions["assigned_to"]
        assert fs.value == "alice@corp"
        assert fs.confidence == 0.8
        assert r.suggested_assigned_to == "alice@corp"

    @pytest.mark.asyncio
    async def test_all_assigned_to_null_below_coverage(self) -> None:
        now = datetime.now(UTC)
        neighbours = [
            _inc_row(c, assigned_to=None, created_at=now)
            for c in "ABCDE"
        ]
        conn = _FakeConn({"ts_rank_cd": neighbours, "<=>": neighbours})
        r = await check_duplicate_candidates(
            service_id="incident", tenant_id="t1",
            ticket_row={"title": "x", "description": "y"},
            embed_fn=_ok_embed, conn=conn, now=now,
        )
        fs = r.field_suggestions["assigned_to"]
        assert fs.value is None
        assert fs.coverage == 0.0
        assert fs.basis == "empty_neighbours"
        assert r.suggested_assigned_to is None

    @pytest.mark.asyncio
    async def test_ci_id_aggregation_for_unlinked_probe(self) -> None:
        """Probe ticket has no ci_id; neighbours all share CI001 → suggest it."""
        now = datetime.now(UTC)
        neighbours = [
            _inc_row("A", ci_id="CI001", created_at=now),
            _inc_row("B", ci_id="CI001", created_at=now),
            _inc_row("C", ci_id="CI001", created_at=now),
            _inc_row("D", ci_id="CI002", created_at=now),
        ]
        conn = _FakeConn({"ts_rank_cd": neighbours, "<=>": neighbours})
        r = await check_duplicate_candidates(
            service_id="incident", tenant_id="t1",
            ticket_row={"incident_id": "INC_NEW",
                        "title": "VPN", "description": "drops",
                        "ci_id": None},  # explicitly unlinked
            embed_fn=_ok_embed, conn=conn, now=now,
        )
        fs = r.field_suggestions["ci_id"]
        assert fs.value == "CI001"
        assert fs.confidence == 0.75  # 3 of 4
        assert r.suggested_ci_id == "CI001"


# ── Step 5 enrichment: tag keywords ──────────────────────────────────────────

class TestTagKeywords:
    """Devil's-play coverage for the tag extractor.

    Locked rules (operator 2026-05-29):
      • Output is distinct lowercase tokens — never repeated
      • Hard cap MAX_TAGS=3; minimum 0; no padding when sparse
    """

    def setup_method(self) -> None:
        reset_cache()

    def _make_neighbours(self, titles: list[str]) -> list[ScoredNeighbour]:
        return [
            ScoredNeighbour(
                id=f"INC{i:04d}", fields={"title": t,
                                          "assignment_group": "Network-L2"},
                vec_score=0.9, fts_score=1.0, fused_score=0.9,
            )
            for i, t in enumerate(titles, start=1)
        ]

    async def _run(self, probe_title: str, neighbour_titles: list[str],
                   probe_description: str = "",
                   neighbour_descriptions: list[str] | None = None,
                   tag_fn=None,
                   suggested_category: str | None = None,
                   suggested_subcategory: str | None = None) -> list[str]:
        """Note: suggested_category/subcategory kwargs accepted but ignored —
        Fix B (2026-05-29 PM) removed the dedup. Kept for test compatibility."""
        from oneops.use_cases.uc05_triage.tools.check_duplicates import _extract_tags
        nds = neighbour_descriptions or [""] * len(neighbour_titles)
        cands = [
            ScoredNeighbour(
                id=f"INC{i:04d}",
                fields={"title": t, "description": d, "assignment_group": "X"},
                vec_score=0.9, fts_score=1.0, fused_score=0.9,
            )
            for i, (t, d) in enumerate(zip(neighbour_titles, nds, strict=False), start=1)
        ]
        return await _extract_tags(
            probe_title=probe_title,
            probe_description=probe_description,
            candidates=cands,
            tag_fn=tag_fn,
        )

    @pytest.mark.asyncio
    async def test_empty_inputs_returns_empty(self) -> None:
        tags = await self._run(probe_title="", neighbour_titles=[])
        assert tags == []

    @pytest.mark.asyncio
    async def test_all_stopwords_returns_empty(self) -> None:
        tags = await self._run(
            probe_title="the is and for",
            neighbour_titles=["the of an the", "is on a but"],
        )
        assert tags == []

    @pytest.mark.asyncio
    async def test_same_word_appears_once_in_output(self) -> None:
        """5 neighbours all have 'dropping'; tag list contains 'dropping' once."""
        tags = await self._run(
            probe_title="VPN dropping",
            neighbour_titles=["dropping again", "dropping", "dropping",
                              "dropping repeatedly", "dropping x"],
        )
        assert tags.count("dropping") == 1

    @pytest.mark.asyncio
    async def test_case_collapses_to_single_token(self) -> None:
        """VPN / vpn / Vpn should collapse into one lowercase tag."""
        tags = await self._run(
            probe_title="VPN error",
            neighbour_titles=["vpn drops", "Vpn flakes", "VPN issue"],
        )
        assert tags.count("vpn") <= 1
        assert "VPN" not in tags  # only lowercase

    @pytest.mark.asyncio
    async def test_identical_titles_still_distinct(self) -> None:
        tags = await self._run(
            probe_title="alpha beta",
            neighbour_titles=["alpha beta gamma"] * 5,
        )
        # Each distinct token once
        assert len(tags) == len(set(tags))
        # Should include at least one of the meaningful words
        assert set(tags) <= {"alpha", "beta", "gamma"}

    @pytest.mark.asyncio
    async def test_category_no_longer_deduped_fix_b(self) -> None:
        """Fix B (2026-05-29 PM): category dedup removed. 'vpn' can now be a tag."""
        tags = await self._run(
            probe_title="vpn dropping",
            neighbour_titles=["vpn issue", "vpn outage"],
            suggested_category="vpn",
        )
        # vpn is now ALLOWED as a tag (and likely to surface since it's frequent)
        assert "vpn" in tags

    @pytest.mark.asyncio
    async def test_subcategory_no_longer_deduped_fix_b(self) -> None:
        tags = await self._run(
            probe_title="vpn dropping",
            neighbour_titles=["vpn handoff", "vpn flake"],
            suggested_subcategory="vpn",
        )
        assert "vpn" in tags

    @pytest.mark.asyncio
    async def test_long_token_excluded(self) -> None:
        long_word = "a" + "b" * 35
        tags = await self._run(
            probe_title=f"normal {long_word} text",
            neighbour_titles=[f"more {long_word} more"],
        )
        assert long_word not in tags

    @pytest.mark.asyncio
    async def test_pure_digit_token_excluded(self) -> None:
        tags = await self._run(
            probe_title="error 0001002",
            neighbour_titles=["0001002 fails again", "code 0001002"],
        )
        assert "0001002" not in tags

    @pytest.mark.asyncio
    async def test_hyphenated_word_stays_whole(self) -> None:
        tags = await self._run(
            probe_title="Wi-Fi flakes",
            neighbour_titles=["Wi-Fi unstable"] * 4,
        )
        # tokenizer keeps Wi-Fi as one token; lowercased to wi-fi
        assert "wi-fi" in tags

    @pytest.mark.asyncio
    async def test_probe_boost_makes_probe_only_word_top_ranked(self) -> None:
        """Probe-only word should beat neighbour-only words appearing once each."""
        tags = await self._run(
            probe_title="mumbai gateway",
            neighbour_titles=["alpha", "beta", "gamma", "delta", "epsilon"],
        )
        # mumbai + gateway each get 2 (probe boost); each neighbour word gets 1
        # So mumbai + gateway should be top 2 of 3
        assert "mumbai" in tags
        assert "gateway" in tags

    @pytest.mark.asyncio
    async def test_hard_cap_at_three(self) -> None:
        """Many distinct meaningful tokens should still produce ≤3 tags."""
        # 10 distinct meaningful words, no duplicates
        long_title = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
        tags = await self._run(
            probe_title="zzzz",
            neighbour_titles=[long_title] * 3,
        )
        assert len(tags) <= 3
        # And no duplicates
        assert len(tags) == len(set(tags))

    @pytest.mark.asyncio
    async def test_no_padding_when_sparse(self) -> None:
        """If only 1 meaningful token survives, return 1 — don't pad with junk."""
        tags = await self._run(
            probe_title="x x x lonely",  # only 'lonely' survives length filter
            neighbour_titles=["is the of and", "to in on at"],
        )
        assert tags == ["lonely"]

    # ── Fix A — description tokenisation ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_description_contributes_to_tags(self) -> None:
        """Fix A (2026-05-29 PM): tokens from probe.description also count."""
        tags = await self._run(
            probe_title="error",  # title alone gives nothing
            probe_description="tunnel authentication failed kerberos timeout",
            neighbour_titles=["x", "y", "z"],
        )
        # Tokens from description should appear
        assert any(t in tags for t in ("tunnel", "authentication", "kerberos", "timeout"))

    @pytest.mark.asyncio
    async def test_neighbour_descriptions_contribute(self) -> None:
        tags = await self._run(
            probe_title="issue",
            neighbour_titles=["issue"] * 3,
            neighbour_descriptions=[
                "mailbox sync delay between exchange servers",
                "mailbox cluster failover",
                "exchange queue depth high",
            ],
        )
        assert "mailbox" in tags or "exchange" in tags

    # ── Fix E — LLM tagger path ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_llm_tagger_returns_clean_list(self) -> None:
        """Algorithmic is low-confidence (sparse corpus) → LLM fires."""
        called = {"n": 0}
        async def fake_llm(**_):
            called["n"] += 1
            return ["vpn", "tunnel", "wi-fi"]
        tags = await self._run(
            probe_title="VPN drops",
            neighbour_titles=["x", "y"],   # sparse → algorithmic < confident
            tag_fn=fake_llm,
        )
        assert tags == ["vpn", "tunnel", "wi-fi"]
        assert called["n"] == 1

    @pytest.mark.asyncio
    async def test_llm_always_runs_when_tag_fn_provided(self) -> None:
        """LLM-first policy (locked 2026-05-29 PM, industry consensus):
        when tag_fn is provided, run it — quality > token cost.
        The caller decides cost discipline by passing or omitting tag_fn."""
        called = {"n": 0}
        async def fake_llm(**_):
            called["n"] += 1
            return ["tunnel", "authentication", "retry"]
        tags = await self._run(
            probe_title="tunnel error",
            neighbour_titles=[
                "tunnel authentication failure", "tunnel reset",
                "authentication retry tunnel", "tunnel timeout",
                "authentication retry",
            ],
            tag_fn=fake_llm,
        )
        assert called["n"] == 1
        assert tags == ["tunnel", "authentication", "retry"]

    @pytest.mark.asyncio
    async def test_llm_receives_candidate_pool_hint(self) -> None:
        """LLM gets a pre-filtered candidate_pool kwarg (stopwords removed)
        as a hint, alongside the raw titles and descriptions."""
        captured = {}
        async def fake_llm(*, candidate_pool, **kwargs):
            captured["pool"] = candidate_pool
            return ["vpn"]
        await self._run(
            probe_title="VPN tunnel down",
            probe_description="the gateway is unreachable from the office",
            neighbour_titles=["VPN drops"],
            tag_fn=fake_llm,
        )
        # candidate_pool should be the algorithmic-filtered list — no stopwords
        assert "the" not in captured["pool"]
        assert "is" not in captured["pool"]
        # but real domain words should be present
        assert any(t in captured["pool"] for t in ("vpn", "tunnel", "gateway", "office"))

    @pytest.mark.asyncio
    async def test_llm_tagger_caps_at_three(self) -> None:
        async def fake_llm(**_):
            return ["alpha", "bravo", "charlie", "delta", "echo"]
        tags = await self._run(
            probe_title="x", neighbour_titles=["y"], tag_fn=fake_llm,
        )
        assert len(tags) == 3
        assert tags == ["alpha", "bravo", "charlie"]

    @pytest.mark.asyncio
    async def test_llm_tagger_dedupes(self) -> None:
        async def fake_llm(**_):
            return ["vpn", "VPN", "vpn", "tunnel"]
        tags = await self._run(
            probe_title="x", neighbour_titles=["y"], tag_fn=fake_llm,
        )
        assert tags == ["vpn", "tunnel"]

    @pytest.mark.asyncio
    async def test_llm_tagger_skips_non_strings(self) -> None:
        async def fake_llm(**_):
            return ["vpn", 42, None, {"oops": 1}, "tunnel"]
        tags = await self._run(
            probe_title="x", neighbour_titles=["y"], tag_fn=fake_llm,
        )
        assert tags == ["vpn", "tunnel"]

    @pytest.mark.asyncio
    async def test_llm_tagger_skips_stopwords_and_digits(self) -> None:
        async def fake_llm(**_):
            return ["the", "vpn", "12345", "  ", "tunnel"]
        tags = await self._run(
            probe_title="x", neighbour_titles=["y"], tag_fn=fake_llm,
        )
        assert tags == ["vpn", "tunnel"]

    @pytest.mark.asyncio
    async def test_llm_tagger_empty_falls_back_to_algorithmic(self) -> None:
        async def fake_llm(**_):
            return []  # LLM gave us nothing
        tags = await self._run(
            probe_title="vpn drops tunnel",
            neighbour_titles=["vpn", "tunnel", "drops"],
            tag_fn=fake_llm,
        )
        # Algorithmic path kicks in — should produce something
        assert len(tags) > 0

    @pytest.mark.asyncio
    async def test_llm_tagger_exception_falls_back(self) -> None:
        async def broken_llm(**_):
            raise RuntimeError("gateway down")
        tags = await self._run(
            probe_title="vpn drops tunnel",
            neighbour_titles=["vpn", "tunnel", "drops"],
            tag_fn=broken_llm,
        )
        assert len(tags) > 0  # algorithmic fallback worked

    @pytest.mark.asyncio
    async def test_llm_tagger_malformed_response_falls_back(self) -> None:
        async def odd_llm(**_):
            return "not a list, just a string"  # wrong type
        tags = await self._run(
            probe_title="vpn drops tunnel",
            neighbour_titles=["vpn", "tunnel", "drops"],
            tag_fn=odd_llm,
        )
        assert len(tags) > 0


# ── Default top-K = 5 ────────────────────────────────────────────────────────

class TestTopK:
    def test_default_top_k_is_five(self) -> None:
        assert DEFAULT_TOP_K == 5
