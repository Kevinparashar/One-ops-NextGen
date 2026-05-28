"""Tests for the four LLM-backed components, driven through the gateway.

Each component runs against `EchoTransport` with a canned JSON completion, so
the test controls exactly what "the LLM returned" and verifies the component's
prompt-build → gateway-call → parse path — including the fallback when the
gateway response is unparseable or the call fails.
"""
from __future__ import annotations

from oneops.executor.boundary import LlmBoundaryResponder
from oneops.llm import EchoTransport, LlmGateway
from oneops.router.decompose import LlmDecomposer
from oneops.router.disambiguation import LlmDisambiguator
from oneops.router.retrieval import Candidate
from oneops.router.rewrite import ConversationTurn, LlmRewriter


def _gateway(canned: str | None = None, *, fail_times: int = 0) -> LlmGateway:
    return LlmGateway(EchoTransport(canned=canned, fail_times=fail_times),
                      max_retries=0)


_CTX = {"tenant_id": "tenant-a", "request_id": "r-1"}


# ── LlmDecomposer ────────────────────────────────────────────────────────


async def test_decomposer_parses_structured_subqueries():
    canned = ('{"subqueries":[{"id":"sq1","text":"summarize INC1","depends_on":[]},'
              '{"id":"sq2","text":"find related KB","depends_on":["sq1"]}]}')
    subs = await LlmDecomposer(_gateway(canned)).decompose(
        "summarize INC1 and find related KB", request_ctx=_CTX)
    assert [s.id for s in subs] == ["sq1", "sq2"]
    assert subs[1].depends_on == ("sq1",)


async def test_decomposer_falls_back_on_unparseable_response():
    subs = await LlmDecomposer(_gateway("not json at all")).decompose(
        "do a thing", request_ctx=_CTX)
    assert len(subs) == 1                            # never drops the message
    assert subs[0].text == "do a thing"


async def test_decomposer_falls_back_when_the_gateway_fails():
    subs = await LlmDecomposer(_gateway(fail_times=99)).decompose(
        "do a thing", request_ctx=_CTX)
    assert len(subs) == 1 and subs[0].text == "do a thing"


# ── LlmRewriter ──────────────────────────────────────────────────────────


async def test_rewriter_applies_a_resolved_rewrite():
    canned = ('{"rewritten":"close INC0048213","changed":true,'
              '"rationale":"resolved it"}')
    history = [ConversationTurn("user", "summarize INC0048213")]
    result = await LlmRewriter(_gateway(canned)).rewrite(
        "close it", history=history, request_ctx=_CTX)
    assert result.text == "close INC0048213"
    assert result.changed is True


async def test_rewriter_passes_through_on_failure():
    result = await LlmRewriter(_gateway(fail_times=99)).rewrite(
        "close it", history=[], request_ctx=_CTX)
    assert result.text == "close it"
    assert result.changed is False


# ── LlmDisambiguator ─────────────────────────────────────────────────────


async def test_disambiguator_selects_an_offered_agent():
    canned = ('{"selected_agent_ids":["uc01_summary"],"intents":["summary"],'
              '"confidence":0.9,"rationale":"clear"}')
    cands = [Candidate("uc01_summary", 0.8), Candidate("uc03_kb", 0.5)]
    out = await LlmDisambiguator(_gateway(canned)).disambiguate(
        "summarize it", cands, request_ctx=_CTX)
    assert out.is_confident_match
    assert out.selected_agent_ids == ("uc01_summary",)
    assert out.intents == ("summary",)


async def test_disambiguator_drops_an_invented_agent_id():
    # The closed-class guard: an agent id the LLM was never offered is rejected.
    canned = ('{"selected_agent_ids":["uc99_hallucinated"],"intents":[],'
              '"confidence":0.9,"rationale":"x"}')
    cands = [Candidate("uc01_summary", 0.8)]
    out = await LlmDisambiguator(_gateway(canned)).disambiguate(
        "do it", cands, request_ctx=_CTX)
    assert not out.is_confident_match                # invented id → no match


async def test_disambiguator_no_match_on_failure():
    out = await LlmDisambiguator(_gateway(fail_times=99)).disambiguate(
        "do it", [Candidate("uc01_summary", 0.8)], request_ctx=_CTX)
    assert not out.is_confident_match


# ── LlmBoundaryResponder ─────────────────────────────────────────────────


async def test_boundary_responder_returns_the_llm_reply():
    text = await LlmBoundaryResponder(_gateway("I can help with tickets and KB.")).respond(
        outcome="no_confident_match", reason="nothing matched",
        request={"tenant_id": "tenant-a", "message": "do my taxes"})
    assert "tickets" in text


async def test_boundary_responder_falls_back_when_gateway_fails():
    text = await LlmBoundaryResponder(_gateway(fail_times=99)).respond(
        outcome="policy_denied", reason="denied",
        request={"tenant_id": "tenant-a", "message": "x"})
    # Deterministic fallback reply — the user always gets an answer.
    assert "permission" in text
