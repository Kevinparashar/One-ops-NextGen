"""Contract tests for ToolContext + CacheHint — the per-invocation ambient state.

Devil's-advocate coverage:
  * cross-tenant: principal vs tenant mismatch must abort BEFORE the handler
  * cache-hint with hit=True but no age → reject (silent-staleness guard)
  * from_request without required fields → typed raise
  * defaulted-fields surface so observability can see partial enrichment
  * frozenness blocks tampering after construction
  * runner integration: the handler sees the same ctx that was passed in
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from oneops.authz.models import AuthzDecision, Principal
from oneops.registry.models import (
    ActivationCondition,
    ConditionOperator,
    ConditionSignal,
    ExecutionTier,
    ToolRecord,
)
from oneops.tenancy import TenantContext, Tier
from oneops.toolrunner.context import (
    CacheHint,
    CacheSource,
    ToolContext,
)
from oneops.toolrunner.resolver import HandlerResolver
from oneops.toolrunner.runner import ToolRunner


def _tool(handler_ref: str = "reg:h", timeout_ms: int = 30_000) -> ToolRecord:
    return ToolRecord(
        id="tool_one", version=1, owner="team-test", description="A test tool.",
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF,
            signal=ConditionSignal.INTENT_IN, values=("x",)),
        handler_ref=handler_ref, execution_type=ExecutionTier.READ,
        timeout_ms=timeout_ms, idempotent=True)


# ── CacheHint ─────────────────────────────────────────────────────────


def test_cache_hint_miss_constructs_cleanly():
    h = CacheHint(hit=False)
    assert h.hit is False
    assert h.age_s is None
    assert h.source is CacheSource.NONE


def test_cache_hint_hit_requires_age():
    """Devil's advocate: 'we know it's cached but we don't know how old'
    is a silent-staleness leak — reject."""
    with pytest.raises(ValidationError):
        CacheHint(hit=True, age_s=None, source=CacheSource.READ_CACHE)


def test_cache_hint_hit_with_age_and_source_ok():
    h = CacheHint(
        hit=True, age_s=240,
        source=CacheSource.READ_CACHE,
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert h.age_s == 240
    assert h.source is CacheSource.READ_CACHE


def test_cache_hint_miss_must_have_source_none():
    """Mismatched: hit=False but source=READ_CACHE is incoherent."""
    with pytest.raises(ValidationError):
        CacheHint(hit=False, source=CacheSource.READ_CACHE)


def test_cache_hint_age_must_be_non_negative():
    with pytest.raises(ValidationError):
        CacheHint(hit=True, age_s=-1, source=CacheSource.READ_CACHE)


# ── ToolContext direct construction ─────────────────────────────────


def _principal(tenant_id: str = "acme", user_id: str = "u1", role: str = "agent") -> Principal:
    return Principal(tenant_id=tenant_id, user_id=user_id, role=role)


def test_minimal_construction():
    ctx = ToolContext(
        tenant=TenantContext(tenant_id="acme"),
        principal=_principal("acme"),
        authz=AuthzDecision.allow(),
        request_id="r-1",
    )
    assert ctx.tenant.tenant_id == "acme"
    assert ctx.authz.allowed
    assert ctx.cache_hint is None
    assert ctx.defaulted_fields == ()


def test_cross_tenant_guard_rejects_mismatch():
    """Devil's advocate: a Principal from tenant-A wrapped in a
    TenantContext of tenant-B is a cross-tenant injection. Abort before
    the handler runs."""
    with pytest.raises(ValidationError) as exc:
        ToolContext(
            tenant=TenantContext(tenant_id="acme"),
            principal=_principal(tenant_id="evil-corp"),
            authz=AuthzDecision.allow(),
            request_id="r-1",
        )
    assert "cross-tenant guard" in str(exc.value)


def test_blank_request_id_rejected():
    with pytest.raises(ValidationError):
        ToolContext(
            tenant=TenantContext(tenant_id="acme"),
            principal=_principal("acme"),
            authz=AuthzDecision.allow(),
            request_id="   ",
        )


def test_frozen_blocks_field_mutation():
    ctx = ToolContext(
        tenant=TenantContext(tenant_id="acme"),
        principal=_principal("acme"),
        authz=AuthzDecision.allow(),
        request_id="r-1",
    )
    with pytest.raises(ValidationError):
        ctx.request_id = "tampered"        # type: ignore[misc]


def test_carries_a_deny_decision_without_raising():
    """A DENY upstream is informative — the handler may still need to
    respond (e.g. UC-1 returns a redacted-row summary). ToolContext
    carries the decision; it does not gate execution itself."""
    deny = AuthzDecision.deny(["insufficient_role"])
    ctx = ToolContext(
        tenant=TenantContext(tenant_id="acme"),
        principal=_principal("acme"),
        authz=deny,
        request_id="r-1",
    )
    assert ctx.authz.allowed is False
    assert "insufficient_role" in ctx.authz.reasons


# ── from_request ────────────────────────────────────────────────────


def test_from_request_minimal():
    ctx = ToolContext.from_request({"tenant_id": "acme", "request_id": "r-1"})
    assert ctx.tenant.tenant_id == "acme"
    assert ctx.request_id == "r-1"
    # Defaulted fields surface to observability.
    defaulted = set(ctx.defaulted_fields)
    assert "principal" in defaulted
    assert "authz" in defaulted
    assert any(f.startswith("tenant.") for f in defaulted)


def test_from_request_full_payload():
    req = {
        "tenant_id": "acme",
        "request_id": "r-1",
        "trace_id": "trace-42",
        "tenant": {
            "tier": "enterprise",
            "region": "eu-west-1",
            "locale": "de-DE",
            "feature_flags": {"experimental_summary": True},
            "residency": "EU",
        },
        "principal": {
            "user_id": "u1",
            "role": "agent",
            "attributes": {"department": "ops"},
        },
        "authz": {"effect": "allow"},
    }
    ctx = ToolContext.from_request(req)
    assert ctx.tenant.tier is Tier.ENTERPRISE
    assert ctx.tenant.region == "eu-west-1"
    assert ctx.tenant.locale == "de-DE"
    assert ctx.tenant.has_flag("experimental_summary")
    assert ctx.principal.user_id == "u1"
    assert ctx.principal.attr("department") == "ops"
    assert ctx.authz.allowed
    assert ctx.trace_id == "trace-42"
    assert ctx.defaulted_fields == ()


def test_from_request_missing_tenant_id_raises():
    with pytest.raises(ValueError, match="tenant_id"):
        ToolContext.from_request({"request_id": "r-1"})


def test_from_request_missing_request_id_raises():
    with pytest.raises(ValueError, match="request_id"):
        ToolContext.from_request({"tenant_id": "acme"})


def test_from_request_propagates_principal_tenant_mismatch_via_guard():
    """A request that ships a principal with a different tenant_id than
    the envelope: from_request honours principal.tenant_id verbatim, the
    cross-tenant guard then aborts."""
    req = {
        "tenant_id": "acme",
        "request_id": "r-1",
        "principal": {"tenant_id": "evil-corp", "user_id": "u1", "role": "agent"},
    }
    with pytest.raises(ValidationError) as exc:
        ToolContext.from_request(req)
    assert "cross-tenant guard" in str(exc.value)


def test_from_request_deny_authz_payload():
    req = {
        "tenant_id": "acme",
        "request_id": "r-1",
        "authz": {"effect": "deny", "reasons": ["audience_mismatch"]},
    }
    ctx = ToolContext.from_request(req)
    assert ctx.authz.allowed is False
    assert ctx.authz.reasons == ("audience_mismatch",)


def test_from_request_with_cache_hint_payload():
    req = {
        "tenant_id": "acme",
        "request_id": "r-1",
        "cache_hint": {"hit": True, "age_s": 60, "source": "read_cache"},
    }
    ctx = ToolContext.from_request(req)
    assert ctx.cache_hint
    assert ctx.cache_hint.hit is True
    assert ctx.cache_hint.age_s == 60
    assert ctx.cache_hint.source is CacheSource.READ_CACHE


def test_from_request_extra_keys_ignored():
    """Forward-compat: an upstream that ships extra fields must not
    break the loader (additive evolution rule, ADR-0001 codec spirit)."""
    ctx = ToolContext.from_request({
        "tenant_id": "acme",
        "request_id": "r-1",
        "future_field_we_dont_know_about": {"x": 1},
    })
    assert ctx.tenant.tenant_id == "acme"


# ── runner integration: ctx reaches the handler intact ─────────────


async def test_handler_receives_typed_tool_context():
    seen: dict[str, object] = {}

    async def handler(args, ctx):
        seen["ctx"] = ctx
        return {"ok": True}

    resolver = HandlerResolver()
    resolver.register("reg:h", handler)
    runner = ToolRunner(resolver)

    ctx_in = ToolContext.from_request({"tenant_id": "acme", "request_id": "r-1"})
    await runner.run(_tool(), {}, context=ctx_in)

    assert isinstance(seen["ctx"], ToolContext)
    assert seen["ctx"] is ctx_in                          # passed through, not copied
    assert seen["ctx"].tenant.tenant_id == "acme"


async def test_runner_rejects_non_context_object():
    """Devil's advocate: a caller passes a dict (the old, untyped shape).
    The runner must refuse — locking the substrate is the whole point."""
    resolver = HandlerResolver()

    async def handler(args, ctx):
        return {"ok": True}

    resolver.register("reg:h", handler)
    runner = ToolRunner(resolver)
    with pytest.raises(TypeError):
        await runner.run(_tool(), {}, context={"tenant_id": "acme"})  # type: ignore[arg-type]
