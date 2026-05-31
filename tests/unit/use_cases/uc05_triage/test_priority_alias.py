"""Unit tests for the priority alias normaliser (Option A).

Covers the P-prefix -> Motadata canonical mapping with devil's-play:
  • All 4 P-prefix aliases map correctly
  • Already-canonical values pass through unchanged
  • None / empty input -> None
  • Unknown value passes through unchanged (silent identity, read-side only)
  • Case sensitivity ("p3" != "P3" -> not aliased)
  • Whitespace is stripped before alias lookup
  • Round-trip: canonical -> canonical -> canonical (stable)
"""
from __future__ import annotations

import pytest

from oneops.use_cases.uc05_triage.tools.prioritize import normalise_priority


class TestKnownAliases:
    @pytest.mark.parametrize(("alias", "canonical"), [
        ("P1", "Urgent"),
        ("P2", "High"),
        ("P3", "Medium"),
        ("P4", "Low"),
    ])
    def test_each_p_alias_maps(self, alias, canonical) -> None:
        assert normalise_priority(alias) == canonical


class TestCanonicalPassthrough:
    @pytest.mark.parametrize("value", ["Low", "Medium", "High", "Urgent"])
    def test_canonical_unchanged(self, value) -> None:
        assert normalise_priority(value) == value


class TestEdgeCases:
    def test_none_returns_none(self) -> None:
        assert normalise_priority(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert normalise_priority("") is None
        assert normalise_priority("   ") is None

    def test_unknown_value_passthrough(self) -> None:
        # Legacy row with a value we have no alias for → silent identity
        assert normalise_priority("Critical") == "Critical"
        assert normalise_priority("P5") == "P5"

    def test_case_sensitivity_lowercase_not_aliased(self) -> None:
        # 'p3' (lowercase) is NOT 'P3' — Motadata vocab is case-sensitive
        assert normalise_priority("p3") == "p3"
        assert normalise_priority("p1") == "p1"

    def test_whitespace_stripped(self) -> None:
        assert normalise_priority("  P2  ") == "High"
        assert normalise_priority("\tP3\n") == "Medium"

    def test_round_trip_stability(self) -> None:
        """normalise(normalise(x)) == normalise(x) for any input."""
        for v in ["P1", "P2", "P3", "P4", "Low", "Medium", "High", "Urgent",
                  "Critical", None, ""]:
            once = normalise_priority(v)
            twice = normalise_priority(once)
            assert once == twice, f"unstable for {v!r}: {once!r} != {twice!r}"
