"""RBAC resolver tests — role → permission resolution, deny-by-default."""
from __future__ import annotations

import pytest

from oneops.authz.rbac import ADMIN_PERMISSION, RbacResolver
from oneops.errors import ConfigError


def test_loads_roles_from_the_registry_file():
    rbac = RbacResolver.from_registry_file()
    assert rbac.role_count >= 14                     # the registry defines 14 roles
    assert rbac.known_role("service_desk_agent")


def test_permissions_for_known_role():
    rbac = RbacResolver.from_registry_file()
    assert rbac.permissions_for("viewer") == frozenset({"read:own_tickets"})
    assert "write:ticket" in rbac.permissions_for("service_desk_agent")


def test_unknown_role_resolves_to_empty_set_not_error():
    """Deny-by-default — an unknown role is the empty permission set, never a
    raised error a caller might swallow into an allow."""
    rbac = RbacResolver.from_registry_file()
    assert rbac.permissions_for("ceo_of_everything") == frozenset()
    assert rbac.known_role("ceo_of_everything") is False


def test_has_permission_direct():
    rbac = RbacResolver({"agent": frozenset({"read:all_tickets", "write:ticket"})})
    assert rbac.has_permission("agent", "write:ticket") is True
    assert rbac.has_permission("agent", "approve:change") is False


def test_admin_satisfies_any_permission_check():
    rbac = RbacResolver({"director": frozenset({ADMIN_PERMISSION})})
    assert rbac.has_permission("director", "write:cmdb") is True
    assert rbac.has_permission("director", "anything:at:all") is True


def test_it_director_carries_admin_in_the_registry():
    rbac = RbacResolver.from_registry_file()
    assert ADMIN_PERMISSION in rbac.permissions_for("it_director")


def test_missing_registry_file_raises_config_error():
    with pytest.raises(ConfigError, match="not found"):
        RbacResolver.from_registry_file("/nonexistent/role-registry.json")
