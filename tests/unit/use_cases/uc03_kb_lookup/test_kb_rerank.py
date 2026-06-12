"""UC-3 listwise reranker — parsing, gateway wrapper, and the handler's
rerank-then-abstain precision stage (with degraded fallback).

Covers: closed-scale parsing + alignment (C8), no silent failure / graceful
degrade on a reranker outage (C17), and that a reranker verdict — not raw
cosine — decides the final ORDER and the abstain decision.
"""
from __future__ import annotations

import pytest

from oneops.use_cases._shared.kb_store import InMemoryKbStore, set_kb_store
from oneops.use_cases.uc03_kb_lookup import kb_rerank as _kr
from oneops.use_cases.uc03_kb_lookup.handlers import search_kb
from oneops.use_cases.uc03_kb_lookup.kb_rerank import (
    LlmListwiseReranker,
    RerankResult,
    _parse_rankings,
    set_kb_reranker,
)


class _FakeRerankCache:
    """In-memory stand-in for the Dragonfly result cache (dict-backed)."""

    def __init__(self) -> None:
        self.store: dict = {}

    async def get(self, key, *, tenant_id):  # noqa: ANN001
        v = self.store.get(key)
        return list(v) if v is not None else None

    async def put(self, key, results):  # noqa: ANN001
        self.store[key] = list(results)


@pytest.fixture(autouse=True)
def _isolate_rerank_cache(monkeypatch):
    """Every test gets a FRESH in-memory rerank cache — never the shared
    Dragonfly (which would persist results across tests and across runs, making
    `gw.calls` assertions order-dependent / flaky)."""
    monkeypatch.setattr(_kr, "_cache", _FakeRerankCache())


# ── _parse_rankings ────────────────────────────────────────────────────────

_ARTS = [{"kb_id": "KB1"}, {"kb_id": "KB2"}, {"kb_id": "KB3"}]


def test_parse_rankings_aligns_to_input_and_normalises():
    out = _parse_rankings(
        '{"rankings":[{"id":"KB1","relevance":3},'
        '{"id":"KB2","relevance":0},{"id":"KB3","relevance":2}]}', _ARTS)
    assert out is not None
    by = {r.kb_id: r for r in out}
    assert by["KB1"].relevance == 1.0 and by["KB1"].raw_label == 3
    assert by["KB2"].relevance == 0.0
    assert round(by["KB3"].relevance, 3) == 0.667
    # result list is aligned 1:1 with the input articles
    assert [r.kb_id for r in out] == ["KB1", "KB2", "KB3"]


def test_parse_rankings_missing_candidate_defaults_to_zero():
    out = _parse_rankings('{"rankings":[{"id":"KB1","relevance":3}]}', _ARTS)
    assert out is not None
    assert {r.kb_id: r.raw_label for r in out} == {"KB1": 3, "KB2": 0, "KB3": 0}


def test_parse_rankings_strips_code_fence():
    out = _parse_rankings(
        '```json\n{"rankings":[{"id":"KB1","relevance":2}]}\n```', _ARTS)
    assert out is not None and out[0].raw_label == 2


def test_parse_rankings_clamps_out_of_range_label():
    out = _parse_rankings('{"rankings":[{"id":"KB1","relevance":9}]}', _ARTS)
    assert out is not None and out[0].raw_label == 3  # clamped to max


def test_parse_rankings_unparseable_returns_none():
    assert _parse_rankings("not json at all", _ARTS) is None
    assert _parse_rankings('{"wrong":"shape"}', _ARTS) is None


# ── LlmListwiseReranker (stub gateway) ─────────────────────────────────────

class _StubResp:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubGateway:
    def __init__(self, content: str = "", *, raise_exc: bool = False) -> None:
        self._content = content
        self._raise = raise_exc
        self.calls = 0

    async def call(self, request):  # noqa: ANN001
        self.calls += 1
        if self._raise:
            from oneops.errors import LLMGatewayError
            raise LLMGatewayError("boom")
        return _StubResp(self._content)


async def test_reranker_returns_aligned_results():
    gw = _StubGateway('{"rankings":[{"id":"KB2","relevance":3},'
                      '{"id":"KB1","relevance":1}]}')
    rr = LlmListwiseReranker(gw, model="gpt-4o")
    out = await rr.rerank(query="reset password",
                          articles=[{"kb_id": "KB1", "title": "a"},
                                    {"kb_id": "KB2", "title": "b"}],
                          tenant_id="T1")
    assert out is not None and gw.calls == 1
    by = {r.kb_id: r.raw_label for r in out}
    assert by == {"KB1": 1, "KB2": 3}


async def test_reranker_gateway_failure_returns_none():
    rr = LlmListwiseReranker(_StubGateway(raise_exc=True), model="gpt-4o")
    out = await rr.rerank(query="x", articles=[{"kb_id": "KB1"}], tenant_id="T1")
    assert out is None  # caller falls back to cosine order — never raises


async def test_reranker_empty_inputs_short_circuit():
    rr = LlmListwiseReranker(_StubGateway('{"rankings":[]}'), model="gpt-4o")
    assert await rr.rerank(query="", articles=[{"kb_id": "KB1"}],
                           tenant_id="T1") is None
    assert await rr.rerank(query="x", articles=[], tenant_id="T1") is None


# ── cross-session result cache (token-saver) ───────────────────────────────


async def test_rerank_cache_hit_skips_second_llm_call():
    gw = _StubGateway('{"rankings":[{"id":"KB1","relevance":3}]}')
    rr = LlmListwiseReranker(gw, model="gpt-4o-mini")
    arts = [{"kb_id": "KB1", "title": "t", "content": "body"}]
    r1 = await rr.rerank(query="reset password", articles=arts, tenant_id="T1")
    r2 = await rr.rerank(query="reset password", articles=arts, tenant_id="T1")
    assert gw.calls == 1                       # 2nd turn served from cache
    assert [x.kb_id for x in r2] == [x.kb_id for x in r1] == ["KB1"]
    assert r2[0].raw_label == 3


async def test_rerank_cache_invalidates_on_content_change():
    """Edited article content → new fingerprint → new key → NOT a stale hit."""
    gw = _StubGateway('{"rankings":[{"id":"KB1","relevance":3}]}')
    rr = LlmListwiseReranker(gw, model="gpt-4o-mini")
    await rr.rerank(query="q", articles=[{"kb_id": "KB1", "content": "v1"}],
                    tenant_id="T1")
    await rr.rerank(query="q", articles=[{"kb_id": "KB1", "content": "v2"}],
                    tenant_id="T1")
    assert gw.calls == 2                        # content changed → re-ranked


async def test_rerank_cache_is_tenant_scoped():
    gw = _StubGateway('{"rankings":[{"id":"KB1","relevance":3}]}')
    rr = LlmListwiseReranker(gw, model="gpt-4o-mini")
    arts = [{"kb_id": "KB1", "content": "body"}]
    await rr.rerank(query="q", articles=arts, tenant_id="T1")
    await rr.rerank(query="q", articles=arts, tenant_id="T2")
    assert gw.calls == 2                        # different tenant → no shared read


# ── handler integration: rerank decides ORDER + ABSTAIN ────────────────────

class _DictReranker:
    """Deterministic stub: scores each article by a kb_id→label map."""

    def __init__(self, labels: dict[str, int]) -> None:
        self._labels = labels

    async def rerank(self, *, query, articles, tenant_id,  # noqa: ANN001
                     user_id="", request_id=""):
        return [RerankResult(kb_id=a.get("kb_id", ""),
                             relevance=self._labels.get(a.get("kb_id", ""), 0)
                             / 3.0,
                             raw_label=self._labels.get(a.get("kb_id", ""), 0))
                for a in articles]


@pytest.fixture
def store() -> InMemoryKbStore:
    from oneops.use_cases.uc03_kb_lookup.kb_embed import (
        set_kb_embed_fn,
        set_kb_relevance_scorer,
    )
    set_kb_embed_fn(None)
    set_kb_relevance_scorer(None)
    set_kb_reranker(None)
    s = InMemoryKbStore()
    s.seed(kb_id="KB_PWD", tenant_id="T1", title="Password reset procedure",
           summary="reset your password", content="login password reset steps",
           tags=["login"], state="published", audience="all", helpful_votes=5)
    s.seed(kb_id="KB_SEC", tenant_id="T1", title="Detect a suspicious login",
           summary="security runbook", content="login attack detection",
           tags=["login"], state="published", audience="all", helpful_votes=99)
    set_kb_store(s)
    yield s
    set_kb_reranker(None)


async def test_rerank_decides_order_over_keyword_rank(store):
    # KB_SEC has far more helpful_votes (would win keyword ordering), but the
    # reranker judges KB_PWD the better answer → it must come FIRST.
    set_kb_reranker(_DictReranker({"KB_PWD": 3, "KB_SEC": 1}))
    out = await search_kb({"query": "having login issues"},
                          {"tenant_id": "T1", "role": "employee"})
    assert out["outcome"] == "found"
    assert [a["kb_id"] for a in out["articles"]] == ["KB_PWD"]
    assert out["articles"][0]["relevance_score"] == 100


async def test_rerank_abstains_when_all_below_floor(store):
    # Every candidate scored label 1 (loosely related) → below the 0.5 floor →
    # genuine no_match even though keyword search returned hits.
    set_kb_reranker(_DictReranker({"KB_PWD": 1, "KB_SEC": 1}))
    out = await search_kb({"query": "login"},
                          {"tenant_id": "T1", "role": "employee"})
    assert out["outcome"] == "no_match"
    assert out["articles"] == []


async def test_no_reranker_falls_back_to_keyword_gate(store):
    # No reranker wired → degraded path still returns the keyword hits (never
    # silently empty just because the precision stage is absent).
    set_kb_reranker(None)
    out = await search_kb({"query": "password reset"},
                          {"tenant_id": "T1", "role": "employee"})
    assert out["outcome"] == "found"
    assert "KB_PWD" in {a["kb_id"] for a in out["articles"]}
