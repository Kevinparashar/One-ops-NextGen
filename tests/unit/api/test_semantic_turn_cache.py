"""Semantic turn cache — the cross-session consistency layer.

Locks the two load-bearing behaviours:
  * `is_standalone` gate — only context-free queries (no pronoun / record id)
    are eligible, so focus context never leaks across sessions.
  * get/put round-trip — an equivalent query returns the stored response
    (cosine ≥ threshold); an unrelated query misses.
"""
from __future__ import annotations

import math

from oneops.api.semantic_turn_cache import SemanticTurnCache, is_standalone


def test_is_standalone_accepts_context_free_queries():
    assert is_standalone("how do I configure a VPN client")
    assert is_standalone("database connection fails")
    assert is_standalone("recommend a good pizza place")


def test_is_standalone_rejects_referential_or_record_specific():
    assert not is_standalone("what is its priority")          # pronoun
    assert not is_standalone("summarize the incident")        # "the incident"
    assert not is_standalone("summarize INC0001001")          # record id
    assert not is_standalone("1003")                          # bare digits
    assert not is_standalone("")                              # empty


class _FakeRedis:
    """Minimal async Redis list API used by the cache."""

    def __init__(self) -> None:
        self.store: dict[str, list[bytes]] = {}

    async def lpush(self, k, v):
        self.store.setdefault(k, []).insert(0, v)

    async def ltrim(self, k, a, b):
        self.store[k] = self.store.get(k, [])[a:(None if b == -1 else b + 1)]

    async def expire(self, k, t):
        return True

    async def lrange(self, k, a, b):
        return self.store.get(k, [])[a:(None if b == -1 else b + 1)]


def _toy_embed_factory():
    """Deterministic toy embedding: char-frequency vector over a-z (+space),
    punctuation stripped. Same/near-same text → cosine ~1.0; different → low."""
    async def _embed(text, *, tenant_id, user_id=""):
        t = "".join(c for c in (text or "").lower() if c.isalpha() or c == " ")
        vec = [0.0] * 27
        for c in t:
            vec[26 if c == " " else ord(c) - 97] += 1.0
        n = math.sqrt(sum(x * x for x in vec))
        return [x / n for x in vec] if n else vec
    return _embed


async def test_put_then_get_returns_cached_response():
    cache = SemanticTurnCache(redis=_FakeRedis(), embed=_toy_embed_factory(),
                              threshold=0.97)
    resp = {"final_status": "executed", "final_response": "VPN steps…"}
    await cache.put(tenant_id="T001", role="agent",
                    query="how do I configure a VPN client", response=resp)
    hit = await cache.get(tenant_id="T001", role="agent",
                          query="how do I configure a VPN client")
    assert hit == resp


async def test_get_misses_unrelated_query():
    cache = SemanticTurnCache(redis=_FakeRedis(), embed=_toy_embed_factory(),
                              threshold=0.97)
    await cache.put(tenant_id="T001", role="agent",
                    query="how do I configure a VPN client",
                    response={"final_response": "vpn"})
    miss = await cache.get(tenant_id="T001", role="agent",
                           query="quarterly revenue projections spreadsheet")
    assert miss is None


async def test_tenant_isolation():
    cache = SemanticTurnCache(redis=_FakeRedis(), embed=_toy_embed_factory())
    await cache.put(tenant_id="T001", role="agent", query="vpn config",
                    response={"final_response": "x"})
    assert await cache.get(tenant_id="T002", role="agent",
                           query="vpn config") is None
