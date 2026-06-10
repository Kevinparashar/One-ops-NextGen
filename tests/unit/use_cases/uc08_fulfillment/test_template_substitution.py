"""C-2 (P1-6) — catalog-template substitution is depth-bounded.

`_substitute_input_template` walks an admin-authored catalog template recursively.
A pathologically deep template must raise a clear `InvalidTemplateError` (config
bug → 422) instead of an ungraceful RecursionError — while every real (shallow)
template substitutes exactly as before. Pure-function test, no DB (hermetic unit).
See docs/history/change-log.md Batch C-2.
"""
from __future__ import annotations

import pytest

from oneops.use_cases.uc08_fulfillment.core import (
    _MAX_TEMPLATE_DEPTH,
    _substitute_input_template,
)
from oneops.use_cases.uc08_fulfillment.errors import InvalidTemplateError

# ── behavior preserved: real templates substitute exactly as before ──────────


def test_empty_template_returns_empty():
    assert _substitute_input_template(None, {"a": "1"}) == {}
    assert _substitute_input_template({}, {"a": "1"}) == {}


def test_leaf_string_substitution():
    out = _substitute_input_template(
        {"user": "{username}", "lit": "static"}, {"username": "kevin"})
    assert out == {"user": "kevin", "lit": "static"}


def test_nested_dict_and_list_substitution():
    tpl = {"a": {"b": "{x}"}, "c": ["{x}", "lit", {"d": "{x}"}]}
    out = _substitute_input_template(tpl, {"x": "V"})
    assert out == {"a": {"b": "V"}, "c": ["V", "lit", {"d": "V"}]}


def test_missing_variable_leaves_placeholder():
    out = _substitute_input_template({"k": "{absent}"}, {"x": "1"})
    assert out == {"k": "{absent}"}


def test_non_string_values_pass_through():
    out = _substitute_input_template({"n": 5, "b": True, "f": 1.5}, {})
    assert out == {"n": 5, "b": True, "f": 1.5}


# ── depth bound: deep template → clean typed error, not RecursionError ───────


def _nest(depth: int) -> dict:
    node: dict = {"leaf": "{x}"}
    for _ in range(depth):
        node = {"n": node}
    return node


def test_at_max_depth_is_allowed():
    # A template exactly at the limit still substitutes (no false positive).
    tpl = _nest(_MAX_TEMPLATE_DEPTH - 1)
    out = _substitute_input_template(tpl, {"x": "V"})
    # innermost leaf was substituted
    cur = out
    while isinstance(cur, dict) and "n" in cur:
        cur = cur["n"]
    assert cur["leaf"] == "V"


def test_beyond_max_depth_raises_invalid_template():
    tpl = _nest(_MAX_TEMPLATE_DEPTH + 5)
    with pytest.raises(InvalidTemplateError, match="nested beyond max depth"):
        _substitute_input_template(tpl, {"x": "V"})
