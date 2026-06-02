"""D4 — planner emits data-flow bindings (flag-gated, default OFF).

The decomposer only parses/asks-for bindings when ONEOPS_PLANNER_EMIT_BINDINGS
is on; with it off the output is byte-identical to before (zero routing risk).
"""
from __future__ import annotations

from oneops.router.decompose import (
    LlmDecomposer,
    SubQuery,
    _parse_bindings,
    _sanitize_subqueries,
)

_DOC_WITH_BINDINGS = (
    '{"reasoning":"two asks; sq2 consumes sq1 root cause",'
    '"subqueries":['
    '{"id":"sq1","text":"summarize INC0001001","depends_on":[]},'
    '{"id":"sq2","text":"find KB for the root cause","depends_on":["sq1"],'
    '"bindings":[{"from":"sq1","from_field":"root_cause","to_param":"query"}]}'
    ']}'
)


class _FakeResp:
    def __init__(self, content):
        self.content = content


class _FakeGateway:
    def __init__(self, content):
        self._content = content

    async def call(self, _req):
        return _FakeResp(self._content)


_CTX = {"tenant_id": "t", "user_id": "u", "request_id": "r"}


# ── flag gating ───────────────────────────────────────────────────────────


async def test_flag_off_drops_bindings(monkeypatch):
    monkeypatch.delenv("ONEOPS_PLANNER_EMIT_BINDINGS", raising=False)
    dec = LlmDecomposer(_FakeGateway(_DOC_WITH_BINDINGS))
    subs = await dec.decompose("summarize INC0001001 and find kb for root cause",
                               request_ctx=_CTX)
    sq2 = next(s for s in subs if s.id == "sq2")
    assert sq2.bindings == ()                 # parsed away when flag off


async def test_flag_on_parses_bindings(monkeypatch):
    monkeypatch.setenv("ONEOPS_PLANNER_EMIT_BINDINGS", "1")
    dec = LlmDecomposer(_FakeGateway(_DOC_WITH_BINDINGS))
    subs = await dec.decompose("summarize INC0001001 and find kb for root cause",
                               request_ctx=_CTX)
    sq2 = next(s for s in subs if s.id == "sq2")
    assert sq2.bindings == (("sq1", "root_cause", "query"),)


# ── _parse_bindings (defensive against untrusted LLM output) ───────────────


def test_parse_bindings_drops_malformed_and_self_ref():
    s = {"id": "sq2", "bindings": [
        {"from": "sq1", "from_field": "x", "to_param": "y"},   # ok
        {"from": "sq2", "from_field": "x", "to_param": "y"},   # self-ref → drop
        {"from": "sq1", "from_field": "", "to_param": "y"},    # empty field → drop
        {"from": "sq1", "to_param": "y"},                      # missing field → drop
        "not-a-dict",                                          # junk → drop
    ]}
    assert _parse_bindings(s) == (("sq1", "x", "y"),)


# ── sanitizer remaps binding source ids after re-id ────────────────────────


def test_sanitizer_remaps_binding_source_ids():
    subs = [
        SubQuery(id="sqA", text="summarize INC1"),
        SubQuery(id="sqB", text="kb for root cause", depends_on=("sqA",),
                 bindings=(("sqA", "root_cause", "query"),)),
    ]
    out = _sanitize_subqueries(subs)
    # ids are re-issued sq1..sqN; the binding's source must track the remap.
    by_text = {s.text: s for s in out}
    b = by_text["kb for root cause"]
    a = by_text["summarize INC1"]
    assert b.bindings == ((a.id, "root_cause", "query"),)


def test_sanitizer_drops_binding_to_deduped_away_source():
    # Two identical "summarize INC1" → one is deduped; a binding to the dropped
    # id must not survive dangling.
    subs = [
        SubQuery(id="sq1", text="summarize INC1"),
        SubQuery(id="sq2", text="summarize INC1"),            # dup → removed
        SubQuery(id="sq3", text="kb", depends_on=("sq2",),
                 bindings=(("sq2", "f", "p"),)),
    ]
    out = _sanitize_subqueries(subs)
    kb = next(s for s in out if s.text == "kb")
    # sq2 was deduped into sq1's slot; the binding source is remapped or dropped,
    # never left pointing at a vanished id.
    valid_ids = {s.id for s in out}
    for fr, _f, _p in kb.bindings:
        assert fr in valid_ids
