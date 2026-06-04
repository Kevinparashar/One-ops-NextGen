"""C-1 (P1-5/P1-6) — fast-path `inputs` is bounded against pathological payloads.

Devil's-advocate: a fast-path caller could flood `inputs` with a huge / deeply
nested dict, reaching downstream validators, SQL, and embeddings. The bound must
reject those with a clean 422-class ValidationError while NEVER rejecting a real
input (a handful of short scalar fields). See docs/change-log.md Batch C.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from oneops.api.app import (
    _MAX_FAST_PATH_INPUT_BYTES,
    _MAX_FAST_PATH_INPUT_DEPTH,
    _MAX_FAST_PATH_INPUT_KEYS,
    FastPathPostRequest,
    _nesting_depth,
)

# ── real inputs are unaffected (no behavior change) ──────────────────────────


@pytest.mark.parametrize("inputs", [
    {},
    {"ticket_id": "INC0001001"},
    {"article_id": "KB0001000"},
    {"ticket_id": "INC0001001", "service_id": "incident", "scope": "open"},
])
def test_real_fast_path_inputs_pass(inputs):
    req = FastPathPostRequest(inputs=inputs)
    assert req.inputs == inputs


def test_default_inputs_is_empty_dict():
    assert FastPathPostRequest().inputs == {}


# ── _nesting_depth helper ────────────────────────────────────────────────────


@pytest.mark.parametrize(("value", "depth"), [
    ("scalar", 0),
    ({"a": 1}, 1),
    ({"a": {"b": 1}}, 2),
    ({"a": {"b": {"c": 1}}}, 3),
    ({"a": [1, 2, {"b": 1}]}, 3),
    ({}, 1),
])
def test_nesting_depth(value, depth):
    assert _nesting_depth(value) == depth


# ── bounds: too many keys / too deep / too large → ValidationError ───────────


def test_too_many_keys_rejected():
    payload = {f"k{i}": i for i in range(_MAX_FAST_PATH_INPUT_KEYS + 1)}
    with pytest.raises(ValidationError, match="too many keys"):
        FastPathPostRequest(inputs=payload)


def test_at_key_limit_is_allowed():
    payload = {f"k{i}": i for i in range(_MAX_FAST_PATH_INPUT_KEYS)}
    assert FastPathPostRequest(inputs=payload).inputs == payload


def test_too_deeply_nested_rejected():
    # Build a dict nested one level deeper than allowed.
    node: dict = {"leaf": 1}
    for _ in range(_MAX_FAST_PATH_INPUT_DEPTH + 1):
        node = {"n": node}
    with pytest.raises(ValidationError, match="nested too deeply"):
        FastPathPostRequest(inputs=node)


def test_too_large_rejected():
    # One key, shallow — but a value over the byte cap (exercises the size path,
    # which the key/depth checks do not catch).
    big = "x" * (_MAX_FAST_PATH_INPUT_BYTES + 1)
    with pytest.raises(ValidationError, match="too large"):
        FastPathPostRequest(inputs={"blob": big})
