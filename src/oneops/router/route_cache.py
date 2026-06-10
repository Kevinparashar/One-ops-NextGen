"""Router caches — the route-DECISION cache and the query-EMBEDDING cache.

Two caches on the routing critical path. NEITHER caches the *answer* (that is
`api/chat_turn_cache.py`, keyed by session). They cache the *route* and the
*query vector* — the most cross-session-stable artefacts in the system.

1. RouteDecisionCache — query+context → the funnel verdict (selected agents +
   their bound parameters + sub-query DAG, or a non-routed reason). The route
   "summarize INC0010045 → uc01" is identical for everyone, forever; without
   this cache, every answer-cache miss re-runs decompose + rewrite +
   disambiguate (3 LLM calls) just to re-derive it. Keyed on the normalized
   query + the routing-relevant request signals + focus + domain + role +
   conversation digest + the registry fingerprint — NOT on session or ticket
   data. Invalidated *structurally* by the registry fingerprint (a card edit
   changes the key) plus a TTL backstop. On a hit the executor still runs the
   plan FRESH, so the data is current — only the routing *decision* is reused.

   Correctness: the key contains every input the funnel reads to decide a route
   (signals digest = role/tenant/entities/capabilities/focus/intents;
   conversation digest = the history the rewriter resolves references against).
   Two requests with the same key provably produce the same route, so a hit can
   never serve a wrong route. Reference-bearing follow-ups ("close it") differ
   in the conversation digest, so they key per-context — correct, if less
   cacheable. The plan is rebuilt via `assemble_plan` (deterministic, no LLM)
   against the CURRENT registry on every hit.

2. QueryEmbeddingCache — normalize(query) → vector. The Stage-2 query embedding
   is deterministic per (model, dimensions); recomputing it on every route is a
   redundant gateway round-trip. Model-versioned key, long TTL, tenant-
   INDEPENDENT (the vector is identical for every caller — embeddings carry no
   tenant data; the agent corpus itself is global, see retrieval.py).

Storage shape mirrors `api/chat_turn_cache.py` (Protocol + InMemory + Dragonfly
+ build-from-env), per §2.10 — no new abstraction, same proven pattern.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:                                                  # pragma: no cover
    from oneops.router.plan import SubQueryRoute
    from oneops.router.rewrite import ConversationTurn
    from oneops.router.signals import RequestSignals

# Bump to invalidate every cached route on a routing-logic change (funnel
# stages, key composition) that the registry fingerprint would not catch.
_ROUTE_KEY_VERSION = "rc1"
_EMB_KEY_VERSION = "ec1"

# Route is far more stable than an answer — the registry fingerprint is the real
# invalidator, so the TTL is only a backstop (default 6h). Embeddings are
# deterministic per model version → long TTL (default 7d).
_DEFAULT_ROUTE_TTL_S = 6 * 60 * 60
_DEFAULT_EMB_TTL_S = 7 * 24 * 60 * 60


def _norm(s: str) -> str:
    """Whitespace-collapse + lowercase — the same normalization the chat-turn
    cache uses, so "Summarize  INC1" and "summarize inc1" share a key."""
    return " ".join((s or "").strip().lower().split())


# ── key composition ──────────────────────────────────────────────────────────

def signals_digest(signals: RequestSignals) -> str:
    """A stable 16-hex digest of every RequestSignals field the funnel reads.

    Anything that changes which candidates survive Stage-3 (role, tenant,
    entities, capabilities, focus presence, resolved intents) MUST be in the
    key, or the cache could serve a route computed under different signals.
    """
    payload = {
        "role": signals.role,
        "tenant": signals.tenant_id,
        "entities": sorted([list(e) for e in (signals.present_entities or ())]),
        "caps": sorted(signals.tenant_capabilities or frozenset()),
        "focus": bool(signals.has_active_focus),
        "intents": sorted(signals.intents) if signals.intents else None,
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def conversation_digest(history: Sequence[ConversationTurn] | None) -> str:
    """16-hex digest of the conversation the rewriter resolves references
    against. Empty history → "0" (the high-value first-turn / fresh-session
    case, where identical queries share a key). A different prior context
    yields a different digest, so a reference-bearing query never reuses a
    route resolved against other history."""
    if not history:
        return "0"
    raw = json.dumps(
        [[getattr(t, "role", ""), getattr(t, "content", "")] for t in history],
        default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def route_cache_key(
    *,
    query: str,
    role: str,
    domain: str,
    focus_entity_id: str,
    focus_service_id: str,
    sig_digest: str,
    conv_digest: str,
    registry_fingerprint: str,
) -> str:
    """Deterministic SHA-256 hex prefix over every route-deciding input."""
    raw = "\x1f".join([
        _ROUTE_KEY_VERSION, _norm(query), role or "", domain or "",
        focus_entity_id or "", focus_service_id or "",
        sig_digest, conv_digest, registry_fingerprint,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def embedding_cache_key(*, text: str, model: str, dimensions: int) -> str:
    raw = f"{_EMB_KEY_VERSION}\x1f{model}\x1f{dimensions}\x1f{_norm(text)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ── route-decision (de)serialization ─────────────────────────────────────────

def serialize_decision(
    *, outcome: str, routes: Sequence[SubQueryRoute],
    unrouted: Sequence[str], reason: str,
) -> dict[str, Any]:
    """Decision → JSON-safe dict. Stores the funnel verdict, NOT the answer."""
    return {
        "outcome": outcome,
        "reason": reason,
        "unrouted": list(unrouted),
        "routes": [
            {
                "sub_query_id": r.sub_query_id,
                "agent_ids": list(r.agent_ids),
                "parameters_by_agent": {
                    a: dict(p) for a, p in r.parameters_by_agent.items()},
                "depends_on_subqueries": list(r.depends_on_subqueries),
                "bindings": [list(b) for b in r.bindings],
            }
            for r in routes
        ],
    }


def deserialize_routes(data: Sequence[Mapping[str, Any]]) -> list[SubQueryRoute]:
    from oneops.router.plan import SubQueryRoute

    return [
        SubQueryRoute(
            sub_query_id=d["sub_query_id"],
            agent_ids=list(d["agent_ids"]),
            parameters_by_agent={
                a: dict(p) for a, p in (d.get("parameters_by_agent") or {}).items()},
            depends_on_subqueries=list(d.get("depends_on_subqueries") or []),
            bindings=[tuple(b) for b in (d.get("bindings") or [])],
        )
        for d in data
    ]


# ── caches: Protocol + InMemory + Dragonfly (mirrors chat_turn_cache) ─────────

class RouteDecisionCache(Protocol):
    async def get(self, *, tenant_id: str, key: str) -> dict[str, Any] | None: ...
    async def put(self, *, tenant_id: str, key: str,
                  value: Mapping[str, Any]) -> None: ...


class QueryEmbeddingCache(Protocol):
    async def get(self, *, key: str) -> list[float] | None: ...
    async def put(self, *, key: str, vector: Sequence[float]) -> None: ...


class _InMemoryTTL:
    """Shared TTL dict for the in-memory cache variants."""

    def __init__(self, ttl_seconds: int) -> None:
        self._rows: dict[str, tuple[float, Any]] = {}
        self._ttl = ttl_seconds

    def _get(self, k: str) -> Any | None:
        row = self._rows.get(k)
        if row is None:
            return None
        stored_at, value = row
        if time.time() - stored_at > self._ttl:
            self._rows.pop(k, None)
            return None
        return value

    def _put(self, k: str, value: Any) -> None:
        self._rows[k] = (time.time(), value)


class InMemoryRouteDecisionCache(_InMemoryTTL):
    """No-infra default (tests / local dev). Per-process."""

    def __init__(self, *, ttl_seconds: int = _DEFAULT_ROUTE_TTL_S) -> None:
        super().__init__(ttl_seconds)

    async def get(self, *, tenant_id: str, key: str) -> dict[str, Any] | None:
        v = self._get(f"{tenant_id}:{key}")
        return dict(v) if v is not None else None

    async def put(self, *, tenant_id: str, key: str,
                  value: Mapping[str, Any]) -> None:
        self._put(f"{tenant_id}:{key}", dict(value))


class InMemoryQueryEmbeddingCache(_InMemoryTTL):
    def __init__(self, *, ttl_seconds: int = _DEFAULT_EMB_TTL_S) -> None:
        super().__init__(ttl_seconds)

    async def get(self, *, key: str) -> list[float] | None:
        v = self._get(key)
        return list(v) if v is not None else None

    async def put(self, *, key: str, vector: Sequence[float]) -> None:
        self._put(key, list(vector))


_ROUTE_PREFIX = "oneops:router:route"
_EMB_PREFIX = "oneops:router:emb"


class DragonflyRouteDecisionCache:
    """Production route cache over Dragonfly (Redis protocol). Tenant-namespaced
    in the key, so a cross-tenant leak is impossible by construction (§2.4)."""

    def __init__(self, client: Any, *, ttl_seconds: int = _DEFAULT_ROUTE_TTL_S) -> None:
        self._redis = client
        self._ttl = ttl_seconds

    def _k(self, tenant_id: str, key: str) -> str:
        return f"{_ROUTE_PREFIX}:{tenant_id}:{key}"

    async def get(self, *, tenant_id: str, key: str) -> dict[str, Any] | None:
        raw = await self._redis.get(self._k(tenant_id, key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    async def put(self, *, tenant_id: str, key: str,
                  value: Mapping[str, Any]) -> None:
        await self._redis.set(
            self._k(tenant_id, key),
            json.dumps(dict(value), default=str).encode("utf-8"),
            ex=self._ttl)


class DragonflyQueryEmbeddingCache:
    """Production query-embedding cache over Dragonfly. Tenant-independent — the
    vector is identical for every caller, so keys are NOT tenant-namespaced
    (sharing across tenants is correct and saves memory)."""

    def __init__(self, client: Any, *, ttl_seconds: int = _DEFAULT_EMB_TTL_S) -> None:
        self._redis = client
        self._ttl = ttl_seconds

    def _k(self, key: str) -> str:
        return f"{_EMB_PREFIX}:{key}"

    async def get(self, *, key: str) -> list[float] | None:
        raw = await self._redis.get(self._k(key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    async def put(self, *, key: str, vector: Sequence[float]) -> None:
        await self._redis.set(
            self._k(key),
            json.dumps(list(vector), default=str).encode("utf-8"),
            ex=self._ttl)


def _dragonfly_client() -> Any:
    import redis.asyncio as aioredis

    from oneops.config import get_settings
    s = get_settings()
    return aioredis.from_url(
        getattr(s, "dragonfly_url", "redis://localhost:6379/0"),
        decode_responses=False)


def build_route_decision_cache(
    *, ttl_seconds: int | None = None,
) -> RouteDecisionCache | None:
    """Dragonfly by default (shared across replicas); in-memory on failure or
    when ROUTE_DECISION_CACHE_BACKEND=memory. Disable entirely with
    ROUTE_DECISION_CACHE_BACKEND=off → returns None (router skips the cache)."""
    backend = os.getenv("ROUTE_DECISION_CACHE_BACKEND", "dragonfly").strip().lower()
    if backend == "off":
        return None
    ttl = ttl_seconds if ttl_seconds is not None else int(
        os.getenv("ROUTE_DECISION_CACHE_TTL_S", str(_DEFAULT_ROUTE_TTL_S)))
    if backend == "dragonfly":
        try:
            return DragonflyRouteDecisionCache(_dragonfly_client(), ttl_seconds=ttl)
        except Exception:                                          # noqa: BLE001
            pass
    return InMemoryRouteDecisionCache(ttl_seconds=ttl)


def build_query_embedding_cache(
    *, ttl_seconds: int | None = None,
) -> QueryEmbeddingCache | None:
    backend = os.getenv("QUERY_EMBEDDING_CACHE_BACKEND", "dragonfly").strip().lower()
    if backend == "off":
        return None
    ttl = ttl_seconds if ttl_seconds is not None else int(
        os.getenv("QUERY_EMBEDDING_CACHE_TTL_S", str(_DEFAULT_EMB_TTL_S)))
    if backend == "dragonfly":
        try:
            return DragonflyQueryEmbeddingCache(_dragonfly_client(), ttl_seconds=ttl)
        except Exception:                                          # noqa: BLE001
            pass
    return InMemoryQueryEmbeddingCache(ttl_seconds=ttl)


__all__ = [
    "RouteDecisionCache", "QueryEmbeddingCache",
    "InMemoryRouteDecisionCache", "InMemoryQueryEmbeddingCache",
    "DragonflyRouteDecisionCache", "DragonflyQueryEmbeddingCache",
    "build_route_decision_cache", "build_query_embedding_cache",
    "route_cache_key", "embedding_cache_key",
    "signals_digest", "conversation_digest",
    "serialize_decision", "deserialize_routes",
]
