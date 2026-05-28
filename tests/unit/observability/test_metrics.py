"""Guarantee tests for the metrics layer.

Promises:
1. Instruments are cached by name (same instance returned for same name)
2. increment() never raises on bad inputs (None labels, missing meter)
3. histogram() records observations with optional labels
4. Default no-op meter is used when no exporter configured (no exceptions)
"""
from __future__ import annotations

import pytest

from oneops.observability import metrics as om


@pytest.fixture(autouse=True)
def _reset_instruments() -> None:
    """Clear instrument cache between tests so we exercise both create + reuse."""
    om._reset_for_tests()


def test_counter_cached_by_name() -> None:
    om.increment("test.counter.cached", value=1)
    first = om._counters["test.counter.cached"]
    om.increment("test.counter.cached", value=1)
    second = om._counters["test.counter.cached"]
    assert first is second  # same instrument re-used


def test_histogram_cached_by_name() -> None:
    om.histogram("test.histo.cached", value=1.0)
    first = om._histograms["test.histo.cached"]
    om.histogram("test.histo.cached", value=2.0)
    second = om._histograms["test.histo.cached"]
    assert first is second


def test_increment_filters_none_labels() -> None:
    # Must not raise even if a label value is None
    om.increment("test.with_none", value=1, model="gpt-4", operation=None)


def test_increment_no_labels() -> None:
    om.increment("test.no_labels", value=5)


def test_histogram_records_with_labels() -> None:
    om.histogram("test.histo.with_labels", value=42.5, model="gpt-4", operation="classify")


def test_increment_with_zero_value() -> None:
    om.increment("test.zero", value=0)


def test_histogram_negative_value_swallowed() -> None:
    # OTel histograms reject negative; our wrapper must not propagate the error
    om.histogram("test.histo.negative", value=-1.0)


def test_default_meter_does_not_raise() -> None:
    # Even with no exporter configured, calls should be no-ops
    om.increment("test.default.meter", value=1)
    om.histogram("test.default.meter.histo", value=1.0)
