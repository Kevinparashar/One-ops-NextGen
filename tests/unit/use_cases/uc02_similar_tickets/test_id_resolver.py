"""UC-2 id_resolver — boundary edge cases (spec §UC-2 + button edge-case table)."""
from __future__ import annotations

import pytest

from oneops.use_cases.uc02_similar_tickets.id_resolver import (
    ResolveError,
    resolve,
)

# ── Happy paths ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("raw", "hint", "expected_id", "expected_svc"), [
    ("INC0001234", None, "INC0001234", "incident"),
    ("REQ0001234", None, "REQ0001234", "request"),
    ("inc0001234", None, "INC0001234", "incident"),        # case-insensitive
    ("  REQ0001234  ", None, "REQ0001234", "request"),     # padding
    ("INC-0001-234", None, "INC0001234", "incident"),      # separators
    ("(inc_0001234)", None, "INC0001234", "incident"),     # punctuation
    ("0001234", "incident", "INC0001234", "incident"),     # bare + hint
    ("123", "request", "REQ0000123", "request"),           # zero-pads short
    ("INC0001234", "incident", "INC0001234", "incident"),  # hint matches prefix
    ("REQ0001234", "request", "REQ0001234", "request"),
])
def test_resolve_accepts_canonical_inputs(raw, hint, expected_id, expected_svc):
    r = resolve(raw, hint)
    assert r.entity_id == expected_id
    assert r.service_id == expected_svc


# ── Rejections (button edge cases #1, #3, #5, #6, #8) ─────────────────────────

@pytest.mark.parametrize(("raw", "hint", "needle"), [
    ("",                None, "required"),
    ("   ",             None, "required"),
    (None,              None, "required"),
    ("0001234",         None, "ambiguous"),
    ("123",             None, "ambiguous"),
    ("PBM0001234",      None, "UC-2 supports"),           # out-of-scope service
    ("CHG0001234",      None, "UC-2 supports"),
    ("KB0001234",       None, "UC-2 supports"),
    ("INCabc",          None, "not a number"),
    ("INC",             None, "no number after it"),
    ("foobar",          None, "does not begin with"),
    ("INC0001234",      "request", "contradicts"),         # hint vs prefix clash
    ("REQ0001234",      "incident", "contradicts"),
    ("INC0001234",      "problem", "service_id must be"), # bad hint enum
])
def test_resolve_rejects(raw, hint, needle):
    with pytest.raises(ResolveError) as exc:
        resolve(raw, hint)
    assert needle.lower() in str(exc.value).lower()
