"""Unit tests for the UC-5 retrieval engine.

In-memory fake connection so tests don't need Postgres. Covers:
  • SQL composition is schema-driven (incident vs request columns differ)
  • RRF fusion math
  • Rerank boosts (same_ci / same_service / recency)
  • Threshold gate (top match >= 0.85 → duplicate; below → None)
  • Degraded mode (embed_fn fails → vector branch empty, FTS still runs)
  • Dynamic field shapes (engine surfaces unknown columns)
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from oneops.use_cases.uc05_triage.retrieval import similarity_search as ss
from oneops.use_cases.uc05_triage.retrieval.schema_loader import reset_cache

# ── Helpers ──────────────────────────────────────────────────────────────────

class _FakeConn:
    """asyncpg-like fetch() that returns pre-canned rows by SQL substring."""

    def __init__(self, by_substring: dict[str, list[dict]]) -> None:
        self._by_sub = by_substring
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict]:
        self.calls.append((query, args))
        for needle, rows in self._by_sub.items():
            if needle in query:
                return [dict(r) for r in rows]
        return []


async def _ok_embed(text: str, *, tenant_id: str = "", user_id: str = "") -> list[float]:
    return [0.1] * 1536


async def _broken_embed(text: str, *, tenant_id: str = "", user_id: str = "") -> list[float]:
    raise RuntimeError("gateway down")


def _row(rid: str, **fields: Any) -> dict:
    base = {"id": rid, "title": f"T-{rid}", "category": "network",
            "fts_score": 1.0, "vec_score": 0.9}
    base.update(fields)
    return base


# ── SQL composition (schema-driven) ──────────────────────────────────────────

class TestSQLComposition:
    def setup_method(self) -> None:
        reset_cache()

    @pytest.mark.asyncio
    async def test_incident_sql_targets_incident_table(self) -> None:
        conn = _FakeConn({"FROM itsm.incident": []})
        await ss.search_similar(
            conn, service_id="incident", tenant_id="t1",
            probe_text="vpn", embed_fn=_ok_embed,
        )
        assert any("FROM itsm.incident" in q for q, _ in conn.calls)
        # Both FTS and vector SQL should target itsm.incident
        assert not any("FROM itsm.request" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_request_sql_targets_request_table(self) -> None:
        conn = _FakeConn({"FROM itsm.request": []})
        await ss.search_similar(
            conn, service_id="request", tenant_id="t1",
            probe_text="laptop", embed_fn=_ok_embed,
        )
        assert any("FROM itsm.request" in q for q, _ in conn.calls)
        assert not any("FROM itsm.incident" in q for q, _ in conn.calls)

    @pytest.mark.asyncio
    async def test_unknown_service_raises(self) -> None:
        from oneops.use_cases.uc05_triage.retrieval.schema_loader import (
            RetrievalSchemaError,
        )
        conn = _FakeConn({})
        with pytest.raises(RetrievalSchemaError):
            await ss.search_similar(
                conn, service_id="problem", tenant_id="t1",
                probe_text="x", embed_fn=_ok_embed,
            )


# ── RRF fusion ───────────────────────────────────────────────────────────────

class TestRRFFusion:
    def test_pure_fts_top_one(self) -> None:
        fused = ss._fuse_rrf([_row("A"), _row("B")], [])
        ids = [r["id"] for r in fused]
        assert ids == ["A", "B"]
        # rank 1 in fts → 1/(60+1) = ~0.01639
        assert fused[0]["_fused_score"] == pytest.approx(1 / 61, rel=1e-4)

    def test_pure_vector_top_one(self) -> None:
        fused = ss._fuse_rrf([], [_row("X"), _row("Y")])
        assert [r["id"] for r in fused] == ["X", "Y"]

    def test_both_branches_compound(self) -> None:
        # A appears rank 1 in fts AND rank 1 in vec → score = 2/(60+1)
        fused = ss._fuse_rrf([_row("A")], [_row("A")])
        assert len(fused) == 1
        assert fused[0]["_fused_score"] == pytest.approx(2 / 61, rel=1e-4)
        assert set(fused[0]["_sources"]) == {"fts", "vec"}

    def test_drops_rowless_id(self) -> None:
        fused = ss._fuse_rrf([{"title": "x", "fts_score": 1.0}], [])
        assert fused == []


# ── Rerank boosts ────────────────────────────────────────────────────────────

class TestRerank:
    def test_same_ci_lifts_score(self) -> None:
        a = {"id": "A", "ci_id": "CI001", "_fused_score": 0.04}
        b = {"id": "B", "ci_id": "CI999", "_fused_score": 0.04}
        out = ss._rerank([a, b], probe_ci_id="CI001",
                          probe_service_name=None, age_filter_days=30)
        assert out[0]["id"] == "A"
        assert "same_ci" in out[0]["_rerank_basis"]

    def test_same_service_lifts_incident(self) -> None:
        a = {"id": "A", "service_name": "Corporate VPN", "_fused_score": 0.04}
        b = {"id": "B", "service_name": "Other", "_fused_score": 0.04}
        out = ss._rerank([a, b], probe_ci_id=None,
                          probe_service_name="Corporate VPN", age_filter_days=30)
        assert out[0]["id"] == "A"

    def test_recency_decays_to_zero_at_window(self) -> None:
        now = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
        fresh = {"id": "FRESH",
                 "created_at": now - timedelta(hours=1),
                 "_fused_score": 0.04}
        stale = {"id": "STALE",
                 "created_at": now - timedelta(days=29, hours=23),
                 "_fused_score": 0.04}
        out = ss._rerank([fresh, stale], probe_ci_id=None,
                         probe_service_name=None, age_filter_days=30, now=now)
        assert out[0]["id"] == "FRESH"
        assert "recency" in str(out[0]["_rerank_basis"])

    def test_score_clamped_to_unit_interval(self) -> None:
        many = {"id": "X", "ci_id": "C", "service_name": "S",
                "created_at": datetime.now(UTC),
                "_fused_score": 0.5}
        out = ss._rerank([many], probe_ci_id="C", probe_service_name="S",
                         age_filter_days=30)
        assert 0.0 <= out[0]["_rerank_score"] <= 1.0


# ── End-to-end threshold gate ────────────────────────────────────────────────

class TestThresholdGate:
    def setup_method(self) -> None:
        reset_cache()

    @pytest.mark.asyncio
    async def test_high_score_yields_duplicate(self) -> None:
        # Same row surfaces in both branches AND matches CI/service → high rerank
        now = datetime.now(UTC)
        row = _row("INC0001001",
                   ci_id="CI001", service_name="Corporate VPN",
                   created_at=now)
        conn = _FakeConn({
            "ts_rank_cd": [row],
            "vector_cosine_ops": [row],
            "<=>": [row],
        })
        candidates, top_match = await ss.search_similar(
            conn, service_id="incident", tenant_id="t1",
            probe_text="VPN drops at Mumbai",
            embed_fn=_ok_embed,
            probe_ci_id="CI001",
            probe_service_name="Corporate VPN",
            now=now,
            duplicate_threshold=0.85,
        )
        assert len(candidates) >= 1
        assert top_match is not None
        assert top_match.id == "INC0001001"

    @pytest.mark.asyncio
    async def test_below_threshold_no_top_match(self) -> None:
        # Single row, FTS only, no CI/service match, old → low rerank
        old = datetime.now(UTC) - timedelta(days=29)
        row = _row("INC9999", ci_id="OTHER", service_name="Other",
                   created_at=old)
        conn = _FakeConn({"FROM itsm.incident": [row]})
        candidates, top_match = await ss.search_similar(
            conn, service_id="incident", tenant_id="t1",
            probe_text="vpn",
            embed_fn=_ok_embed,
            duplicate_threshold=0.85,
        )
        assert top_match is None


# ── Degraded mode (embed_fn fails) ───────────────────────────────────────────

class TestDegradedMode:
    def setup_method(self) -> None:
        reset_cache()

    @pytest.mark.asyncio
    async def test_embed_failure_falls_back_to_fts(self) -> None:
        row = _row("INC0001001",
                   ci_id="C", service_name="S",
                   created_at=datetime.now(UTC))
        conn = _FakeConn({"ts_rank_cd": [row]})
        candidates, _ = await ss.search_similar(
            conn, service_id="incident", tenant_id="t1",
            probe_text="vpn",
            embed_fn=_broken_embed,
        )
        assert len(candidates) == 1
        # Vector branch should NOT have been called (no vec literal in any args)
        assert all(
            not (isinstance(a, str) and a.startswith("[") and "," in a)
            for _, args in conn.calls for a in args
        )


# ── Dynamic field shape ──────────────────────────────────────────────────────

class TestDynamicFields:
    def setup_method(self) -> None:
        reset_cache()

    @pytest.mark.asyncio
    async def test_unknown_column_flows_into_fields_dict(self) -> None:
        """If service-schema adds a column, the engine must surface it without
        a code change. Verified by stuffing an unknown column into the row."""
        row = _row("INC0001001",
                   ci_id="C", service_name="S",
                   created_at=datetime.now(UTC),
                   future_field_we_dont_know="surprise")
        conn = _FakeConn({"FROM itsm.incident": [row]})
        candidates, _ = await ss.search_similar(
            conn, service_id="incident", tenant_id="t1",
            probe_text="vpn",
            embed_fn=_ok_embed,
        )
        assert candidates
        assert candidates[0].fields.get("future_field_we_dont_know") == "surprise"
