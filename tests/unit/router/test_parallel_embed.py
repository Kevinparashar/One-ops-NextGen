"""Parallel-embed pre-warm (latency, RCA 2026-06-09).

When ONEOPS_ROUTER_PARALLEL_EMBED is on, the router fires the Stage-2 query
embed CONCURRENTLY with the decompose/split LLM call (both read only the raw
message), so retrieve() hits a warm embedding cache. This is a SCHEDULING
change only — these tests prove it changes NO routing decision, is best-effort
(a prewarm fault never breaks routing), and is fully gated by the flag.
"""
from __future__ import annotations

import pytest

from oneops.authz.decision_cache import InMemoryDecisionCache
from oneops.authz.models import Principal
from oneops.authz.rbac import RbacResolver
from oneops.authz.service import AuthzService
from oneops.router.disambiguation import ThresholdDisambiguator
from oneops.router.glossary import Glossary
from oneops.router.plan import RouteOutcome
from oneops.router.retrieval import Candidate, PgVectorRetriever
from oneops.router.router import Router

from ._factories import intent_cond, make_agent, make_registry

pytestmark = pytest.mark.asyncio

_RBAC = RbacResolver({
    "service_desk_agent": frozenset(
        {"read:all_tickets", "write:ticket", "create:ticket"}),
})


# ── retriever-level: prewarm_embed contract ───────────────────────────────


class _FakeEmbedder:
    def __init__(self, *, has_cache: bool = True) -> None:
        self._cache = object() if has_cache else None
        self.embed_calls: list[tuple[str, str]] = []

    async def embed(self, text: str, *, tenant_id: str) -> list[float]:
        self.embed_calls.append((text, tenant_id))
        return [0.0] * 1536


def _pg(embedder):
    # prewarm_embed only touches the embedder; pool/registry are unused there.
    return PgVectorRetriever(registry=None, embedder=embedder, pool=None)


async def test_prewarm_embed_populates_via_embedder_when_cache_present():
    emb = _FakeEmbedder(has_cache=True)
    await _pg(emb).prewarm_embed("how do I fix vpn", tenant_id="T001")
    assert emb.embed_calls == [("how do I fix vpn", "T001")]


async def test_prewarm_embed_is_noop_without_a_cache():
    # No embedding cache → warming it would be pure waste; skip the embed.
    emb = _FakeEmbedder(has_cache=False)
    await _pg(emb).prewarm_embed("how do I fix vpn", tenant_id="T001")
    assert emb.embed_calls == []


async def test_prewarm_embed_is_noop_on_empty_text():
    emb = _FakeEmbedder(has_cache=True)
    await _pg(emb).prewarm_embed("   ", tenant_id="T001")
    assert emb.embed_calls == []


async def test_prewarm_embed_swallows_embedder_errors():
    class _BoomEmbedder:
        _cache = object()

        async def embed(self, text, *, tenant_id):
            raise RuntimeError("gateway down")

    # best-effort: must not raise (retrieve() would embed normally)
    await _pg(_BoomEmbedder()).prewarm_embed("x", tenant_id="T001")


# ── router-level: flag changes scheduling, NOT the route ──────────────────


class _PrewarmStubRetriever:
    """Stub retriever that records prewarm calls and returns fixed candidates,
    so we can assert (a) prewarm fires only under the flag and (b) the route is
    byte-identical with and without it."""

    def __init__(self, candidates, *, fail_prewarm: bool = False) -> None:
        self._candidates = candidates
        self.prewarm_calls: list[tuple[str, str]] = []
        self._fail = fail_prewarm

    async def retrieve(self, query_text, *, tenant_id, top_k):
        return list(self._candidates)

    async def prewarm_embed(self, query_text, *, tenant_id):
        self.prewarm_calls.append((query_text, tenant_id))
        if self._fail:
            raise RuntimeError("prewarm boom")


def _router(reg, retriever):
    return Router(reg, Glossary({}), retriever, ThresholdDisambiguator(),
                  AuthzService(_RBAC, InMemoryDecisionCache()))


def _principal():
    return Principal(tenant_id="t-a", user_id="u-1", role="service_desk_agent")


def _signals():
    from oneops.router.signals import RequestSignals
    return RequestSignals(role="service_desk_agent", tenant_id="t-a")


async def _route_once(tmp_path, retriever):
    reg = make_registry(tmp_path, [
        make_agent("uc_summary", condition=intent_cond("summary"))])
    router = _router(reg, retriever)
    return await router.route("summarize the incident",
                              principal=_principal(), signals=_signals())


async def test_flag_off_does_not_prewarm(tmp_path, monkeypatch):
    monkeypatch.delenv("ONEOPS_ROUTER_PARALLEL_EMBED", raising=False)
    r = _PrewarmStubRetriever([Candidate("uc_summary", 0.9)])
    result = await _route_once(tmp_path, r)
    assert result.outcome is RouteOutcome.ROUTED
    assert result.plan.agent_ids == ("uc_summary",)
    assert r.prewarm_calls == []                     # not fired when flag off


async def test_flag_on_prewarms_and_route_is_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEOPS_ROUTER_PARALLEL_EMBED", "1")
    r = _PrewarmStubRetriever([Candidate("uc_summary", 0.9)])
    result = await _route_once(tmp_path, r)
    # SAME routing decision as flag-off
    assert result.outcome is RouteOutcome.ROUTED
    assert result.plan.agent_ids == ("uc_summary",)
    # and the prewarm fired with the raw query
    assert r.prewarm_calls == [("summarize the incident", "t-a")]


async def test_prewarm_failure_never_breaks_routing(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEOPS_ROUTER_PARALLEL_EMBED", "1")
    r = _PrewarmStubRetriever([Candidate("uc_summary", 0.9)], fail_prewarm=True)
    result = await _route_once(tmp_path, r)
    # best-effort: a prewarm crash must not change the outcome
    assert result.outcome is RouteOutcome.ROUTED
    assert result.plan.agent_ids == ("uc_summary",)


async def test_retriever_without_prewarm_method_is_safe(tmp_path, monkeypatch):
    """A retriever that doesn't implement prewarm_embed (e.g. LexicalRetriever)
    must route normally under the flag — the router guards with hasattr."""
    monkeypatch.setenv("ONEOPS_ROUTER_PARALLEL_EMBED", "1")

    class _NoPrewarm:
        async def retrieve(self, query_text, *, tenant_id, top_k):
            return [Candidate("uc_summary", 0.9)]

    result = await _route_once(tmp_path, _NoPrewarm())
    assert result.outcome is RouteOutcome.ROUTED
    assert result.plan.agent_ids == ("uc_summary",)
