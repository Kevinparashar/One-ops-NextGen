"""Policy-engine value objects (P10, ADR-0003).

Policy is **data** — `PolicyRule`s loaded from a structured file. A rule's
`match` says when it applies; its `effect` is the verdict. `PolicyEngine`
evaluates a `PolicyQuery` against the ruleset and returns a `PolicyDecision`.

The three effects:
  * `ALLOW`  — proceed.
  * `DENY`   — refuse; `reason` says why.
  * `CANNED` — replace any LLM-drafted output with `canned_response` verbatim.
    This is the Parlant "canned response at a critical moment" — zero
    hallucination at compliance / legal / PII touchpoints.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PolicyEffect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    CANNED = "canned"


@dataclass(frozen=True)
class PolicyMatch:
    """When a rule applies. Every field is a set of accepted values; an empty
    set means "any". A rule matches a query when *every* non-empty field
    contains the query's corresponding value."""

    roles: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    data_classifications: tuple[str, ...] = ()
    surfaces: tuple[str, ...] = ()
    intents: tuple[str, ...] = ()

    def matches(self, query: "PolicyQuery") -> bool:
        def ok(accepted: tuple[str, ...], value: str | None) -> bool:
            return not accepted or (value is not None and value in accepted)
        return (
            ok(self.roles, query.role)
            and ok(self.actions, query.action)
            and ok(self.data_classifications, query.data_classification)
            and ok(self.surfaces, query.surface)
            and ok(self.intents, query.intent)
        )


@dataclass(frozen=True)
class PolicyRule:
    """One policy rule. Higher `priority` wins on a conflict."""

    id: str
    description: str
    match: PolicyMatch
    effect: PolicyEffect
    reason: str = ""
    canned_response: str = ""
    priority: int = 0

    def __post_init__(self) -> None:
        if self.effect is PolicyEffect.CANNED and not self.canned_response:
            raise ValueError(f"rule '{self.id}': effect=canned requires a canned_response")


@dataclass(frozen=True)
class PolicyQuery:
    """The facts a policy decision is made over — built at a touchpoint
    (a step about to run, a response about to be returned)."""

    tenant_id: str
    role: str | None = None
    action: str | None = None
    data_classification: str | None = None
    surface: str | None = None
    intent: str | None = None


@dataclass(frozen=True)
class PolicyDecision:
    """The engine's verdict. `matched_rule_id` is empty for the default allow."""

    effect: PolicyEffect
    reason: str = ""
    canned_response: str = ""
    matched_rule_id: str = ""
    policy_version: str = ""

    @property
    def allowed(self) -> bool:
        return self.effect is PolicyEffect.ALLOW

    @property
    def is_canned(self) -> bool:
        return self.effect is PolicyEffect.CANNED


__all__ = [
    "PolicyEffect",
    "PolicyMatch",
    "PolicyRule",
    "PolicyQuery",
    "PolicyDecision",
]
