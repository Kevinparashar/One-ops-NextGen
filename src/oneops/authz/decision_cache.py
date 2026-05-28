"""AuthZ decision cache — pluggable, TTL'd.

An AuthZ decision is a pure function of (Principal, ResourceDescriptor) for the
life of the cache entry, so it caches cleanly. The cache turns the hot path
into a single keyed lookup — the sub-millisecond p99 the exit criterion needs.

`DecisionCache` is a Protocol; `InMemoryDecisionCache` and
`DragonflyDecisionCache` are real implementations. The TTL bounds staleness —
a role/permission change takes effect within one TTL without an explicit
invalidation hook (acceptable for coarse RBAC/ABAC; a future revocation event
can call `invalidate()` for immediacy).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Protocol

from oneops.authz.models import AuthzDecision, Effect, Principal, ResourceDescriptor

# Default decision TTL — short; AuthZ correctness over cache longevity.
DEFAULT_DECISION_TTL_SECONDS = 60


def decision_key(principal: Principal, resource: ResourceDescriptor) -> str:
    """A stable cache key for one (principal, resource) pair. Every field that
    can change the decision is in the digest — nothing is omitted."""
    payload = {
        "p_tenant": principal.tenant_id,
        "p_user": principal.user_id,
        "p_role": principal.role,
        "p_attrs": sorted(principal.attributes),
        "r_id": resource.resource_id,
        "r_tenant": resource.resource_tenant_id,
        "r_tier": resource.tier.value,
        "r_data": resource.data_classification.value,
        "r_audience": sorted(resource.audience),
        "r_scopes": sorted(resource.required_scopes),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "authz:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _encode(decision: AuthzDecision) -> str:
    return json.dumps({"effect": decision.effect.value, "reasons": list(decision.reasons)})


def _decode(raw: str) -> AuthzDecision:
    doc = json.loads(raw)
    effect = Effect(doc["effect"])
    reasons = tuple(doc.get("reasons", []))
    return AuthzDecision(effect, reasons, from_cache=True)


class DecisionCache(Protocol):
    """TTL'd store of AuthZ decisions, keyed by `decision_key()`."""

    async def get(self, key: str) -> AuthzDecision | None: ...
    async def put(self, key: str, decision: AuthzDecision, *, ttl_seconds: int) -> None: ...
    async def invalidate(self, key: str) -> None: ...


class InMemoryDecisionCache:
    """Thread-safe in-process decision cache. Real implementation; the unit
    suite runs against it. A dict lookup — the sub-ms hot path."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._store: dict[str, tuple[AuthzDecision, float]] = {}

    async def get(self, key: str) -> AuthzDecision | None:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            decision, expires_at = entry
            if now >= expires_at:
                del self._store[key]                # lazy expiry
                return None
            return decision.with_cache_flag()

    async def put(self, key: str, decision: AuthzDecision, *, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        with self._lock:
            self._store[key] = (decision, time.monotonic() + ttl_seconds)

    async def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)


class DragonflyDecisionCache:
    """Decision cache backed by Dragonfly (Redis protocol) — shared across
    worker processes. Exercised only in the env-gated integration suite."""

    def __init__(self, client: Any) -> None:
        self._redis = client

    async def get(self, key: str) -> AuthzDecision | None:
        raw = await self._redis.get(key)
        if raw is None:
            return None
        return _decode(raw.decode("utf-8") if isinstance(raw, bytes) else raw)

    async def put(self, key: str, decision: AuthzDecision, *, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        await self._redis.set(key, _encode(decision), ex=ttl_seconds)

    async def invalidate(self, key: str) -> None:
        await self._redis.delete(key)


__all__ = [
    "DecisionCache",
    "InMemoryDecisionCache",
    "DragonflyDecisionCache",
    "decision_key",
    "DEFAULT_DECISION_TTL_SECONDS",
]
