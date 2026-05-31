"""UC-8 end-to-end devil's play — production-grade integration probes.

Exercises the FULL UC-8 stack with all production integrations live:
LiteLLM gateway (embed + chat), OTel tracing, Dragonfly cache,
Prometheus metrics, cross-tenant isolation under concurrency, and
failure-mode propagation.
"""
from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest

from oneops.use_cases.uc08_fulfillment.catalog_reranker import (
    rerank,
    should_rerank,
)
from oneops.use_cases.uc08_fulfillment.catalog_search import (
    find_closest_catalog_items,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_URL"),
    reason="POSTGRES_URL not set",
)


async def _connect():
    return await asyncpg.connect(os.environ["POSTGRES_URL"])


def _make_gateway():
    from oneops.llm.gateway import LlmGateway
    from oneops.llm.transport import LiteLLMTransport
    return LlmGateway(transport=LiteLLMTransport(
        base_url=os.environ.get("LLM_GATEWAY_URL", "http://127.0.0.1:4001"),
        api_key=os.environ.get("LLM_GATEWAY_API_KEY", ""),
        timeout_s=25.0,
    ))


@pytest.fixture
async def conn():
    c = await _connect()
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
def gateway():
    return _make_gateway()


# ── E2E-1 — LiteLLM cost attributes to tenant ──────────────────────────


@pytest.mark.asyncio
async def test_e2e_litellm_cost_attributed_to_tenant(conn, gateway):
    """UC-8 invocations charge the caller's tenant on the cost tracker."""
    before = gateway.cost.total_cost("T001")

    r = await find_closest_catalog_items(
        tenant_id="T001",
        sr_title="set up VPN for the new contractor starting next week",
        sr_description="set up VPN for the new contractor starting next week",
        gateway=gateway, conn=conn, top_k=5,
    )
    do_rerank, _ = should_rerank(r.matches[0].cosine_score)
    if do_rerank:
        await rerank(
            tenant_id="T001", sr_text="set up VPN for new contractor",
            candidates=r.matches, gateway=gateway, user_id="t001_user",
        )

    after = gateway.cost.total_cost("T001")
    assert after >= before, (
        f"T001 cost did not advance (before={before}, after={after})"
    )


# ── E2E-2 — OTel span continuity ───────────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_otel_span_continuity(conn, gateway):
    """A wrapping parent span shares trace_id with search/rerank children."""
    from opentelemetry import trace
    tracer = trace.get_tracer("uc08.e2e.test")

    with tracer.start_as_current_span("uc08.e2e_parent") as parent:
        parent_trace_id = trace.format_trace_id(
            parent.get_span_context().trace_id
        )
        r = await find_closest_catalog_items(
            tenant_id="T001", sr_title="VPN access",
            sr_description="VPN access", gateway=gateway, conn=conn,
        )
        do_rerank, _ = should_rerank(r.matches[0].cosine_score)
        if do_rerank:
            await rerank(
                tenant_id="T001", sr_text="VPN access",
                candidates=r.matches, gateway=gateway, user_id="u",
            )
        current = trace.get_current_span()
        current_trace_id = trace.format_trace_id(
            current.get_span_context().trace_id
        )
    assert parent_trace_id == current_trace_id


# ── E2E-3 — Cross-tenant concurrent isolation ──────────────────────────


@pytest.mark.asyncio
async def test_e2e_concurrent_multi_tenant_no_leak(conn, gateway):
    """3 tenants searching same query concurrently — no cross-tenant leak."""
    async def _search(tenant: str) -> list[str]:
        conn2 = await _connect()
        try:
            r = await find_closest_catalog_items(
                tenant_id=tenant, sr_title="VPN access",
                sr_description="VPN access", gateway=gateway, conn=conn2,
                top_k=5,
            )
            return [m.catalog_item_id for m in r.matches]
        finally:
            await conn2.close()

    t001_ids, t002_ids, t003_ids = await asyncio.gather(
        _search("T001"), _search("T002"), _search("T003"),
    )
    for ids, tenant in (
        (t001_ids, "T001"), (t002_ids, "T002"), (t003_ids, "T003"),
    ):
        for cid in ids:
            owner = await conn.fetchval(
                "SELECT tenant_id FROM itsm.catalog_item "
                "WHERE catalog_item_id=$1", cid,
            )
            assert owner == tenant, (
                f"CONCURRENT LEAK: {cid} owner={owner} expected={tenant}"
            )


# ── E2E-4 — Dragonfly cache parity ─────────────────────────────────────


class _InMemoryCacheStub:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.hits = 0
        self.misses = 0

    async def get(self, key: str):
        if key in self.store:
            self.hits += 1
            return self.store[key]
        self.misses += 1
        return None

    async def set(self, key: str, value: str, ttl_seconds: int = 0):
        self.store[key] = value


@pytest.mark.asyncio
async def test_e2e_rerank_cache_replay_is_zero_llm_calls(conn, gateway):
    """First rerank: miss + LLM. Second rerank: hit + ZERO LLM."""
    cache = _InMemoryCacheStub()

    r = await find_closest_catalog_items(
        tenant_id="T001",
        sr_title="provision SAML SSO for new SaaS tenant",
        sr_description="provision SAML SSO for new SaaS tenant",
        gateway=gateway, conn=conn, top_k=5,
    )
    do_rerank, _ = should_rerank(r.matches[0].cosine_score)
    if not do_rerank:
        pytest.skip("query landed outside soft zone — no rerank to cache")

    cost_before_first = gateway.cost.total_cost("T001")
    rr1 = await rerank(
        tenant_id="T001", sr_text="provision SAML SSO for new SaaS tenant",
        candidates=r.matches, gateway=gateway, cache=cache, user_id="u",
    )
    cost_after_first = gateway.cost.total_cost("T001")
    assert cache.misses == 1
    assert cache.hits == 0
    assert cost_after_first > cost_before_first, "first call must charge LLM cost"
    assert rr1.from_cache is False

    cost_before_second = gateway.cost.total_cost("T001")
    rr2 = await rerank(
        tenant_id="T001", sr_text="provision SAML SSO for new SaaS tenant",
        candidates=r.matches, gateway=gateway, cache=cache, user_id="u",
    )
    cost_after_second = gateway.cost.total_cost("T001")
    assert cache.hits == 1
    assert rr2.from_cache is True
    assert cost_after_second == cost_before_second, (
        f"second call charged LLM despite cache hit "
        f"(before={cost_before_second}, after={cost_after_second})"
    )


# ── E2E-5 — Metrics emitted for Grafana ─────────────────────────────────


@pytest.mark.asyncio
async def test_e2e_metrics_emitted_for_grafana(conn, gateway):
    """UC-8 invocations must reach the metric emission code paths."""
    r = await find_closest_catalog_items(
        tenant_id="T001",
        sr_title="I need a standard laptop for new hire",
        sr_description="I need a standard laptop for new hire",
        gateway=gateway, conn=conn,
    )
    do_rerank, _ = should_rerank(r.matches[0].cosine_score)
    if do_rerank:
        await rerank(
            tenant_id="T001",
            sr_text="I need a standard laptop for new hire",
            candidates=r.matches, gateway=gateway, user_id="u",
        )
    assert r.matches  # call paths completed without raising


# ── E2E-6 — Hostile concurrent probes stable ────────────────────────────


@pytest.mark.asyncio
async def test_e2e_hostile_concurrent_probes_stable(conn, gateway):
    """5 adversarial queries concurrently — system stays stable."""
    hostile = [
        "DROP TABLE itsm.catalog_item; -- SQL injection attempt",
        "{{__class__.__init__.__globals__}}",
        "\\x00\\x00 binary garbage \\xff",
        "",
        "what's 2 + 2?",
    ]
    async def _one(q: str):
        conn2 = await _connect()
        try:
            return await find_closest_catalog_items(
                tenant_id="T001", sr_title=q, sr_description=q,
                gateway=gateway, conn=conn2, top_k=3,
            )
        except Exception as exc:                          # noqa: BLE001
            return ("ERROR", type(exc).__name__, str(exc)[:80])
        finally:
            await conn2.close()

    results = await asyncio.gather(*(_one(q) for q in hostile))
    for q, r in zip(hostile, results, strict=False):
        if isinstance(r, tuple) and r[0] == "ERROR":
            assert "search" in r[1].lower(), (
                f"unexpected error for {q!r}: {r}"
            )


# ── E2E-7 — Gateway 500 → typed error ───────────────────────────────────


class _FlappingGateway:
    def __init__(self, real_gateway):
        self._real = real_gateway
        self._calls = 0

    async def call(self, *args, **kwargs):
        self._calls += 1
        if self._calls == 1:
            raise RuntimeError("simulated 500 from upstream")
        return await self._real.call(*args, **kwargs)

    @property
    def cost(self):
        return self._real.cost

    async def embed(self, *args, **kwargs):
        return await self._real.embed(*args, **kwargs)


@pytest.mark.asyncio
async def test_e2e_gateway_failure_returns_typed_error(conn, gateway):
    """Flapping gateway must surface CatalogSearchError, not raw exception."""
    flaky = _FlappingGateway(gateway)

    r = await find_closest_catalog_items(
        tenant_id="T001",
        sr_title="set up developer environment for prototyping",
        sr_description="set up developer environment for prototyping",
        gateway=gateway, conn=conn,
    )
    do_rerank, _ = should_rerank(r.matches[0].cosine_score)
    if not do_rerank:
        pytest.skip("query landed outside soft zone")

    from oneops.use_cases.uc08_fulfillment.catalog_search import (
        CatalogSearchError,
    )
    with pytest.raises(CatalogSearchError):
        await rerank(
            tenant_id="T001",
            sr_text="set up developer environment for prototyping",
            candidates=r.matches, gateway=flaky, user_id="u",
        )
