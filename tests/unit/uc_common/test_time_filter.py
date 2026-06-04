"""TimeFilter — schema, validators, year-inference, OTel attrs."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from oneops.uc_common import TimeFilter

# ── Construction & defaults ──────────────────────────────────────────────────

def test_default_empty_filter():
    tf = TimeFilter()
    assert tf.is_empty()
    assert not tf.has_relative()
    assert tf.boundary == "created_at"


def test_relative_window_round_trip():
    tf = TimeFilter(relative_days=7, label="last week")
    assert tf.has_relative()
    assert not tf.is_empty()
    assert tf.relative_days == 7


def test_absolute_window_round_trip():
    tf = TimeFilter(start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
                    label="in January 2026")
    assert not tf.is_empty()
    assert tf.start_date == date(2026, 1, 1)


def test_only_start_or_only_end_is_valid():
    """`since May 1` → start only; `older than 6 months` → end only."""
    a = TimeFilter(start_date=date(2026, 1, 1), label="since 1 Jan")
    b = TimeFilter(end_date=date(2025, 1, 1), label="before 2025")
    assert not a.is_empty()
    assert not b.is_empty()


# ── Mutual exclusion ─────────────────────────────────────────────────────────

def test_relative_and_start_date_together_rejected():
    with pytest.raises(ValidationError) as exc:
        TimeFilter(relative_days=7, start_date=date(2026, 1, 1))
    assert "mutually exclusive" in str(exc.value)


def test_relative_and_end_date_together_rejected():
    with pytest.raises(ValidationError):
        TimeFilter(relative_days=7, end_date=date(2026, 1, 1))


# ── Bounds & ordering ────────────────────────────────────────────────────────

def test_backwards_range_rejected():
    with pytest.raises(ValidationError) as exc:
        TimeFilter(start_date=date(2026, 6, 1), end_date=date(2026, 1, 1))
    assert "after end_date" in str(exc.value)


def test_relative_days_zero_rejected():
    with pytest.raises(ValidationError):
        TimeFilter(relative_days=0)


def test_relative_days_huge_rejected():
    with pytest.raises(ValidationError):
        TimeFilter(relative_days=4000)


def test_label_too_long_rejected():
    with pytest.raises(ValidationError):
        TimeFilter(relative_days=7, label="x" * 200)


def test_extra_field_rejected():
    with pytest.raises(ValidationError):
        TimeFilter(relative_days=7, unknown_field="oops")


# ── Year inference (the "since November" edge case) ─────────────────────────

def test_start_date_clearly_in_future_rolls_back_a_year():
    """If today is e.g. Jan 2026 and the LLM resolves 'since November' to
    2026-11-01, that's clearly user-meant past — roll back to 2025-11-01."""
    far_future = date.today() + timedelta(days=180)
    tf = TimeFilter(start_date=far_future, label="since November")
    assert tf.start_date.year == far_future.year - 1


def test_near_future_within_grace_kept_as_is():
    """A date 3 days out is intentionally future (closing Friday, etc.)."""
    near_future = date.today() + timedelta(days=3)
    tf = TimeFilter(start_date=near_future, label="this week")
    assert tf.start_date == near_future


def test_end_date_in_future_rolled_back():
    far_future = date.today() + timedelta(days=200)
    tf = TimeFilter(end_date=far_future, label="by year-end")
    assert tf.end_date.year == far_future.year - 1


def test_past_dates_left_alone():
    """Year inference must not touch dates that are already in the past."""
    past = date.today() - timedelta(days=400)
    tf = TimeFilter(start_date=past)
    assert tf.start_date == past


def test_year_inference_failure_inverts_range_is_rejected():
    """If start rolls back but end doesn't, and the new start > end,
    we surface the bug instead of silently swapping."""
    far_future_start = date.today() + timedelta(days=180)
    near_future_end = date.today() + timedelta(days=3)
    with pytest.raises(ValidationError):
        TimeFilter(start_date=far_future_start, end_date=near_future_end)


# ── Day-inclusive end ────────────────────────────────────────────────────────

def test_end_date_inclusive_adds_one_day():
    tf = TimeFilter(end_date=date(2026, 1, 31), label="by Jan 31")
    assert tf.end_date_inclusive() == date(2026, 2, 1)


def test_end_date_inclusive_none_when_no_end_date():
    tf = TimeFilter(relative_days=7)
    assert tf.end_date_inclusive() is None


# ── OTel attribute shape ─────────────────────────────────────────────────────

def test_otel_attrs_contains_every_spec_key():
    tf = TimeFilter(start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
                    label="January", boundary="updated_at")
    attrs = tf.otel_attrs()
    assert set(attrs.keys()) == {
        "time_filter.relative_days",
        "time_filter.start_date",
        "time_filter.end_date",
        "time_filter.label",
        "time_filter.boundary",
    }
    assert attrs["time_filter.start_date"] == "2026-01-01"
    assert attrs["time_filter.end_date"] == "2026-01-31"
    assert attrs["time_filter.boundary"] == "updated_at"


def test_otel_attrs_prefix_overridable():
    tf = TimeFilter(relative_days=7)
    attrs = tf.otel_attrs(prefix="tf")
    assert "tf.relative_days" in attrs


def test_otel_attrs_nulls_present_when_empty():
    """Span attributes should still surface null fields, so a search like
    `time_filter.start_date != null` works."""
    attrs = TimeFilter().otel_attrs()
    assert attrs["time_filter.relative_days"] is None
    assert attrs["time_filter.start_date"] is None
