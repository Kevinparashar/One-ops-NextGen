"""AuthzService — the access-decision entry point.

`check(principal, resource)` resolves the principal's RBAC permissions,
evaluates the ABAC rules, and returns an `AuthzDecision`. Decisions are cached
(decision_cache.py) so a repeated check is a single keyed lookup — the
sub-millisecond p99 the P4 exit criterion requires.

Deny-by-default: an unknown role resolves to an empty permission set, so every
scope / tier / data-class rule it touches fails closed. There is no path where
a missing role, a cache error, or an unexpected input yields an ALLOW.
"""
from __future__ import annotations

from oneops.authz.abac import evaluate
from oneops.authz.decision_cache import (
    DEFAULT_DECISION_TTL_SECONDS,
    DecisionCache,
    InMemoryDecisionCache,
    decision_key,
)
from oneops.authz.models import AuthzDecision, Principal, ResourceDescriptor
from oneops.authz.rbac import RbacResolver
from oneops.observability import get_logger, get_tracer

_log = get_logger("oneops.authz.service")
_tracer = get_tracer("oneops.authz.service")


class AuthzService:
    """RBAC + ABAC decision service with a TTL'd decision cache."""

    def __init__(
        self,
        rbac: RbacResolver,
        cache: DecisionCache,
        *,
        decision_ttl_seconds: int = DEFAULT_DECISION_TTL_SECONDS,
    ) -> None:
        self._rbac = rbac
        self._cache = cache
        self._ttl = decision_ttl_seconds

    @classmethod
    def create(cls) -> "AuthzService":
        """Default wiring — RBAC from the role registry, in-process cache.
        Production swaps the cache for `DragonflyDecisionCache` (shared across
        workers); the `check()` contract does not change."""
        return cls(RbacResolver.from_registry_file(), InMemoryDecisionCache())

    async def check(self, principal: Principal, resource: ResourceDescriptor) -> AuthzDecision:
        """Authorize `principal` to act on `resource`. Cached; deny-by-default."""
        key = decision_key(principal, resource)
        with _tracer.start_as_current_span(
            "authz.check",
            attributes={
                "oneops.tenant_id": principal.tenant_id,
                "authz.role": principal.role,
                "authz.resource_id": resource.resource_id,
                "authz.tier": resource.tier.value,
            },
        ) as span:
            cached = await self._cache.get(key)
            if cached is not None:
                span.set_attribute("authz.cache_hit", True)
                span.set_attribute("authz.effect", cached.effect.value)
                return cached
            span.set_attribute("authz.cache_hit", False)

            granted = self._rbac.permissions_for(principal.role)
            reasons = evaluate(principal, resource, granted)
            decision = (
                AuthzDecision.allow() if not reasons
                else AuthzDecision.deny(reasons)
            )
            await self._cache.put(key, decision, ttl_seconds=self._ttl)

            span.set_attribute("authz.effect", decision.effect.value)
            if not decision.allowed:
                span.set_attribute("authz.deny_reason_count", len(decision.reasons))
                _log.info(
                    "authz.denied",
                    tenant_id=principal.tenant_id, role=principal.role,
                    resource_id=resource.resource_id, reasons=list(decision.reasons),
                )
            return decision

    async def is_allowed(self, principal: Principal, resource: ResourceDescriptor) -> bool:
        """Boolean convenience over `check()`."""
        return (await self.check(principal, resource)).allowed


__all__ = ["AuthzService"]
