"""Semantic turn cache — cross-session consistency for standalone chat queries.

WHY: the routing pipeline uses several LLM stages that are NOT bit-deterministic
even at temperature 0 (OpenAI's API is officially "mostly deterministic"). For a
STANDALONE query — one with no pronoun and no specific record id — tiny per-call
variance can flip a *borderline* result between runs (observed 2026-06-02: "how
do I configure a VPN client" answered on some runs, out-of-scope on others). The
only way to GUARANTEE "same query → same answer" is a deterministic cache lookup
(the semantic-cache pattern — GPTCache / Portkey / Redis vector cache).

DESIGN:
  * Bucket = (tenant, role). Cross-session and cross-user on purpose: a
    standalone query's answer is context-free, and role scopes KB audience.
  * Match by embedding cosine ≥ threshold (near-identical → same answer). This
    folds "configure a VPN client" / "...client?" / "...clients" / light
    rephrasings together WITHOUT any keyword catalog — the embedding is the
    semantic key (deterministic for the same input string).
  * EXCLUDES focus-bound / record-specific queries (pronoun, "the incident",
    a canonical id, a bare number). Those are answered from the session
    turn-cache + the record-hash UC caches, so context can never leak here.

SAFETY: best-effort. No embedder, Redis down, or any error → returns None / no-op
so the turn runs the normal pipeline. Never blocks or breaks a turn. TTL-bounded
and version-stamped (PIPELINE_CACHE_VERSION) like the other caches.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Awaitable, Callable
from typing import Any

from oneops.observability import get_logger
from oneops.observability.metrics import increment as _metric_inc

_log = get_logger("oneops.api.semantic_turn_cache")

# Deterministic "standalone" gate. A query is standalone (context-free, safe to
# share cross-session) only when it does NOT reference session focus or a
# specific record. These are grammatical/structural patterns, not a domain
# keyword catalogue.
_REF_RE = re.compile(
    r"\b(it|its|it's|this|that|these|those|they|them|their|the\s+"
    r"(incident|ticket|problem|change|request|record|article|ci|asset|kb))\b",
    re.IGNORECASE,
)
_ID_RE = re.compile(r"\b[A-Za-z]{2,4}\d{4,}\b")
_BARE_DIGITS = re.compile(r"^\s*\d+\s*$")


def is_standalone(query: str) -> bool:
    """True when the query has no implicit reference (pronoun / "the ticket") and
    no specific record id — i.e. its answer does not depend on session focus."""
    q = (query or "").strip()
    if len(q) < 3:
        return False
    if _ID_RE.search(q) or _BARE_DIGITS.match(q):
        return False
    return not _REF_RE.search(q)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# embed_fn(query, *, tenant_id, user_id) -> list[float]  (the wired KB embedder)
EmbedFn = Callable[..., Awaitable[list[float]]]

_PREFIX = "oneops:semcache"


class SemanticTurnCache:
    """Cross-session embedding-similarity cache for standalone chat turns."""

    def __init__(self, *, redis: Any, embed: EmbedFn, ttl_seconds: int = 600,
                 threshold: float = 0.97, bucket_max: int = 64) -> None:
        self._redis = redis
        self._embed = embed
        self._ttl = ttl_seconds
        self._threshold = threshold
        self._max = bucket_max

    def _bucket(self, tenant_id: str, role: str) -> str:
        from oneops.api.cache_version import PIPELINE_CACHE_VERSION
        return f"{_PREFIX}:{PIPELINE_CACHE_VERSION}:{tenant_id}:{role or '-'}"

    async def _vec(self, query: str, tenant_id: str) -> list[float]:
        return await self._embed(query, tenant_id=tenant_id, user_id="")

    async def get(self, *, tenant_id: str, role: str, query: str) -> dict[str, Any] | None:
        """Return the cached response for a semantically-equivalent prior
        standalone query, or None. Best-effort — never raises."""
        try:
            qv = await self._vec(query, tenant_id)
            if not qv:
                return None
            entries = await self._redis.lrange(
                self._bucket(tenant_id, role), 0, self._max - 1)
            best: dict[str, Any] | None = None
            best_s = 0.0
            for raw in entries or []:
                try:
                    e = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                s = _cosine(qv, e.get("emb") or [])
                if s > best_s:
                    best_s, best = s, e
            if best is not None and best_s >= self._threshold:
                _metric_inc("ai.semantic_cache.hits.total", 1,
                            tenant_id=tenant_id)
                _log.info("semantic_turn_cache.hit",
                          tenant_id=tenant_id, score=round(best_s, 4))
                return best.get("resp")
        except Exception as exc:                               # noqa: BLE001
            _log.warning("semantic_turn_cache.get_failed", error=str(exc)[:160])
        _metric_inc("ai.semantic_cache.misses.total", 1, tenant_id=tenant_id)
        return None

    async def put(self, *, tenant_id: str, role: str, query: str,
                  response: dict[str, Any]) -> None:
        """Store the response under the query embedding. Best-effort."""
        try:
            qv = await self._vec(query, tenant_id)
            if not qv:
                return
            b = self._bucket(tenant_id, role)
            await self._redis.lpush(
                b, json.dumps({"emb": qv, "resp": dict(response)},
                              default=str).encode("utf-8"))
            await self._redis.ltrim(b, 0, self._max - 1)
            await self._redis.expire(b, self._ttl)
            _metric_inc("ai.semantic_cache.writes.total", 1, tenant_id=tenant_id)
        except Exception as exc:                               # noqa: BLE001
            _log.warning("semantic_turn_cache.put_failed", error=str(exc)[:160])


__all__ = ["SemanticTurnCache", "is_standalone"]
