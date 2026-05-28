"""Tests for the deterministic retriever, decomposer, and rewriter."""
from __future__ import annotations

import pytest

from oneops.router.decompose import PassthroughDecomposer, SubQuery
from oneops.router.retrieval import LexicalRetriever
from oneops.router.rewrite import ConversationTurn, PassthroughRewriter

from ._factories import make_agent, make_registry

# pyproject sets `asyncio_mode = auto` — async tests are collected without an
# explicit mark, so this file can mix async and sync tests cleanly.


# ── LexicalRetriever ─────────────────────────────────────────────────────


async def test_retriever_ranks_by_token_overlap(tmp_path):
    reg = make_registry(tmp_path, [
        make_agent("uc_summary", description="summarize an incident ticket record"),
        make_agent("uc_kb", description="search the knowledge base for articles"),
    ])
    retriever = LexicalRetriever(reg)
    out = await retriever.retrieve("summarize this incident", tenant_id="t", top_k=5)
    assert out[0].agent_id == "uc_summary"           # best overlap first


async def test_retriever_returns_empty_for_empty_query(tmp_path):
    reg = make_registry(tmp_path, [make_agent("uc_a")])
    assert await LexicalRetriever(reg).retrieve("", tenant_id="t", top_k=5) == []


async def test_retriever_returns_empty_for_only_stopwords(tmp_path):
    reg = make_registry(tmp_path, [make_agent("uc_a")])
    out = await LexicalRetriever(reg).retrieve("what is the", tenant_id="t", top_k=5)
    assert out == []


async def test_retriever_honours_top_k(tmp_path):
    reg = make_registry(tmp_path, [
        make_agent(f"uc_{i}", description="summarize incident ticket")
        for i in range(10)
    ])
    out = await LexicalRetriever(reg).retrieve("summarize incident", tenant_id="t", top_k=3)
    assert len(out) == 3


async def test_retriever_skips_agents_with_no_overlap(tmp_path):
    reg = make_registry(tmp_path, [
        make_agent("uc_match", description="summarize incident records"),
        make_agent("uc_miss", description="provision cloud infrastructure servers"),
    ])
    out = await LexicalRetriever(reg).retrieve("summarize incident", tenant_id="t", top_k=5)
    assert {c.agent_id for c in out} == {"uc_match"}


# ── PassthroughDecomposer ────────────────────────────────────────────────


async def test_passthrough_decomposer_yields_one_subquery():
    out = await PassthroughDecomposer().decompose("summarize INC1 and find KB",
                                                  request_ctx={})
    assert len(out) == 1
    assert out[0].text == "summarize INC1 and find KB"
    assert out[0].depends_on == ()


def test_subquery_rejects_self_dependency():
    with pytest.raises(ValueError, match="cannot depend on itself"):
        SubQuery(id="sq1", text="x", depends_on=("sq1",))


# ── PassthroughRewriter ──────────────────────────────────────────────────


async def test_passthrough_rewriter_leaves_text_unchanged():
    history = [ConversationTurn(role="user", content="summarize INC0048213")]
    result = await PassthroughRewriter().rewrite("close it", history=history,
                                                 request_ctx={})
    assert result.text == "close it"
    assert result.changed is False
