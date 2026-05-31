"""Phase 2 tests — W3C traceparent helper."""
from __future__ import annotations

import pytest

from oneops.use_cases.uc05_triage.traceparent import (
    extract_from_headers,
    parse_traceparent,
)


class TestParseHappy:
    def test_sampled(self) -> None:
        tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        out = parse_traceparent(tp)
        assert out is not None
        trace_id, span_id, flags = out
        assert trace_id == "0af7651916cd43dd8448eb211c80319c"
        assert span_id == "b7ad6b7169203331"
        assert flags == 1

    def test_not_sampled(self) -> None:
        tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00"
        out = parse_traceparent(tp)
        assert out is not None
        assert out[2] == 0


class TestParseDevilsPlay:
    @pytest.mark.parametrize("bad", [
        None, "",
        "00-trace-span",
        "00-aaa-bbb-cc-extra",
        "01-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "00-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1-b7ad6b7169203331-01",
        "00-0af7651916cd43dd8448eb211c80319-b7ad6b7169203331-01",
        "00-0af7651916cd43dd8448eb211c80319c-b7ad6b716920333-01",
        "00-00000000000000000000000000000000-b7ad6b7169203331-01",
        "00-0af7651916cd43dd8448eb211c80319c-0000000000000000-01",
        "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-gg",
        "00-0af7651916cd43dd8448eb211c8031zz-b7ad6b7169203331-01",
        42,
    ])
    def test_each_bad_returns_none(self, bad) -> None:
        assert parse_traceparent(bad) is None

    def test_whitespace_stripped(self) -> None:
        tp = "  00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01  "
        assert parse_traceparent(tp) is not None


class TestExtractFromHeaders:
    def test_returns_value(self) -> None:
        headers = {"traceparent":
                   "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
        assert extract_from_headers(headers) is not None

    def test_missing_returns_none(self) -> None:
        assert extract_from_headers({}) is None
        assert extract_from_headers({"x-other": "y"}) is None

    def test_none_headers(self) -> None:
        assert extract_from_headers(None) is None

    def test_list_value_first_item(self) -> None:
        headers = {"traceparent":
                   ["00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"]}
        assert extract_from_headers(headers) is not None

    def test_empty_list_returns_none(self) -> None:
        assert extract_from_headers({"traceparent": []}) is None
