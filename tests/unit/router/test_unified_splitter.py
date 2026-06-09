"""LlmUnifiedSplitter — the merged decompose+rewrite call (latency, RCA
2026-06-09). ONE gateway call resolves references AND splits into atomic
sub-queries, returning self-contained text so the router's per-sub-query
rewrite becomes a passthrough.

These tests assert the splitter's CONTRACT (parse, fallback, history wiring,
flag default), not the model's judgment — the LLM is faked, as in the sibling
decompose tests.
"""
from __future__ import annotations

from oneops.router.decompose import (
    LlmUnifiedSplitter,
    SubQuery,
    merge_decompose_rewrite_enabled,
)
from oneops.router.rewrite import ConversationTurn

_SINGLE = (
    '{"reasoning":"1 entity, 1 ask — refs resolved",'
    '"subqueries":[{"id":"sq1","text":"what is the priority of INC0001001",'
    '"depends_on":[]}]}'
)
_MULTI = (
    '{"reasoning":"2 independent asks; sq2 resolves it",'
    '"subqueries":['
    '{"id":"sq1","text":"summarize INC0001001","depends_on":[]},'
    '{"id":"sq2","text":"any docs for INC0001001","depends_on":["sq1"]}'
    ']}'
)
_CTX = {"tenant_id": "t", "user_id": "u", "request_id": "r"}


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content


class _CapturingGateway:
    """Fake gateway that records the messages it was called with so we can
    assert the conversation context + focus reached the prompt."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.last_messages = None

    async def call(self, req):
        self.last_messages = req.messages
        return _FakeResp(self._content)


# ── flag default ────────────────────────────────────────────────────────


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("ONEOPS_ROUTER_MERGE_DECOMPOSE_REWRITE", raising=False)
    assert merge_decompose_rewrite_enabled() is False


def test_flag_on_when_set(monkeypatch):
    monkeypatch.setenv("ONEOPS_ROUTER_MERGE_DECOMPOSE_REWRITE", "1")
    assert merge_decompose_rewrite_enabled() is True


# ── parse contract ──────────────────────────────────────────────────────


async def test_splits_single_resolved_subquery():
    gw = _CapturingGateway(_SINGLE)
    splitter = LlmUnifiedSplitter(gw)
    subs = await splitter.split("what is the priority of it",
                                history=[], request_ctx=_CTX)
    assert len(subs) == 1
    # text is the reference-RESOLVED form (rewrite job done in the same call)
    assert subs[0].text == "what is the priority of INC0001001"


async def test_splits_multi_subquery():
    gw = _CapturingGateway(_MULTI)
    splitter = LlmUnifiedSplitter(gw)
    subs = await splitter.split("summarize INC0001001 and any docs for it",
                                history=[], request_ctx=_CTX)
    assert [s.id for s in subs] == ["sq1", "sq2"]
    assert subs[1].depends_on == ("sq1",)


# ── history + focus reach the prompt (the rewrite job's inputs) ──────────


async def test_history_and_focus_reach_user_block():
    gw = _CapturingGateway(_SINGLE)
    splitter = LlmUnifiedSplitter(gw)
    history = [ConversationTurn(role="user", content="summarize INC0001001")]
    ctx = {**_CTX, "focus_entity_id": "INC0001001", "focus_service_id": "incident"}
    await splitter.split("what is the priority", history=history, request_ctx=ctx)
    user_msg = gw.last_messages[-1].content
    assert "summarize INC0001001" in user_msg          # conversation context
    assert "CURRENT FOCUS" in user_msg                  # authoritative focus block
    assert "INC0001001" in user_msg


# ── resilience: a fault never drops the message ─────────────────────────


async def test_llm_failure_falls_back_to_passthrough():
    from oneops.errors import LLMGatewayError

    class _BoomGateway:
        async def call(self, _req):
            raise LLMGatewayError("gateway down")

    splitter = LlmUnifiedSplitter(_BoomGateway())
    subs = await splitter.split("summarize INC0001009",
                                history=[], request_ctx=_CTX)
    assert subs == [SubQuery(id="sq1", text="summarize INC0001009")]


async def test_unparseable_json_falls_back():
    splitter = LlmUnifiedSplitter(_CapturingGateway("not json at all"))
    subs = await splitter.split("summarize INC0001009",
                                history=[], request_ctx=_CTX)
    assert subs[0].text == "summarize INC0001009"


async def test_empty_subqueries_falls_back():
    splitter = LlmUnifiedSplitter(
        _CapturingGateway('{"reasoning":"x","subqueries":[]}'))
    subs = await splitter.split("summarize INC0001009",
                                history=[], request_ctx=_CTX)
    assert subs[0].text == "summarize INC0001009"
