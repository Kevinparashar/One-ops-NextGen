"""Unit tests for Fix 2 — near-identical title dedup in retrieval engine."""
from __future__ import annotations

import pytest

from oneops.use_cases.uc05_triage.retrieval.similarity_search import (
    _dedup_by_title,
    _normalise_title_for_dedup,
)


class TestNormaliseTitle:
    """Devil's-play for the no-regex normaliser (locked 2026-05-29)."""

    @pytest.mark.parametrize(("inp", "expected"), [
        ("VPN drops (123)",                              "vpn drops"),
        ("Office Wi-Fi unreachable on one floor (9010054)",
         "office wi-fi unreachable on one floor"),
        ("VPN drops",                                    "vpn drops"),
        ("VPN drops (abc)",                              "vpn drops (abc)"),   # NOT digits → kept
        ("VPN drops ()",                                 "vpn drops ()"),       # empty parens → kept
        ("VPN drops (123) extra",                        "vpn drops (123) extra"),  # paren not at end
        ("  VPN drops (42)  ",                           "vpn drops"),
        ("(999)",                                        "(999)"),              # whole title — open_idx=0 → not stripped
        ("",                                             ""),
        ("Mailbox (42)(43)",                             "mailbox (42)"),       # only last paren-group stripped
    ])
    def test_each_case(self, inp, expected) -> None:
        assert _normalise_title_for_dedup(inp) == expected


def _r(title: str, score: float) -> dict:
    return {"id": title[:10], "title": title, "_rerank_score": score,
            "_fused_score": score}


class TestDedup:
    def test_keeps_top_when_titles_match(self) -> None:
        rows = [
            _r("Office Wi-Fi unreachable on one floor (9010054)", 0.55),
            _r("Office Wi-Fi unreachable on one floor (9010001)", 0.53),
        ]
        out = _dedup_by_title(rows)
        # Both have the same first-60-char prefix → 1 survives
        assert len(out) == 1
        assert out[0]["_rerank_score"] == 0.55

    def test_distinct_titles_all_pass(self) -> None:
        rows = [
            _r("VPN drops after 10 minutes", 0.57),
            _r("Mailbox access denied intermittently", 0.56),
            _r("Network connectivity drop affecting Building 5", 0.55),
        ]
        out = _dedup_by_title(rows)
        assert len(out) == 3

    def test_case_insensitive(self) -> None:
        rows = [_r("VPN Drops Again", 0.6), _r("vpn drops again", 0.55)]
        out = _dedup_by_title(rows)
        assert len(out) == 1
        assert out[0]["_rerank_score"] == 0.6

    def test_whitespace_normalised(self) -> None:
        rows = [_r("VPN Drops Again", 0.6), _r("  VPN Drops Again  ", 0.55)]
        out = _dedup_by_title(rows)
        assert len(out) == 1

    def test_empty_title_passes_through(self) -> None:
        rows = [_r("", 0.5), _r("Real Title", 0.5)]
        out = _dedup_by_title(rows)
        assert len(out) == 2

    def test_preserves_input_order(self) -> None:
        rows = [
            _r("Aaa first (123)", 0.9),
            _r("Bbb second", 0.8),
            _r("Aaa first (456)", 0.7),  # same after serial strip
            _r("Ccc third", 0.6),
        ]
        out = _dedup_by_title(rows)
        assert [r["title"] for r in out] == [
            "Aaa first (123)", "Bbb second", "Ccc third",
        ]

    def test_trailing_serial_stripped_for_dedup(self) -> None:
        """Stress-test rows like INC9010054 vs INC9010001 share title prefix
        but differ in trailing (NNNNN). They should collapse."""
        rows = [
            _r("Office Wi-Fi unreachable on one floor (9010054)", 0.55),
            _r("Office Wi-Fi unreachable on one floor (9010001)", 0.53),
        ]
        out = _dedup_by_title(rows)
        assert len(out) == 1
        assert out[0]["_rerank_score"] == 0.55
