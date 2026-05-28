"""ABAC — attribute-level access evaluation.

`evaluate()` is a **pure, deterministic** function: given a `Principal`, a
`ResourceDescriptor`, and the principal's RBAC-granted permissions, it returns
the list of denial reasons. An empty list means allow. It collects *every*
violation, not just the first — an operator debugging a denial sees all of it.

No I/O, no LLM, no state — routing/validation logic out of the model and into
deterministic code (Moveworks). The five rules, in order:

  1. Tenant     — cross-tenant access is denied unconditionally. The strongest
                  rule; isolation is not a check that can be relaxed.
  2. Audience   — if the resource declares an audience, the role must be in it.
  3. Scopes     — every required scope must be a held permission (admin covers).
  4. Tier       — an action-tier resource requires a write-class permission.
  5. Data class — confidential / PII resources require tenant-wide read or admin
                  clearance; own-records-only roles cannot reach them.
"""
from __future__ import annotations

from oneops.authz.models import DataClass, Principal, ResourceDescriptor, Tier
from oneops.authz.rbac import ADMIN_PERMISSION

# Permission prefixes that count as "write-class" for the tier-4 rule.
_WRITE_PREFIXES = ("write:", "approve:", "create:")
# Permissions that grant visibility of confidential / PII data (rule 5).
_BROAD_READ = "read:all_tickets"


def _is_write_class(permission: str) -> bool:
    return permission == ADMIN_PERMISSION or permission.startswith(_WRITE_PREFIXES)


def evaluate(
    principal: Principal,
    resource: ResourceDescriptor,
    granted_permissions: frozenset[str],
) -> list[str]:
    """Return every denial reason for this access. Empty list == allow."""
    reasons: list[str] = []
    has_admin = ADMIN_PERMISSION in granted_permissions

    # ── Rule 1 — tenant isolation (unconditional) ────────────────────────
    if principal.tenant_id != resource.resource_tenant_id:
        reasons.append(
            f"cross-tenant access denied: principal tenant '{principal.tenant_id}' "
            f"!= resource tenant '{resource.resource_tenant_id}'"
        )
        # Tenant mismatch short-circuits — no further rule can rescue it, and
        # evaluating role/scope across a tenant boundary is itself unsafe.
        return reasons

    # ── Rule 2 — audience ────────────────────────────────────────────────
    if resource.audience and principal.role not in resource.audience:
        reasons.append(
            f"role '{principal.role}' is not in resource audience "
            f"{sorted(resource.audience)}"
        )

    # ── Rule 3 — required scopes ─────────────────────────────────────────
    if not has_admin:
        missing = [s for s in resource.required_scopes if s not in granted_permissions]
        if missing:
            reasons.append(f"missing required scope(s): {sorted(missing)}")

    # ── Rule 4 — tier ────────────────────────────────────────────────────
    if resource.tier is Tier.ACTION:
        if not any(_is_write_class(p) for p in granted_permissions):
            reasons.append(
                f"action-tier resource '{resource.resource_id}' requires a "
                f"write-class permission; role '{principal.role}' has none"
            )

    # ── Rule 5 — data classification ─────────────────────────────────────
    if resource.data_classification in (DataClass.CONFIDENTIAL, DataClass.PII):
        if not has_admin and _BROAD_READ not in granted_permissions:
            reasons.append(
                f"{resource.data_classification.value} data requires "
                f"'{_BROAD_READ}' or '{ADMIN_PERMISSION}'; role "
                f"'{principal.role}' has neither"
            )

    return reasons


__all__ = ["evaluate"]
