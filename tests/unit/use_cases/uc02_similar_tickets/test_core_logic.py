"""UC-2 core — pure-logic tests for the re-rank and flag rules.

Live DB-backed coverage lives in integration tests; here we lock the
spec-mandated weight blend, the duplicate / resolution flag rules, and
the recency decay shape.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from oneops.use_cases.uc02_similar_tickets import core as uc02_core

# ── Composite weight constants ───────────────────────────────────────────────

def test_composite_weights_match_spec():
    """docs/product/ai-service-use-cases.md §UC-2: 0.60 + 0.25 + 0.15 = 1.0."""
    assert pytest.approx(0.60) == uc02_core._W_SEMANTIC
    assert pytest.approx(0.25) == uc02_core._W_METADATA
    assert pytest.approx(0.15) == uc02_core._W_RECENCY
    total = uc02_core._W_SEMANTIC + uc02_core._W_METADATA + uc02_core._W_RECENCY
    assert total == pytest.approx(1.0)


def test_flag_thresholds_match_spec():
    assert pytest.approx(0.90) == uc02_core._DUP_SIM
    assert pytest.approx(0.85) == uc02_core._RES_SIM


# ── Recency decay shape ──────────────────────────────────────────────────────

def test_recency_decay_today_is_one():
    now = datetime.now(UTC)
    assert uc02_core._recency_decay(now, now) == pytest.approx(1.0)


def test_recency_decay_old_is_smaller_than_recent():
    now = datetime.now(UTC)
    fresh = uc02_core._recency_decay(now - timedelta(days=1), now)
    old   = uc02_core._recency_decay(now - timedelta(days=365), now)
    assert fresh > old > 0.0


def test_recency_decay_none_is_zero():
    assert uc02_core._recency_decay(None, datetime.now(UTC)) == 0.0


def test_recency_decay_handles_naive_datetime():
    """Source rows may be naive; decay must not raise."""
    now = datetime.now(UTC)
    naive_yesterday = (now - timedelta(days=1)).replace(tzinfo=None)
    assert uc02_core._recency_decay(naive_yesterday, now) > 0.0


# ── Metadata signal ──────────────────────────────────────────────────────────

def test_metadata_signal_same_ci_dominates():
    src = {"ci_id": "CI1", "category": "network", "service_name": "VPN", "assignment_group": "L2"}
    cand = {"ci_id": "CI1", "category": "network", "service_name": "VPN", "assignment_group": "L2"}
    score, why = uc02_core._metadata_signal(source=src, cand=cand)
    assert score == pytest.approx(1.0)
    assert {"same_ci", "same_category", "same_service", "same_group"} <= set(why)


def test_metadata_signal_empty_source_is_zero():
    """Empty source fields must not match empty candidate fields — that would
    be a false positive on every poorly-tagged ticket."""
    src = {"ci_id": "", "category": None}
    cand = {"ci_id": "", "category": None}
    score, why = uc02_core._metadata_signal(source=src, cand=cand)
    assert score == 0.0
    assert why == []


def test_metadata_signal_no_match_is_zero():
    src = {"ci_id": "CI1", "category": "network"}
    cand = {"ci_id": "CI2", "category": "auth"}
    score, _ = uc02_core._metadata_signal(source=src, cand=cand)
    assert score == 0.0


# ── Flag rules (spec §UC-2 'Duplicate Detection Rules') ──────────────────────

def test_flag_likely_duplicate_requires_all_three_conditions():
    src = {"ci_id": "CI1"}
    cand_open_same_ci = {"ci_id": "CI1", "status": "open"}
    assert uc02_core._flag_for(sem=0.95, source=src, cand=cand_open_same_ci) == "likely_duplicate"

    cand_open_diff_ci = {"ci_id": "CI2", "status": "open"}
    assert uc02_core._flag_for(sem=0.95, source=src, cand=cand_open_diff_ci) != "likely_duplicate"

    cand_resolved_same_ci = {"ci_id": "CI1", "status": "resolved"}
    assert uc02_core._flag_for(sem=0.95, source=src, cand=cand_resolved_same_ci) != "likely_duplicate"

    cand_lowsim = {"ci_id": "CI1", "status": "open"}
    assert uc02_core._flag_for(sem=0.80, source=src, cand=cand_lowsim) != "likely_duplicate"


def test_flag_resolution_available_for_resolved_above_threshold():
    src = {"ci_id": "CI1"}
    cand = {"status": "resolved"}
    assert uc02_core._flag_for(sem=0.88, source=src, cand=cand) == "resolution_available"
    cand_closed = {"status": "closed"}
    assert uc02_core._flag_for(sem=0.86, source=src, cand=cand_closed) == "resolution_available"


def test_flag_duplicate_beats_resolution_when_both_could_fire():
    """Spec precedence: duplicate beats resolution. (Edge case: same CI,
    resolved, sim>0.90 → only one flag fires, and it's the duplicate one
    when status='open'.) When status is resolved, duplicate cannot fire,
    so resolution wins by elimination — verifying the precedence here:"""
    src = {"ci_id": "CI1"}
    # status=open AND sim>0.90 AND same_ci → duplicate, not resolution
    cand = {"ci_id": "CI1", "status": "open"}
    assert uc02_core._flag_for(sem=0.95, source=src, cand=cand) == "likely_duplicate"


def test_flag_returns_none_when_no_rule_fires():
    src = {"ci_id": "CI1"}
    cand = {"ci_id": "CI1", "status": "open"}
    assert uc02_core._flag_for(sem=0.50, source=src, cand=cand) is None


# ── pgvector literal serializer ──────────────────────────────────────────────

def test_vec_literal_round_trips():
    vec = [0.1, 0.2, 0.3]
    s = uc02_core._vec_literal(vec)
    assert s.startswith("[")
    assert s.endswith("]")
    parsed = uc02_core._parse_pgvector(s)
    assert parsed == pytest.approx(vec)


# ── Default RBAC ─────────────────────────────────────────────────────────────

def test_default_rbac_end_user_restricted_per_service():
    sql_inc, args_inc = uc02_core._default_rbac("end_user", "u1", "incident")
    assert "reported_by" in sql_inc
    assert args_inc == ["u1"]
    sql_req, args_req = uc02_core._default_rbac("end_user", "u1", "request")
    assert "requested_by" in sql_req
    assert args_req == ["u1"]


def test_default_rbac_service_desk_is_open():
    sql, args = uc02_core._default_rbac("service_desk_agent", "u1", "incident")
    assert sql == "TRUE"
    assert args == []
