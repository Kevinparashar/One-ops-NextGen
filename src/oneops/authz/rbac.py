"""RBAC — coarse role → permission resolution.

The role catalog is data (`registries/role-permission-registry.json`, a
durable asset). `RbacResolver` loads it once and answers `permissions_for(role)`.
RBAC is the *coarse* gate; ABAC (abac.py) refines per tenant/resource/attribute.

The loader is pluggable: `RbacResolver` takes a `dict[role, frozenset[perm]]`,
and `from_registry_file()` builds that from the JSON. A future role registry
(or a DB-backed source) swaps in without touching `RbacResolver` or AuthzService.

Permission grammar (from the registry): `action:scope`, e.g. `read:all_tickets`,
`write:ticket`, `approve:change`, plus the bare super-permission `admin`.
"""
from __future__ import annotations

import json
from pathlib import Path

from oneops.errors import ConfigError
from oneops.observability import get_logger

_log = get_logger("oneops.authz.rbac")

# A role holding this permission satisfies any scope check (super-user).
ADMIN_PERMISSION = "admin"

_DEFAULT_REGISTRY = "registries/role-permission-registry.json"


class RbacResolver:
    """Resolves a role to its permission set. Immutable after construction."""

    def __init__(self, role_permissions: dict[str, frozenset[str]]) -> None:
        self._roles: dict[str, frozenset[str]] = dict(role_permissions)

    @classmethod
    def from_registry_file(cls, path: str | None = None) -> RbacResolver:
        """Build from the role-permission registry JSON."""
        if path is None:
            repo_root = Path(__file__).resolve().parents[3]
            path = str(repo_root / _DEFAULT_REGISTRY)
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"role-permission registry not found: {p}")
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"role-permission registry unreadable: {p}", cause=exc) from exc

        roles: dict[str, frozenset[str]] = {}
        for entry in doc.get("roles", []):
            role = entry.get("role")
            if not role:
                raise ConfigError(f"role entry without a `role` field in {p}")
            roles[role] = frozenset(entry.get("permissions", []))
        if not roles:
            raise ConfigError(f"role-permission registry {p} defines no roles")
        _log.info("authz.rbac_loaded", role_count=len(roles), source=str(p))
        return cls(roles)

    def known_role(self, role: str) -> bool:
        return role in self._roles

    def permissions_for(self, role: str) -> frozenset[str]:
        """The permission set of `role`. An unknown role resolves to the empty
        set — deny-by-default, never an error that a caller might swallow."""
        return self._roles.get(role, frozenset())

    def has_permission(self, role: str, permission: str) -> bool:
        """True if `role` holds `permission` directly or via `admin`."""
        perms = self.permissions_for(role)
        return permission in perms or ADMIN_PERMISSION in perms

    @property
    def role_count(self) -> int:
        return len(self._roles)


__all__ = ["RbacResolver", "ADMIN_PERMISSION"]
