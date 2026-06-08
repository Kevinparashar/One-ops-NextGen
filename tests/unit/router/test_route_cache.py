"""Route-decision cache + query-embedding cache — unit + integration.

These exercise the caches WITHOUT any live LLM: the pure key/serialization
functions, the in-memory stores (TTL + tenant isolation), the registry
fingerprint, and — the load-bearing correctness test — that a second identical
route is a HIT that skips decompose+disambiguate yet returns the same plan,
while anything that changes the route (role, registry, tenant) is a MISS.
"""
from __future__ import annotations

import asyncio

import pytest

from oneops.authz.decision_cache import InMemoryDecisionCache
from oneops.authz.models import Principal
from oneops.authz.rbac import RbacResolver
from oneops.authz.service import AuthzService
from oneops.router.decompose import SubQuery
from oneops.router.disambiguation import Disambiguation
from oneops.router.glossary import Glossary
from oneops.router.plan import RouteOutcome, SubQueryRoute
from oneops.router.retrieval import Candidate
from oneops.router.route_cache import (
    InMemoryQueryEmbeddingCache,
    InMemoryRouteDecisionCache,
    conversation_digest,
    deserialize_routes,
    embedding_cache_key,
    route_cache_key,
    serialize_decision,
    signals_digest,
)
from oneops.router.router import Router
from oneops.router.signals import RequestSignals

from ._factories import intent_cond, make_agent, make_registry

# asyncio_mode = "auto" (pyproject) runs the async tests without an explicit
# marker; no module-level pytest.mark.asyncio (it would warn on the pure-fn
# tests in this file).

_RBAC = RbacResolver({
    "service_desk_agent": frozenset({"read:all_tickets", "write:ticket", "create:ticket"}),
    "employee": frozenset({"read:own_tickets"}),
})


# ── key composition (pure) ───────────────────────────────────────────────────

def _key(**over):
    base = dict(query="summarize inc1", role="r", domain="", focus_entity_id="",
                focus_service_id="", sig_digest="s", conv_digest="0",
                registry_fingerprint="fp")
    base.update(over)
    return route_cache_key(**base)


def test_key_normalizes_query_whitespace_and_case():
    assert _key(query="Summarize  INC1") == _key(query="summarize inc1")


@pytest.mark.parametrize("field,val", [
    ("query", "different"), ("role", "other"), ("domain", "itom"),
    ("focus_entity_id", "INC9"), ("focus_service_id", "incident"),
    ("sig_digest", "other"), ("conv_digest", "deadbeef"),
    ("registry_fingerprint", "CHANGED"),
])
def test_every_routing_input_changes_the_key(field, val):
    # Correctness invariant: anything that can change the route MUST change the
    # key, or the cache could serve a route computed under different inputs.
    assert _key() != _key(**{field: val})


def test_embedding_key_is_model_and_dimension_versioned():
    base = dict(text="reset mfa", model="text-embedding-3-large", dimensions=1536)
    assert embedding_cache_key(**base) == embedding_cache_key(**{**base, "text": "Reset  MFA"})
    assert embedding_cache_key(**base) != embedding_cache_key(**{**base, "model": "other"})
    assert embedding_cache_key(**base) != embedding_cache_key(**{**base, "dimensions": 3072})


# ── signals / conversation digests ───────────────────────────────────────────

def _sig(**over):
    base = dict(role="service_desk_agent", tenant_id="t-a")
    base.update(over)
    return RequestSignals(**base)


def test_signals_digest_stable_and_sensitive():
    assert signals_digest(_sig()) == signals_digest(_sig())
    assert signals_digest(_sig()) != signals_digest(_sig(role="employee"))
    assert signals_digest(_sig()) != signals_digest(
        _sig(present_entities=(("INC1", "incident"),)))
    assert signals_digest(_sig()) != signals_digest(
        _sig(tenant_capabilities=frozenset({"x"})))
    assert signals_digest(_sig()) != signals_digest(_sig(has_active_focus=True))
    assert signals_digest(_sig()) != signals_digest(_sig(intents=frozenset({"summary"})))


def test_conversation_digest_empty_is_zero_and_history_changes_it():
    assert conversation_digest(None) == "0"
    assert conversation_digest([]) == "0"

    class _T:
        def __init__(self, role, content):
            self.role, self.content = role, content

    d1 = conversation_digest([_T("user", "hi")])
    d2 = conversation_digest([_T("user", "bye")])
    assert d1 != "0" and d1 != d2


# ── serialize / deserialize round-trip ───────────────────────────────────────

def test_decision_round_trip_preserves_routes():
    routes = [SubQueryRoute(
        sub_query_id="sq1", agent_ids=["uc01"],
        parameters_by_agent={"uc01": {"ticket_id": "INC1"}},
        depends_on_subqueries=["sq0"],
        bindings=[("sq0", "affected_ci", "ci_id")])]
    blob = serialize_decision(outcome="routed", routes=routes, unrouted=["x"], reason="")
    back = deserialize_routes(blob["routes"])
    assert len(back) == 1
    r = back[0]
    assert r.sub_query_id == "sq1"
    assert r.agent_ids == ["uc01"]
    assert r.parameters_by_agent == {"uc01": {"ticket_id": "INC1"}}
    assert r.depends_on_subqueries == ["sq0"]
    assert r.bindings == [("sq0", "affected_ci", "ci_id")]
    assert blob["unrouted"] == ["x"]


# ── in-memory stores: TTL + tenant isolation ─────────────────────────────────

async def test_route_cache_get_put_and_tenant_isolation():
    c = InMemoryRouteDecisionCache(ttl_seconds=100)
    await c.put(tenant_id="t-a", key="k", value={"outcome": "routed"})
    assert (await c.get(tenant_id="t-a", key="k")) == {"outcome": "routed"}
    # different tenant cannot read it (namespaced) → leak impossible
    assert (await c.get(tenant_id="t-b", key="k")) is None


async def test_route_cache_ttl_expiry():
    c = InMemoryRouteDecisionCache(ttl_seconds=0)
    await c.put(tenant_id="t-a", key="k", value={"x": 1})
    await asyncio.sleep(0.01)
    assert (await c.get(tenant_id="t-a", key="k")) is None


async def test_embedding_cache_round_trip():
    c = InMemoryQueryEmbeddingCache(ttl_seconds=100)
    assert (await c.get(key="k")) is None
    await c.put(key="k", vector=[0.1, 0.2, 0.3])
    assert (await c.get(key="k")) == [0.1, 0.2, 0.3]


# ── registry fingerprint ─────────────────────────────────────────────────────

def test_registry_fingerprint_memoized_and_change_sensitive(tmp_path):
    reg1 = make_registry(tmp_path / "a", [make_agent("uc01", condition=intent_cond("summary"))])
    fp = reg1.routing_fingerprint()
    assert fp and reg1.routing_fingerprint() == fp           # memoized / stable
    reg2 = make_registry(tmp_path / "b", [
        make_agent("uc01", condition=intent_cond("summary")),
        make_agent("uc02", condition=intent_cond("similar"))])
    assert reg2.routing_fingerprint() != fp                  # added agent → new fp


# ── integration: MISS → HIT short-circuits the LLM stages ────────────────────

class _CountingDecomposer:
    def __init__(self):
        self.calls = 0

    async def decompose(self, message, *, request_ctx):
        self.calls += 1
        return [SubQuery(id="sq1", text=message)]


class _CountingDisambiguator:
    def __init__(self, result):
        self.calls = 0
        self._result = result

    async def disambiguate(self, query_text, candidates, *, request_ctx):
        self.calls += 1
        return self._result


class _StubRetriever:
    def __init__(self, candidates):
        self._candidates = candidates

    async def retrieve(self, query_text, *, tenant_id, top_k):
        return list(self._candidates)


def _authz():
    return AuthzService(_RBAC, InMemoryDecisionCache())


def _principal(role="service_desk_agent", tenant="t-a"):
    return Principal(tenant_id=tenant, user_id="u-1", role=role)


def _build_router(reg, *, retriever, disambiguator, decomposer, cache):
    return Router(reg, Glossary({}), retriever, disambiguator, _authz(),
                  decomposer=decomposer, route_cache=cache)


async def test_second_identical_route_is_a_hit_that_skips_llm(tmp_path):
    reg = make_registry(tmp_path, [
        make_agent("uc01", condition=intent_cond("summary")),
        make_agent("uc02", condition=intent_cond("summary"))])
    cache = InMemoryRouteDecisionCache(ttl_seconds=100)
    dec = _CountingDecomposer()
    dis = _CountingDisambiguator(
        Disambiguation.select(["uc01"], confidence=0.9))
    router = _build_router(
        reg, retriever=_StubRetriever([Candidate("uc01", 0.9), Candidate("uc02", 0.5)]),
        disambiguator=dis, decomposer=dec, cache=cache)

    r1 = await router.route("summarize it", principal=_principal(), signals=_sig())
    assert r1.outcome is RouteOutcome.ROUTED
    assert r1.plan.agent_ids == ("uc01",)
    assert dec.calls == 1 and dis.calls == 1                 # funnel ran (miss)

    r2 = await router.route("summarize it", principal=_principal(), signals=_sig())
    assert r2.outcome is RouteOutcome.ROUTED
    assert r2.plan.agent_ids == ("uc01",)                    # same decision
    assert dec.calls == 1 and dis.calls == 1                 # HIT — no LLM re-run


async def test_no_match_is_cached_too(tmp_path):
    reg = make_registry(tmp_path, [
        make_agent("uc01", condition=intent_cond("summary")),
        make_agent("uc02", condition=intent_cond("summary"))])
    cache = InMemoryRouteDecisionCache(ttl_seconds=100)
    dec = _CountingDecomposer()
    dis = _CountingDisambiguator(Disambiguation())           # empty → no match
    router = _build_router(
        reg, retriever=_StubRetriever([Candidate("uc01", 0.1), Candidate("uc02", 0.1)]),
        disambiguator=dis, decomposer=dec, cache=cache)

    r1 = await router.route("vague thing", principal=_principal(), signals=_sig())
    assert r1.outcome is RouteOutcome.NO_CONFIDENT_MATCH
    assert dis.calls == 1
    r2 = await router.route("vague thing", principal=_principal(), signals=_sig())
    assert r2.outcome is RouteOutcome.NO_CONFIDENT_MATCH
    assert dis.calls == 1                                     # cached no-match


async def test_different_role_is_a_miss_no_wrong_route_reuse(tmp_path):
    reg = make_registry(tmp_path, [make_agent("uc01", condition=intent_cond("summary"))])
    cache = InMemoryRouteDecisionCache(ttl_seconds=100)
    dec = _CountingDecomposer()
    dis = _CountingDisambiguator(Disambiguation.select(["uc01"], confidence=0.9))
    router = _build_router(
        reg, retriever=_StubRetriever([Candidate("uc01", 0.9)]),
        disambiguator=dis, decomposer=dec, cache=cache)

    await router.route("summarize it", principal=_principal(role="service_desk_agent"),
                       signals=_sig(role="service_desk_agent"))
    n = dec.calls
    # Same query, DIFFERENT role → different key → must re-run (not a hit).
    await router.route("summarize it", principal=_principal(role="employee"),
                       signals=_sig(role="employee"))
    assert dec.calls == n + 1


async def test_different_tenant_is_a_miss(tmp_path):
    reg = make_registry(tmp_path, [make_agent("uc01", condition=intent_cond("summary"))])
    cache = InMemoryRouteDecisionCache(ttl_seconds=100)
    dec = _CountingDecomposer()
    dis = _CountingDisambiguator(Disambiguation.select(["uc01"], confidence=0.9))
    router = _build_router(
        reg, retriever=_StubRetriever([Candidate("uc01", 0.9)]),
        disambiguator=dis, decomposer=dec, cache=cache)
    await router.route("summarize it", principal=_principal(tenant="t-a"),
                       signals=_sig(tenant_id="t-a"))
    n = dec.calls
    await router.route("summarize it", principal=_principal(tenant="t-b"),
                       signals=_sig(tenant_id="t-b"))
    assert dec.calls == n + 1                                 # tenant-isolated


async def test_cache_disabled_runs_funnel_every_time(tmp_path):
    reg = make_registry(tmp_path, [make_agent("uc01", condition=intent_cond("summary"))])
    dec = _CountingDecomposer()
    dis = _CountingDisambiguator(Disambiguation.select(["uc01"], confidence=0.9))
    router = _build_router(
        reg, retriever=_StubRetriever([Candidate("uc01", 0.9)]),
        disambiguator=dis, decomposer=dec, cache=None)       # disabled
    await router.route("summarize it", principal=_principal(), signals=_sig())
    await router.route("summarize it", principal=_principal(), signals=_sig())
    assert dec.calls == 2                                     # no caching
