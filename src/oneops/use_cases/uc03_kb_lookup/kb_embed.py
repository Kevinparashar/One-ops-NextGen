"""UC-3 query-embedding plumbing — gateway-backed embed fn + Dragonfly
cache + injection seam.

The KbStore stays pure data-access; this module is where the LLM call
lives so the KbStore is testable without a live gateway. Architecture:

    handler.search_kb(query)
        └─> _embed_query(query, tenant_id, user_id)
              ├─> cache.get((tenant_id, model, normalised_query)) → vec or None
              └─> on miss: gateway.embed(...) (OTel + cost + retries)
                      └─> cache.put((tenant_id, model, normalised_query), vec)
        └─> kb_store.search_semantic(query_vec=vec, ...)

OTel: `gateway.embed` opens an `llm.embed` span with tenant_id + user_id
+ model + token-count. The KbStore semantic path opens its own
`kb_store.postgres.search_semantic` span. The two link via the parent
request span.

LiteLLM: the gateway transport routes embed calls through the same
LiteLLM proxy URL as chat calls; per-tenant rate-limit + cost tracking
applies automatically.

Cache: tenant-prefixed Dragonfly key on the *normalised* query
(lowercased + whitespace-collapsed) so case + spacing don't cause
duplicate embeddings. Falls back silently to no-cache if Dragonfly is
unreachable (the embed call still succeeds — the user always gets an
answer).
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
from collections.abc import Awaitable, Callable
from typing import Any

from oneops.observability import get_logger, histogram, increment

_log = get_logger("oneops.use_cases.uc03.kb_embed")

EmbedFn = Callable[..., Awaitable[list[float]]]

_embed_fn: EmbedFn | None = None


def set_kb_embed_fn(fn: EmbedFn | None) -> None:
    """Process-wide injection seam — tests pass a stub, FaaS wiring
    passes the live gateway-backed fn from `app.py`."""
    global _embed_fn
    _embed_fn = fn


def get_kb_embed_fn() -> EmbedFn | None:
    return _embed_fn


def _normalise(query: str) -> str:
    return " ".join((query or "").lower().split())


def _cache_key(*, tenant_id: str, model: str, query: str) -> str:
    h = hashlib.sha256(_normalise(query).encode("utf-8")).hexdigest()[:32]
    return f"oneops:uc03:kbembed:{tenant_id}:{model}:{h}"


class _DragonflyEmbedCache:
    """Lazy Dragonfly client for query→vector caching.

    Failure is non-fatal: `get` returns None on any error, `put` swallows.
    The point is to save cost on repeat queries, not to be authoritative."""

    def __init__(self) -> None:
        self._redis: Any = None
        self._lock = asyncio.Lock()
        self._ttl = int(os.getenv("ONEOPS_KB_EMBED_TTL_S", "86400"))  # 24h

    async def _client(self) -> Any:
        if self._redis is not None:
            return self._redis
        async with self._lock:
            if self._redis is not None:
                return self._redis
            try:
                import redis.asyncio as aioredis

                from oneops.config import get_settings
                url = getattr(get_settings(),
                              "dragonfly_url",
                              "redis://localhost:6379/0")
                self._redis = aioredis.from_url(url, decode_responses=False)
            except Exception as exc:
                _log.warning("kb_embed.cache.client_init_failed",
                             error=str(exc)[:160])
                self._redis = False  # sentinel: disabled
            return self._redis

    async def get(self, *, tenant_id: str, model: str, query: str
                  ) -> list[float] | None:
        client = await self._client()
        if not client:
            return None
        try:
            raw = await client.get(_cache_key(
                tenant_id=tenant_id, model=model, query=query))
        except Exception as exc:
            _log.warning("kb_embed.cache.get_failed", error=str(exc)[:160])
            return None
        if raw is None:
            increment("ai.cache.misses.total", cache_name="uc03_kb_embed",
                      tenant_id=tenant_id)
            return None
        import json
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            vec = json.loads(raw)
        except Exception:
            return None
        if not isinstance(vec, list):
            return None
        increment("ai.cache.hits.total", cache_name="uc03_kb_embed",
                  tenant_id=tenant_id)
        return [float(x) for x in vec]

    async def put(self, *, tenant_id: str, model: str, query: str,
                  vec: list[float]) -> None:
        client = await self._client()
        if not client:
            return
        try:
            import json
            await client.setex(
                _cache_key(tenant_id=tenant_id, model=model, query=query),
                self._ttl,
                json.dumps(list(vec)),
            )
        except Exception as exc:
            _log.warning("kb_embed.cache.put_failed", error=str(exc)[:160])


_cache = _DragonflyEmbedCache()


def build_cached_embed_fn(gateway: Any, *, model: str,
                          dimensions: int | None = None) -> EmbedFn:
    """Wrap `LlmGateway.embed` with the Dragonfly query→vector cache.

    `dimensions` enforces matryoshka reduction at the API layer so the
    query vector matches the dimensionality stored on
    `itsm.kb_knowledge.embedding` (today: 1536-d for
    `text-embedding-3-large`). The cache key is per-(tenant, model);
    if the dimension is ever changed in env the cache should be flushed
    manually since stored vectors would shift shape.

    The returned callable accepts: `(query, tenant_id, user_id="")` → list[float]."""
    async def _embed(query: str, *, tenant_id: str,
                     user_id: str = "") -> list[float]:
        import time
        if not query or not tenant_id:
            return []
        cached = await _cache.get(tenant_id=tenant_id, model=model, query=query)
        if cached is not None:
            return cached
        t0 = time.monotonic()
        vectors = await gateway.embed(
            [query], model=model, tenant_id=tenant_id, user_id=user_id,
            dimensions=dimensions)
        histogram("ai.kb.embed.duration_ms",
                  (time.monotonic() - t0) * 1000.0,
                  tenant_id=tenant_id, model=model)
        if not vectors:
            return []
        vec = [float(x) for x in vectors[0]]
        await _cache.put(tenant_id=tenant_id, model=model,
                         query=query, vec=vec)
        return vec
    return _embed


def build_relevance_scorer(gateway: Any, *, model: str,
                           dimensions: int | None = None):
    """Return an async callable that scores each candidate article's
    relevance to the user's query as cosine similarity ∈ [0, 1].

    Signature:
        await scorer(query, doc_texts, tenant_id=…, user_id=…) ->
            list[float] of the same length as `doc_texts`.

    Used by the KB handler as a pre-composer gate: score each retrieved
    article against the query, filter to those above the configured
    threshold, keep the top-K. Articles that don't clear the threshold
    are dropped before the composer ever sees them.

    One batched embed call (1 query + N article texts → N+1 vectors),
    routed through the same gateway egress as every other LLM call
    (OTel `llm.embed` span + per-tenant cost + LiteLLM proxy + retries).
    Failure returns zeros for every doc — handler treats that as
    "scores unknown, fall through with whatever the retriever already
    ranked" so a gateway outage cannot block legitimate answers.
    """
    async def _score(
        query: str, doc_texts: list[str], *,
        tenant_id: str, user_id: str = "",
    ) -> list[float]:
        if not query or not doc_texts or not tenant_id:
            return [0.0] * len(doc_texts)
        try:
            vecs = await gateway.embed(
                [query, *doc_texts], model=model, tenant_id=tenant_id,
                user_id=user_id, dimensions=dimensions)
        except Exception as exc:
            _log.warning("kb_relevance.embed_failed",
                         error=str(exc)[:160])
            return [0.0] * len(doc_texts)
        if not vecs or len(vecs) != 1 + len(doc_texts):
            return [0.0] * len(doc_texts)
        q, docs = vecs[0], vecs[1:]
        if not q:
            return [0.0] * len(doc_texts)
        return _cosine_scores(q, docs)

    return _score


def _cosine_scores(
    q: list[float], docs: list[list[float]],
) -> list[float]:
    """Cosine similarity of `q` against each doc vector, clamped to [-1, 1].
    A doc with the wrong dimensionality (or empty) scores 0.0."""
    qn = math.sqrt(sum(x * x for x in q)) or 1.0
    scores: list[float] = []
    for d in docs:
        if not d or len(d) != len(q):
            scores.append(0.0)
            continue
        dn = math.sqrt(sum(x * x for x in d)) or 1.0
        cosine = sum(x * y for x, y in zip(q, d, strict=False)) / (qn * dn)
        scores.append(float(max(-1.0, min(1.0, cosine))))
    return scores


# Process-wide scorer seam — same shape as the embed fn.
_relevance_scorer = None


def set_kb_relevance_scorer(fn) -> None:
    global _relevance_scorer
    _relevance_scorer = fn


def get_kb_relevance_scorer():
    return _relevance_scorer


__all__ = [
    "EmbedFn",
    "set_kb_embed_fn",
    "get_kb_embed_fn",
    "build_cached_embed_fn",
    "build_relevance_scorer",
    "set_kb_relevance_scorer",
    "get_kb_relevance_scorer",
]
