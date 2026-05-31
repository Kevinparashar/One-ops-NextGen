"""UC-1 AI-summary cache — pluggable backend + `get_cached_summary` /
`put_cached_summary` tool handlers (Component Spec).

A summary is keyed by a content `fingerprint` (the caller is responsible for
the hash recipe — typically over the record's mutable fields). The cache is
tenant-scoped: a fingerprint is namespaced by `tenant_id` so two tenants can
never share an entry, even if their content fingerprints collide.

Spec conformance:
  * C8  — structured output: every handler returns a typed dict with an
          explicit `outcome` + `message`; never a bare value.
  * C10 — deterministic: pure key/value lookup; no LLM.
  * C13 — tenant-scoped: tenant_id from the request envelope, never user text.
  * C17 — no silent failure: miss / invalid_request are explicit outcomes,
          never `None` returns that look the same as a missing key.
  * C21 — pluggable backend: data access through `SummaryCacheStore`
          (in-memory default; live Dragonfly env-gated, not built yet).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from oneops.observability import get_logger

_log = get_logger("oneops.use_cases.uc01.cache")


# ── backend ──────────────────────────────────────────────────────────────


@runtime_checkable
class SummaryCacheStore(Protocol):
    """Tenant-scoped read-through cache for generated summaries."""

    async def get(
        self, *, fingerprint: str, tenant_id: str
    ) -> dict[str, Any] | None: ...

    async def put(
        self, *, fingerprint: str, tenant_id: str, summary: dict[str, Any]
    ) -> None: ...


class InMemorySummaryCacheStore:
    """Deterministic in-process cache. The no-infra default."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    def clear(self) -> None:
        self._rows.clear()

    async def get(
        self, *, fingerprint: str, tenant_id: str
    ) -> dict[str, Any] | None:
        row = self._rows.get((tenant_id, fingerprint))
        return dict(row) if row is not None else None

    async def put(
        self, *, fingerprint: str, tenant_id: str, summary: dict[str, Any]
    ) -> None:
        self._rows[(tenant_id, fingerprint)] = {
            "summary": dict(summary),
            "cached_at": time.time(),
        }


_DRAGONFLY_KEY_PREFIX = "oneops:uc01:summary"


def _dragonfly_key(tenant_id: str, fingerprint: str) -> str:
    """Tenant-prefixed key — a consumer scoped to one tenant can never
    read another's summary by construction."""
    if not tenant_id or not fingerprint:
        raise ValueError("tenant_id and fingerprint are mandatory")
    return f"{_DRAGONFLY_KEY_PREFIX}:{tenant_id}:{fingerprint}"


class DragonflySummaryCacheStore:
    """Production summary cache over a Redis-protocol Dragonfly cluster.

    Selected by `ONEOPS_SUMMARY_CACHE_BACKEND=dragonfly`. The client is
    constructed from `DRAGONFLY_URL` lazily on first access (cold-start
    friendly), or injected directly for tests.

    Key shape: `oneops:uc01:summary:{tenant_id}:{fingerprint}`. The
    fingerprint already encodes `(tenant, service, entity, content_hash)`,
    so a content mutation rotates the key and the next read is a cache
    miss — automatic invalidation, no flush needed.

    TTL: configurable (default 3600s). The cache-aside `cached_at` field
    is preserved inside the value, so the age the handler surfaces is the
    *actual* write-time of the entry, not "now-TTL".

    Failure mode contract: the cache-aside wrapper expects `get` / `put`
    to either succeed or RAISE. A raise on read → wrapper falls through
    to the LLM (the user still gets an answer). A raise on write → LLM
    result still returned (write failure is non-fatal upstream).
    """

    def __init__(self, client: Any, *, ttl_seconds: int = 3600) -> None:
        self._redis = client
        self._ttl = ttl_seconds

    @classmethod
    def from_settings(cls, *, ttl_seconds: int | None = None) -> DragonflySummaryCacheStore:
        """Build over a client constructed from `DRAGONFLY_URL`."""
        import redis.asyncio as aioredis

        from oneops.config import get_settings
        settings = get_settings()
        client = aioredis.from_url(
            getattr(settings, "dragonfly_url", "redis://localhost:6379/0"),
            decode_responses=False,
        )
        return cls(
            client,
            ttl_seconds=(ttl_seconds
                         if ttl_seconds is not None
                         else int(getattr(settings,
                                          "cache_default_ttl_seconds", 3600))),
        )

    async def get(
        self, *, fingerprint: str, tenant_id: str,
    ) -> dict[str, Any] | None:
        if not tenant_id or not fingerprint:
            return None
        import json
        raw = await self._redis.get(_dragonfly_key(tenant_id, fingerprint))
        if raw is None:
            return None
        # The bytes/str round-trip depends on the client's
        # `decode_responses` flag. JSON parser handles both.
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    async def put(
        self, *, fingerprint: str, tenant_id: str, summary: dict[str, Any],
    ) -> None:
        if not tenant_id:
            raise ValueError("DragonflySummaryCacheStore.put: tenant_id mandatory")
        if not fingerprint:
            raise ValueError("DragonflySummaryCacheStore.put: fingerprint mandatory")
        import json
        import time
        value = {
            "summary": dict(summary),
            "cached_at": time.time(),
        }
        await self._redis.setex(
            _dragonfly_key(tenant_id, fingerprint),
            self._ttl,
            json.dumps(value, default=str),
        )


_store: SummaryCacheStore | None = None


def _build_default() -> SummaryCacheStore:
    backend = os.getenv("ONEOPS_SUMMARY_CACHE_BACKEND", "memory").strip().lower()
    if backend == "dragonfly":
        _log.info("summary_cache.backend_selected", backend="dragonfly")
        return DragonflySummaryCacheStore.from_settings()
    _log.info("summary_cache.backend_selected", backend="memory")
    return InMemorySummaryCacheStore()


def get_summary_cache_store() -> SummaryCacheStore:
    global _store
    if _store is None:
        _store = _build_default()
    return _store


def set_summary_cache_store(store: SummaryCacheStore) -> None:
    """Replace the process-wide cache — for tests and FaaS wiring."""
    global _store
    _store = store


# ── handler results ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class CacheGetResult:
    outcome: str          # "hit" | "miss" | "invalid_request"
    fingerprint: str
    message: str
    summary: dict[str, Any] | None = None
    age_s: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "fingerprint": self.fingerprint,
            "message": self.message,
            "summary": self.summary,
            "age_s": self.age_s,
        }


@dataclass(frozen=True)
class CachePutResult:
    outcome: str          # "stored" | "invalid_request"
    fingerprint: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "fingerprint": self.fingerprint,
            "message": self.message,
        }


# ── handlers ─────────────────────────────────────────────────────────────


async def get_cached_summary(
    arguments: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Read a summary from the cache by `fingerprint`. Tenant-scoped."""
    fingerprint = str(arguments.get("fingerprint") or "").strip()
    tenant_id = str(context.get("tenant_id") or "").strip()

    if not fingerprint:
        return CacheGetResult(
            outcome="invalid_request", fingerprint=fingerprint,
            message="A content fingerprint is required to read the cache.",
        ).to_dict()
    if not tenant_id:
        return CacheGetResult(
            outcome="invalid_request", fingerprint=fingerprint,
            message="No tenant scope was supplied for this request.",
        ).to_dict()

    row = await get_summary_cache_store().get(
        fingerprint=fingerprint, tenant_id=tenant_id)
    if row is None:
        _log.info("uc01.cache.miss", fingerprint=fingerprint)
        return CacheGetResult(
            outcome="miss", fingerprint=fingerprint,
            message="No cached summary for this fingerprint.",
        ).to_dict()

    cached_at = float(row.get("cached_at") or 0.0)
    age = max(0, int(time.time() - cached_at)) if cached_at else None
    return CacheGetResult(
        outcome="hit", fingerprint=fingerprint,
        message="Cached summary returned.",
        summary=row.get("summary"), age_s=age,
    ).to_dict()


async def put_cached_summary(
    arguments: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Write a summary to the cache under `fingerprint`. Tenant-scoped.
    Idempotent — same fingerprint + tenant overwrites."""
    fingerprint = str(arguments.get("fingerprint") or "").strip()
    tenant_id = str(context.get("tenant_id") or "").strip()
    summary = arguments.get("summary")

    if not fingerprint:
        return CachePutResult(
            outcome="invalid_request", fingerprint=fingerprint,
            message="A content fingerprint is required to write the cache.",
        ).to_dict()
    if not tenant_id:
        return CachePutResult(
            outcome="invalid_request", fingerprint=fingerprint,
            message="No tenant scope was supplied for this request.",
        ).to_dict()
    if not isinstance(summary, dict) or not summary:
        return CachePutResult(
            outcome="invalid_request", fingerprint=fingerprint,
            message="A non-empty summary object is required.",
        ).to_dict()

    await get_summary_cache_store().put(
        fingerprint=fingerprint, tenant_id=tenant_id, summary=summary)
    _log.info("uc01.cache.stored", fingerprint=fingerprint)
    return CachePutResult(
        outcome="stored", fingerprint=fingerprint,
        message="Summary cached.",
    ).to_dict()


__all__ = [
    "SummaryCacheStore",
    "InMemorySummaryCacheStore",
    "DragonflySummaryCacheStore",
    "get_summary_cache_store",
    "set_summary_cache_store",
    "CacheGetResult",
    "CachePutResult",
    "get_cached_summary",
    "put_cached_summary",
]
