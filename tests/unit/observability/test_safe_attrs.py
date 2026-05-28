"""Guarantee tests for the PII-scrub helpers.

Promises tested:
1. Hash is deterministic + 16 hex chars
2. Length matches char count; None/empty → 0
3. Flag OFF: <prefix>_text is NEVER set on the span
4. Flag ON: <prefix>_text IS set and is bounded to 4 KiB
5. Helpers NEVER raise — bad spans, bad text, missing attrs all swallowed
"""
from __future__ import annotations

import pytest

from oneops.observability.safe_attrs import (
    capture_text_enabled,
    safe_hash_text,
    safe_json_attr,
    safe_list_attr,
    safe_text_len,
    set_safe_text_attrs,
)


class FakeSpan:
    def __init__(self) -> None:
        self.attrs: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attrs[key] = value


# ── hash ───────────────────────────────────────────────────────────
def test_hash_is_deterministic_16_hex_chars() -> None:
    h1 = safe_hash_text("hello world")
    h2 = safe_hash_text("hello world")
    assert h1 == h2
    assert len(h1) == 16
    assert all(c in "0123456789abcdef" for c in h1)


def test_hash_empty_or_none_returns_empty() -> None:
    assert safe_hash_text("") == ""
    assert safe_hash_text(None) == ""


# ── length ─────────────────────────────────────────────────────────
def test_len_matches_char_count() -> None:
    assert safe_text_len("hello") == 5
    assert safe_text_len("") == 0
    assert safe_text_len(None) == 0


# ── set_safe_text_attrs: scrub flag ────────────────────────────────
def test_flag_off_never_sets_raw_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_CAPTURE_TEXT", raising=False)
    span = FakeSpan()
    set_safe_text_attrs(span, "user_query", "sensitive ticket body")
    assert "user_query_hash" in span.attrs
    assert "user_query_len" in span.attrs
    assert "user_query_text" not in span.attrs
    assert span.attrs["user_query_len"] == len("sensitive ticket body")


def test_flag_on_sets_bounded_raw_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_CAPTURE_TEXT", "true")
    span = FakeSpan()
    payload = "x" * 8192
    set_safe_text_attrs(span, "kb", payload)
    assert "kb_text" in span.attrs
    assert len(span.attrs["kb_text"]) == 4096  # bounded
    assert span.attrs["kb_len"] == 8192


def test_flag_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_CAPTURE_TEXT", "TRUE")
    assert capture_text_enabled() is True
    monkeypatch.setenv("OTEL_CAPTURE_TEXT", "False")
    assert capture_text_enabled() is False


# ── never-raises invariant ─────────────────────────────────────────
def test_no_span_object_silent() -> None:
    set_safe_text_attrs(None, "x", "hello")  # must not raise


def test_span_without_set_attribute_silent() -> None:
    class Bad:
        pass

    set_safe_text_attrs(Bad(), "x", "hello")  # must not raise


def test_empty_text_skips_raw_capture_even_with_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_CAPTURE_TEXT", "true")
    span = FakeSpan()
    set_safe_text_attrs(span, "q", "")
    assert "q_text" not in span.attrs
    assert span.attrs["q_len"] == 0


# ── safe_json_attr / safe_list_attr ────────────────────────────────
def test_json_attr_truncates_with_marker() -> None:
    huge = {"k": "v" * 5000}
    s = safe_json_attr(huge, max_len=128)
    assert len(s) <= 128
    assert s.endswith("...[truncated]")


def test_list_attr_caps_items() -> None:
    out = safe_list_attr(range(50), max_items=5)
    assert len(out) == 5
    assert out == ["0", "1", "2", "3", "4"]


def test_list_attr_none_returns_empty() -> None:
    assert safe_list_attr(None) == []
