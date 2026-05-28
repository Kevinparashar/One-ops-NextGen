"""Policy engine (P10, ADR-0003) — embedded, data-driven, hot-reloadable.

Policy is structured data; `PolicyEngine` evaluates it deterministically.
A policy change is a data deploy + `reload()` — never a code change. Compliance
touchpoints return a pre-approved `CANNED` response (zero hallucination).

Public surface:
    from oneops.policy_engine import PolicyEngine, PolicyQuery, PolicyDecision
    from oneops.policy_engine import PolicyEffect, PolicyRule, PolicyMatch
"""
from __future__ import annotations

from oneops.policy_engine.engine import PolicyEngine
from oneops.policy_engine.models import (
    PolicyDecision,
    PolicyEffect,
    PolicyMatch,
    PolicyQuery,
    PolicyRule,
)

__all__ = [
    "PolicyEngine",
    "PolicyQuery",
    "PolicyDecision",
    "PolicyEffect",
    "PolicyRule",
    "PolicyMatch",
]
