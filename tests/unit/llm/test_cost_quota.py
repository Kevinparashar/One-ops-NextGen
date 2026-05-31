"""Cost-accounting and quota-guard tests."""
from __future__ import annotations

import pytest

from oneops.errors import QuotaExceededError
from oneops.llm.cost import CostTracker, compute_cost, price_for
from oneops.llm.quota import QuotaGuard

# ── cost ─────────────────────────────────────────────────────────────────


def test_compute_cost_uses_the_price_table():
    # gpt-4o-mini = (0.15, 0.60) USD per 1M tokens.
    cost = compute_cost("gpt-4o-mini", prompt_tokens=1_000_000,
                        completion_tokens=1_000_000)
    assert cost == pytest.approx(0.15 + 0.60)


def test_unknown_model_falls_back_to_default_pricing():
    # An unrecognised model is never silently free.
    assert price_for("some-future-model") == (1.0, 3.0)
    assert compute_cost("some-future-model", 1_000_000, 0) == pytest.approx(1.0)


def test_tracker_accumulates_per_tenant_per_model():
    t = CostTracker()
    t.record("tenant-a", "gpt-4o-mini", 1000, 500)
    t.record("tenant-a", "gpt-4o-mini", 2000, 1000)
    usage = t.usage("tenant-a")["gpt-4o-mini"]
    assert usage["calls"] == 2
    assert usage["prompt_tokens"] == 3000
    assert usage["completion_tokens"] == 1500
    assert usage["cost_usd"] > 0


def test_tracker_isolates_tenants():
    t = CostTracker()
    t.record("tenant-a", "gpt-4o", 1000, 1000)
    t.record("tenant-b", "gpt-4o", 5000, 5000)
    assert t.total_cost("tenant-b") > t.total_cost("tenant-a")
    assert "gpt-4o" not in t.usage("tenant-c")        # untouched tenant


# ── quota ────────────────────────────────────────────────────────────────


def test_quota_allows_calls_under_the_limit():
    g = QuotaGuard(default_limit=3)
    g.check_and_charge("tenant-a")
    g.check_and_charge("tenant-a")
    assert g.used("tenant-a") == 2


def test_quota_raises_once_the_limit_is_spent():
    g = QuotaGuard(default_limit=2)
    g.check_and_charge("tenant-a")
    g.check_and_charge("tenant-a")
    with pytest.raises(QuotaExceededError, match="tenant-a"):
        g.check_and_charge("tenant-a")


def test_quota_is_per_tenant():
    g = QuotaGuard(default_limit=1)
    g.check_and_charge("tenant-a")
    g.check_and_charge("tenant-b")                    # b has its own budget
    with pytest.raises(QuotaExceededError):
        g.check_and_charge("tenant-a")


def test_per_tenant_limit_override():
    g = QuotaGuard(default_limit=1)
    g.set_tenant_limit("vip", 100)
    for _ in range(50):
        g.check_and_charge("vip")                     # no raise
    assert g.used("vip") == 50


def test_reset_window_clears_counters():
    g = QuotaGuard(default_limit=1)
    g.check_and_charge("tenant-a")
    g.reset_window()
    g.check_and_charge("tenant-a")                    # fresh budget — no raise


def test_zero_limit_means_unlimited():
    g = QuotaGuard(default_limit=0)
    for _ in range(1000):
        g.check_and_charge("tenant-a")                # never raises
