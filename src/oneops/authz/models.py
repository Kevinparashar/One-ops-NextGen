"""AuthZ value objects — the inputs and output of an access decision.

A decision is `check(Principal, ResourceDescriptor) -> AuthzDecision`.

  * `Principal` — *who* is asking: tenant, user, role, free-form attributes.
    Always built from a validated request envelope, never from user text.
  * `ResourceDescriptor` — *what* is being accessed: the access-control facts
    of a registry agent/tool — its tenant, audience, tier, data class,
    required scopes. Built from an `AgentRecord` / `ToolRecord`'s `abac_tags`.
  * `AuthzDecision` — ALLOW or DENY, with the full reason list on a DENY.

All three are frozen — a decision input cannot be mutated after it is built,
so a cached decision can never be invalidated by a caller mutating its key.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Effect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Tier(StrEnum):
    READ = "read"
    ACTION = "action"


class DataClass(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    PII = "pii"


@dataclass(frozen=True)
class Principal:
    """The caller. Identity comes from the validated envelope / service JWT."""

    tenant_id: str
    user_id: str
    role: str
    # Free-form ABAC attributes (clearance, department, location, ...).
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("Principal.tenant_id is mandatory")
        if not self.role:
            raise ValueError("Principal.role is mandatory")

    def attr(self, key: str) -> str | None:
        for k, v in self.attributes:
            if k == key:
                return v
        return None


@dataclass(frozen=True)
class ResourceDescriptor:
    """The access-control facts of the thing being invoked. Mirrors a registry
    `AbacTags` plus the owning tenant and any tool `required_scopes`."""

    resource_id: str                       # agent_id or tool_id — for audit
    resource_tenant_id: str                # tenant the resource/data belongs to
    tier: Tier
    data_classification: DataClass = DataClass.INTERNAL
    audience: tuple[str, ...] = ()         # roles permitted; () = no role gate
    required_scopes: tuple[str, ...] = ()  # permissions the caller must hold

    def __post_init__(self) -> None:
        if not self.resource_tenant_id:
            raise ValueError("ResourceDescriptor.resource_tenant_id is mandatory")


@dataclass(frozen=True)
class AuthzDecision:
    """The outcome. On DENY, `reasons` lists every failed rule (not just the
    first) so an operator sees the whole picture."""

    effect: Effect
    reasons: tuple[str, ...] = ()
    from_cache: bool = False

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW

    @staticmethod
    def allow(*, from_cache: bool = False) -> AuthzDecision:
        return AuthzDecision(Effect.ALLOW, (), from_cache)

    @staticmethod
    def deny(reasons: list[str], *, from_cache: bool = False) -> AuthzDecision:
        if not reasons:
            raise ValueError("a DENY decision must carry at least one reason")
        return AuthzDecision(Effect.DENY, tuple(reasons), from_cache)

    def with_cache_flag(self) -> AuthzDecision:
        """Return a copy marked as served from cache."""
        return AuthzDecision(self.effect, self.reasons, True)


__all__ = [
    "Effect", "Tier", "DataClass",
    "Principal", "ResourceDescriptor", "AuthzDecision",
]
