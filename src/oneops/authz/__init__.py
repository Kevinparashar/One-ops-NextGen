"""AuthZ layer (P4) — RBAC + ABAC access decisions, on every boundary.

  * RBAC (`RbacResolver`) — coarse role → permission resolution.
  * ABAC (`evaluate`)     — attribute rules: tenant isolation, audience,
    required scopes, tier, data classification.
  * `AuthzService`        — combines both, with a TTL'd decision cache.
  * Service tokens        — signed HS256 JWTs for internal service identity.

Public surface:
    from oneops.authz import AuthzService, Principal, ResourceDescriptor
    from oneops.authz import from_agent_record, from_tool_record
    from oneops.authz import mint_service_token, verify_service_token
"""
from __future__ import annotations

from oneops.authz.abac import evaluate
from oneops.authz.decision_cache import (
    DEFAULT_DECISION_TTL_SECONDS,
    DecisionCache,
    DragonflyDecisionCache,
    InMemoryDecisionCache,
    decision_key,
)
from oneops.authz.descriptors import from_agent_record, from_tool_record
from oneops.authz.models import (
    AuthzDecision,
    DataClass,
    Effect,
    Principal,
    ResourceDescriptor,
    Tier,
)
from oneops.authz.rbac import ADMIN_PERMISSION, RbacResolver
from oneops.authz.service import AuthzService
from oneops.authz.tokens import (
    DEFAULT_CLOCK_SKEW_LEEWAY_SECONDS,
    DEFAULT_TOKEN_TTL_SECONDS,
    ServiceIdentity,
    mint_service_token,
    verify_service_token,
)

__all__ = [
    "AuthzService",
    "RbacResolver",
    "ADMIN_PERMISSION",
    "evaluate",
    "Principal",
    "ResourceDescriptor",
    "AuthzDecision",
    "Effect",
    "Tier",
    "DataClass",
    "DecisionCache",
    "InMemoryDecisionCache",
    "DragonflyDecisionCache",
    "decision_key",
    "DEFAULT_DECISION_TTL_SECONDS",
    "from_agent_record",
    "from_tool_record",
    "ServiceIdentity",
    "mint_service_token",
    "verify_service_token",
    "DEFAULT_TOKEN_TTL_SECONDS",
    "DEFAULT_CLOCK_SKEW_LEEWAY_SECONDS",
]
