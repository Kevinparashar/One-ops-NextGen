"""AuthzService tests — RBAC+ABAC composition, decision cache, deny-by-default."""
from __future__ import annotations

import time

import pytest

from oneops.authz.decision_cache import InMemoryDecisionCache
from oneops.authz.models import DataClass, Principal, ResourceDescriptor, Tier
from oneops.authz.rbac import RbacResolver
from oneops.authz.service import AuthzService

pytestmark = pytest.mark.asyncio

_RBAC = RbacResolver({
    "service_desk_agent": frozenset({"read:all_tickets", "write:ticket", "create:ticket"}),
    "viewer": frozenset({"read:own_tickets"}),
})


def _service() -> AuthzService:
    return AuthzService(_RBAC, InMemoryDecisionCache(), decision_ttl_seconds=60)


def _principal(role="service_desk_agent", tenant="tenant-a", user="u-1"):
    return Principal(tenant_id=tenant, user_id=user, role=role)


def _resource(tenant="tenant-a", *, tier=Tier.READ, data=DataClass.INTERNAL):
    return ResourceDescriptor(resource_id="uc01", resource_tenant_id=tenant,
                              tier=tier, data_classification=data)


async def test_allowed_access_returns_allow():
    decision = await _service().check(_principal(), _resource())
    assert decision.allowed is True
    assert decision.reasons == ()


async def test_denied_access_returns_deny_with_reasons():
    decision = await _service().check(
        _principal(role="viewer"), _resource(data=DataClass.PII))
    assert decision.allowed is False
    assert decision.reasons                          # non-empty


async def test_unknown_role_denies_by_default():
    decision = await _service().check(
        _principal(role="not_a_real_role"), _resource(tier=Tier.ACTION))
    assert decision.allowed is False


async def test_decision_is_served_from_cache_on_repeat():
    svc = _service()
    first = await svc.check(_principal(), _resource())
    second = await svc.check(_principal(), _resource())
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.effect is first.effect


async def test_cache_distinguishes_principals():
    svc = _service()
    await svc.check(_principal(role="service_desk_agent"), _resource())   # allow, cached
    other = await svc.check(_principal(role="viewer"),
                            _resource(data=DataClass.CONFIDENTIAL))
    # The viewer decision is computed fresh — not the cached agent ALLOW.
    assert other.from_cache is False
    assert other.allowed is False


async def test_cache_distinguishes_resources():
    svc = _service()
    await svc.check(_principal(), _resource(tier=Tier.READ))
    action = await svc.check(_principal(), _resource(tier=Tier.ACTION))
    assert action.from_cache is False                # different resource → fresh


async def test_is_allowed_convenience():
    svc = _service()
    assert await svc.is_allowed(_principal(), _resource()) is True
    assert await svc.is_allowed(_principal(role="viewer"),
                                _resource(tier=Tier.ACTION)) is False


async def test_cache_hit_is_sub_millisecond():
    """Exit criterion: sub-ms p99 on a cache hit. The hit path is a dict
    lookup — measure it to catch a regression that adds I/O to the hot path."""
    svc = _service()
    await svc.check(_principal(), _resource())       # warm the cache
    timings: list[float] = []
    for _ in range(1000):
        t0 = time.perf_counter()
        await svc.check(_principal(), _resource())
        timings.append((time.perf_counter() - t0) * 1000)   # ms
    timings.sort()
    p99 = timings[int(0.99 * len(timings)) - 1]
    assert p99 < 1.0, f"cache-hit p99 was {p99:.3f}ms, budget is 1ms"
