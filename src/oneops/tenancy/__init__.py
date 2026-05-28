"""Tenancy — the immutable per-request tenant facts.

`TenantContext` is the single object every handler reads to know who it is
serving. Locking this shape now (before the UC-1/UC-3 port) means the next
998 use cases inherit it for free — no per-handler `tenant_id: str` thread
to refactor later.

Design influences:
  * AgentScript — context is data, propagated as a frozen object; the
    handler treats it as read-only ambient state.
  * Moveworks — per-tenant tier/locale/feature_flags drive behaviour
    without per-handler code branching.
  * Salesforce — TenantContext mirrors the Apex `User Context` pattern —
    tier/region/permissions arrive together, not piecemeal.
"""
from __future__ import annotations

from oneops.tenancy.context import (
    DEFAULT_LOCALE,
    DEFAULT_REGION,
    DEFAULT_TIER,
    TenantContext,
    Tier,
)

__all__ = [
    "TenantContext",
    "Tier",
    "DEFAULT_LOCALE",
    "DEFAULT_REGION",
    "DEFAULT_TIER",
]
