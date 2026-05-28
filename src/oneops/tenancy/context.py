"""TenantContext — the immutable per-request facts about a tenant.

Built once at request ingress (Bridge service / NATS adapter), propagated
through the executor state into every `ToolContext.tenant`. Handlers read
this; they never construct it. Anything tenant-derived (rate limit tier,
data residency, locale-aware rendering) consults this object — never a
loose `tenant_id: str` parameter.

Why frozen Pydantic and not a dataclass:
  - Validators run at construction (BCP-47 locale shape, tier closure,
    region non-empty) — a malformed tenant ctx is impossible by the time
    a handler sees it.
  - Free codec (model_dump / model_validate) for cache + audit.
  - Identical idiom to the registry models — one mental model.
"""
from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field, StrictBool, field_validator

# BCP-47-ish: a 2-3 letter primary tag + optional ISO-3166 region.
# Tight on purpose: the locale string lands in OTel spans, audit, and the
# LLM prompt. Free-form locale text would leak there.
_LOCALE_RE = re.compile(r"^[a-z]{2,3}(-[A-Z]{2})?$")

# Feature flag keys follow snake_case to match the rest of the registry.
_FLAG_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

DEFAULT_LOCALE = "en"
DEFAULT_REGION = "default"


class Tier(str, Enum):
    """Closed set — tier shapes billing, rate limit, model tier, and
    feature_flag overrides. Adding one is a deliberate platform decision,
    not a runtime invention."""

    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


DEFAULT_TIER = Tier.FREE


class TenantContext(BaseModel):
    """Immutable per-request tenant facts.

    Mandatory: `tenant_id`. Everything else has a documented default so a
    request that lands without enriched context still produces a valid
    object (with observable defaults — see the loader in `toolrunner`)."""

    # extra="ignore" — forward-compat: a future tenant attribute (e.g.,
    # `compliance_profile`) can ship in upstream payloads without breaking
    # consumers that haven't been redeployed.
    model_config = {"frozen": True, "extra": "ignore"}

    tenant_id: str = Field(min_length=1, max_length=128)
    tier: Tier = DEFAULT_TIER
    region: str = Field(default=DEFAULT_REGION, min_length=1, max_length=64)
    locale: str = Field(default=DEFAULT_LOCALE, min_length=2, max_length=16)
    # Per-tenant boolean toggles — keys are well-formed, values are bool.
    # The registry/policy layer decides what flags exist; this object only
    # carries the evaluated values.
    # StrictBool: refuse Pydantic's default lax coercion of "yes" / 1 into
    # True. A feature flag is a bool, period — not a "truthy string".
    feature_flags: dict[str, StrictBool] = Field(default_factory=dict)
    # Residency = where the data MUST stay. None = no constraint. A handler
    # that reaches a service outside the residency is a contract bug — the
    # adapter layer is responsible for enforcement; this field carries the
    # constraint and audit evidence.
    residency: str | None = Field(default=None, max_length=64)

    # ── validators ────────────────────────────────────────────────────

    @field_validator("locale")
    @classmethod
    def _locale_shape(cls, v: str) -> str:
        if not _LOCALE_RE.match(v):
            raise ValueError(
                f"TenantContext.locale={v!r} must be BCP-47-ish "
                f"(e.g. 'en', 'en-US', 'fr-CA')"
            )
        return v

    @field_validator("feature_flags")
    @classmethod
    def _flag_keys(cls, v: dict[str, bool]) -> dict[str, bool]:
        for k in v:
            if not _FLAG_KEY_RE.match(k):
                raise ValueError(
                    f"TenantContext.feature_flags key {k!r} must be snake_case "
                    f"(^[a-z][a-z0-9_]{{2,63}}$)"
                )
        return v

    # ── convenience (read-only) ───────────────────────────────────────

    def has_flag(self, key: str) -> bool:
        return bool(self.feature_flags.get(key))


__all__ = [
    "TenantContext",
    "Tier",
    "DEFAULT_LOCALE",
    "DEFAULT_REGION",
    "DEFAULT_TIER",
]
