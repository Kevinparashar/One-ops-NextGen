"""Turn-level chat response cache.

Semantic, no keywords. The cache key is a hash of (tenant, user, role,
session, normalized_message). When the same chat turn re-arrives within the
TTL, the cached response is returned without running the routing pipeline,
focus classifier, UC handlers, or LLM calls.

Why it's safe:
  • Session-scoped — different sessions have different focus state, so
    "what is its category?" cannot leak between sessions.
  • Tenant-scoped — different tenants cannot read each other's responses.
  • TTL-bounded (default 600s = 10 min) — long enough that realistic chat
    follow-ups within a few minutes hit, short enough that ticket updates
    within ~10 min are reflected on the next turn. The embedding-refresh
    worker also invalidates UC-1's inner cache_aside on every UPDATE, so
    a still-warm turn-cache entry would at worst be one LLM call out of
    date — never factually wrong.
  • Refusals and errors are never cached.

What it replaces:
  • The per-component caching that only saved the summarization LLM call.
  • The keyword-based preroute attempts.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from typing import Any, Protocol


def _normalize(s: str) -> str:
    """Whitespace-collapse + lowercase. The cache key is byte-equivalent
    across "Summarize INC0001001" / "summarize  inc0001001" / etc."""
    return " ".join((s or "").strip().lower().split())


def cache_key(
    *, tenant_id: str, user_id: str, role: str,
    session_id: str, message: str,
) -> str:
    """Deterministic SHA-256 → hex prefix. Tenant + session in the hash
    means a leak is impossible by construction.

    Includes `PIPELINE_CACHE_VERSION` so a render-rule change (e.g. hiding
    a field that used to leak) invalidates every entry without manual flush.
    """
    from oneops.api.cache_version import PIPELINE_CACHE_VERSION

    raw = (
        f"{tenant_id}\x1f{user_id}\x1f{role}\x1f"
        f"{session_id}\x1f{_normalize(message)}\x1f"
        f"v={PIPELINE_CACHE_VERSION}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class ChatTurnCache(Protocol):
    async def get(self, *, tenant_id: str, key: str) -> dict[str, Any] | None: ...
    async def put(self, *, tenant_id: str, key: str, value: Mapping[str, Any]) -> None: ...


# Default TTL bumped from 90s to 600s (10 minutes) on 2026-05-30.
# Rationale: 90s was too short for the realistic chat-followup window —
# users routinely revisit "summarize INC0001001" 2-5 minutes after the
# first ask, and a 90s expiry meant the second call paid full-pipeline
# latency (~4s) while only UC-1's inner cache_aside hit (saving the LLM
# call but still running disambiguator + composer). At 10 minutes,
# realistic follow-ups land sub-20ms. TTL still bounds staleness against
# ticket edits — the embedding-refresh worker invalidates UC-1's inner
# cache on UPDATE, so chat hits stay correct.
_DEFAULT_TTL_S = 600


class InMemoryChatTurnCache:
    """No-infra default. Fine for tests and local dev."""

    def __init__(self, *, ttl_seconds: int = _DEFAULT_TTL_S) -> None:
        self._rows: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._ttl = ttl_seconds

    async def get(self, *, tenant_id: str, key: str) -> dict[str, Any] | None:
        row = self._rows.get((tenant_id, key))
        if row is None:
            return None
        stored_at, value = row
        if time.time() - stored_at > self._ttl:
            self._rows.pop((tenant_id, key), None)
            return None
        return dict(value)

    async def put(
        self, *, tenant_id: str, key: str, value: Mapping[str, Any]
    ) -> None:
        self._rows[(tenant_id, key)] = (time.time(), dict(value))


_PREFIX = "oneops:chat:turn"


class DragonflyChatTurnCache:
    """Production cache over Dragonfly (Redis-protocol)."""

    def __init__(self, client: Any, *, ttl_seconds: int = _DEFAULT_TTL_S) -> None:
        self._redis = client
        self._ttl = ttl_seconds

    @classmethod
    def from_settings(cls, *, ttl_seconds: int | None = None) -> DragonflyChatTurnCache:
        import redis.asyncio as aioredis

        from oneops.config import get_settings
        s = get_settings()
        client = aioredis.from_url(
            getattr(s, "dragonfly_url", "redis://localhost:6379/0"),
            decode_responses=False,
        )
        return cls(client, ttl_seconds=(ttl_seconds if ttl_seconds is not None else _DEFAULT_TTL_S))

    def _k(self, tenant_id: str, key: str) -> str:
        return f"{_PREFIX}:{tenant_id}:{key}"

    async def get(self, *, tenant_id: str, key: str) -> dict[str, Any] | None:
        raw = await self._redis.get(self._k(tenant_id, key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    async def put(
        self, *, tenant_id: str, key: str, value: Mapping[str, Any]
    ) -> None:
        await self._redis.set(
            self._k(tenant_id, key),
            json.dumps(dict(value), default=str).encode("utf-8"),
            ex=self._ttl,
        )


def should_cache(response_dict: Mapping[str, Any]) -> bool:
    """Cache only successful, useful responses. Refusals, clarifications,
    empty replies — and interrupted turns — always re-run on the next turn.

    An `interrupted` turn is STATEFUL: its continuation lives in a per-session
    LangGraph checkpoint. Caching it (or serving it to another session) hands
    back a pause whose checkpoint doesn't exist, so the next turn can't resume
    and falls back through the control gate. Never cache it."""
    status = str(response_dict.get("final_status") or "").lower()
    text = str(response_dict.get("final_response") or "")
    if status in ("clarification", "refused", "error", "interrupted", ""):
        return False
    # A turn carrying an interrupt/offer (e.g. the post-KB "raise a service
    # request?" offer) is stateful follow-up — never cache it, or a later request
    # would be served the answer without its offer, or the offer without context.
    if response_dict.get("interrupt"):
        return False
    # Interactive / stateful flows (UC-8 catalog conductor) must never be
    # cached: their outputs depend on the live catalog + the per-session
    # interrupt checkpoint. A cached "no match" or "SR created" would be wrongly
    # served to the next request, and a cached step would skip the live flow.
    for s in (response_dict.get("step_results") or []):
        if str((s or {}).get("agent_id") or "") == "uc08_fulfillment":
            return False
    if not text or len(text.strip()) < 20:
        return False
    return "out of my scope" not in text.lower()


def build_cache(*, ttl_seconds: int | None = None) -> ChatTurnCache:
    """Pick a backend based on env.

    Default backend is `dragonfly` (production durable cache shared across
    replicas). The docker-compose stack always has dragonfly available, and
    in-memory is selected explicitly only for tests / no-infra dev.

    Fallback discipline: if the configured backend cannot be instantiated
    (Dragonfly unreachable), we fall back to in-memory so the chat path
    keeps working. The downside is per-replica cache, which is acceptable
    while operators investigate the upstream cache outage.
    """
    ttl = ttl_seconds if ttl_seconds is not None else int(
        os.getenv("CHAT_TURN_CACHE_TTL_S", str(_DEFAULT_TTL_S)))
    backend = os.getenv("CHAT_TURN_CACHE_BACKEND", "dragonfly").strip().lower()
    if backend == "dragonfly":
        try:
            return DragonflyChatTurnCache.from_settings(ttl_seconds=ttl)
        except Exception:                                       # noqa: BLE001
            # Fall through to in-memory so the chat path stays alive.
            pass
    return InMemoryChatTurnCache(ttl_seconds=ttl)


__all__ = [
    "ChatTurnCache", "InMemoryChatTurnCache", "DragonflyChatTurnCache",
    "build_cache", "cache_key", "should_cache",
]
