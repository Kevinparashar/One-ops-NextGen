"""ToolContext — the ambient information a handler reads, not the request itself.

Every handler is signed `async def handler(args: ToolArgs, ctx: ToolContext)`.
`args` is the specific work; `ctx` is everything else that's true about this
invocation — tenant, principal, prior authz decision, cache state, trace id.

This is the lock-in that lets POC-5-MW scale to 1000 UCs without per-handler
plumbing: new cross-cutting concerns (e.g. a residency check) become a field
on `ToolContext`, NOT a new parameter on every handler.

Devil's-advocate rules enforced here (validation, not documentation):
  * `principal.tenant_id` MUST equal `tenant.tenant_id` — a mismatch is a
    cross-tenant injection and aborts before the handler runs.
  * `cache_hint.hit=True` MUST carry an `age_s` — silent "stale of unknown
    age" is the failure mode this slot exists to prevent.
  * `tenant_id` is mandatory on `from_request`; everything else defaults
    but the defaults are observable (see `defaulted_fields`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from oneops.authz.models import AuthzDecision, Effect, Principal
from oneops.tenancy.context import (
    DEFAULT_LOCALE,
    DEFAULT_REGION,
    DEFAULT_TIER,
    TenantContext,
    Tier,
)


# ── Cache hint ───────────────────────────────────────────────────────────


class CacheSource(str, Enum):
    """Where a cached value came from. Closed set — read-cache layers
    populate this so the handler / audit knows the provenance class."""

    IDEMPOTENCY = "idempotency"   # P7 runner replay
    READ_CACHE = "read_cache"     # per-handler read-through cache
    NONE = "none"                 # no cache was consulted


class CacheHint(BaseModel):
    """Information about how the data the handler is about to return was
    obtained. Optional on `ToolContext`; populated by a read-through cache
    layer when one is wired (today: none). On idempotency replay, the
    runner does NOT call the handler — so `CacheHint` is unused for that
    path; the existing `ToolResult.from_idempotency_cache` flag covers it.

    `hit=True` REQUIRES `age_s` — see the validator. Anything else is a
    silent staleness leak waiting to happen."""

    model_config = {"frozen": True, "extra": "ignore"}

    hit: bool
    age_s: Optional[int] = Field(default=None, ge=0)
    source: CacheSource = CacheSource.NONE
    fetched_at: Optional[datetime] = None

    @model_validator(mode="after")
    def _hit_requires_age(self) -> "CacheHint":
        if self.hit and self.age_s is None:
            raise ValueError("CacheHint.hit=True requires age_s")
        if not self.hit and self.source is not CacheSource.NONE:
            raise ValueError("CacheHint.hit=False requires source=NONE")
        return self


# ── ToolContext ──────────────────────────────────────────────────────────


_REQUIRED_REQUEST_FIELDS = ("tenant_id", "request_id")


class ToolContext(BaseModel):
    """The ambient context for one tool invocation.

    Frozen. A handler may read every field; it must not mutate `ctx`. To
    record a side-effect (e.g. the handler decided to escalate), the
    handler returns it in its result — never via `ctx`."""

    # extra="ignore" — a richer upstream (Bridge service in microservice
    # mode) may ship additional ambient fields; older consumers ignore them.
    model_config = {
        "frozen": True,
        "arbitrary_types_allowed": True,
        "extra": "ignore",
    }

    tenant: TenantContext
    principal: Principal
    authz: AuthzDecision
    cache_hint: Optional[CacheHint] = None
    request_id: str = Field(min_length=1, max_length=128)
    trace_id: str = Field(default="", max_length=64)
    # Diagnostic: which fields fell through to defaults at construction.
    # Empty in production once upstream is fully wired. Observability emits
    # a warning span attribute when non-empty.
    defaulted_fields: tuple[str, ...] = ()

    @field_validator("request_id")
    @classmethod
    def _request_id_nonblank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("ToolContext.request_id must be non-blank")
        return v

    @model_validator(mode="after")
    def _cross_tenant_guard(self) -> "ToolContext":
        # A Principal's tenant must equal the TenantContext's tenant. If
        # upstream built them from different sources, we abort here BEFORE
        # any handler sees a mixed-tenant ctx.
        if self.principal.tenant_id != self.tenant.tenant_id:
            raise ValueError(
                "ToolContext cross-tenant guard: principal.tenant_id="
                f"{self.principal.tenant_id!r} != tenant.tenant_id="
                f"{self.tenant.tenant_id!r}"
            )
        return self

    # ── Construction helpers ──────────────────────────────────────────

    @classmethod
    def from_request(cls, request: dict[str, Any]) -> "ToolContext":
        """Build a `ToolContext` from an inbound request dict.

        `tenant_id` and `request_id` are MANDATORY — a request that omits
        them is a contract bug and raises. Every other field defaults; the
        `defaulted_fields` tuple records which ones did so observability
        can flag a partially-enriched upstream.

        This is the production path. Tests that want a fully-controlled
        context should build the model directly."""
        missing = [k for k in _REQUIRED_REQUEST_FIELDS if not request.get(k)]
        if missing:
            raise ValueError(
                f"ToolContext.from_request: missing mandatory request fields {missing}"
            )

        tenant_id: str = request["tenant_id"]
        defaulted: list[str] = []

        # ── tenant ────────────────────────────────────────────────────
        tenant_payload = request.get("tenant") or {}
        if tenant_payload:
            tenant = TenantContext(tenant_id=tenant_id, **tenant_payload)
        else:
            tenant = TenantContext(tenant_id=tenant_id)
            defaulted.append("tenant.tier")
            defaulted.append("tenant.region")
            defaulted.append("tenant.locale")

        # ── principal ─────────────────────────────────────────────────
        principal_payload = request.get("principal") or {}
        if principal_payload:
            principal = Principal(
                tenant_id=principal_payload.get("tenant_id", tenant_id),
                user_id=principal_payload["user_id"],
                role=principal_payload["role"],
                attributes=tuple(
                    (k, v) for k, v in (principal_payload.get("attributes") or {}).items()
                ),
            )
        else:
            principal = Principal(
                tenant_id=tenant_id,
                user_id="__system__",
                role="__system__",
            )
            defaulted.append("principal")

        # ── authz ─────────────────────────────────────────────────────
        # Default to ALLOW. Production gates (P10 policy engine) run
        # BEFORE the handler; by the time we reach ToolContext.from_request
        # the gate has already decided. If upstream forwards an authz
        # payload, we honour it; otherwise we default-allow and flag.
        authz_payload = request.get("authz")
        if authz_payload:
            effect = Effect(authz_payload.get("effect", "allow"))
            reasons = tuple(authz_payload.get("reasons") or ())
            authz = (
                AuthzDecision.allow() if effect is Effect.ALLOW
                else AuthzDecision.deny(list(reasons) or ["unspecified"])
            )
        else:
            authz = AuthzDecision.allow()
            defaulted.append("authz")

        # ── cache hint ────────────────────────────────────────────────
        # Populated by a future read-through cache layer; not by the
        # runner (idempotency uses ToolResult.from_idempotency_cache).
        cache_hint = None
        cache_payload = request.get("cache_hint")
        if cache_payload:
            cache_hint = CacheHint(**cache_payload)

        return cls(
            tenant=tenant,
            principal=principal,
            authz=authz,
            cache_hint=cache_hint,
            request_id=request["request_id"],
            trace_id=request.get("trace_id", ""),
            defaulted_fields=tuple(defaulted),
        )


__all__ = [
    "ToolContext",
    "CacheHint",
    "CacheSource",
    # re-exports for convenience
    "TenantContext",
    "Tier",
    "DEFAULT_LOCALE",
    "DEFAULT_REGION",
    "DEFAULT_TIER",
]
