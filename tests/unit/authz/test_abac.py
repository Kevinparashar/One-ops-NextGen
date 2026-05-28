"""ABAC evaluation tests.

The exit criterion is "every ABAC deny is honored". This file proves it rule
by rule: for each of the five rules, a violating scenario produces a denial,
and the all-clear scenario produces an allow. Reasons are checked, not just
the allow/deny bit.
"""
from __future__ import annotations

from oneops.authz.abac import evaluate
from oneops.authz.models import DataClass, Principal, ResourceDescriptor, Tier


def _principal(tenant="tenant-a", role="service_desk_agent"):
    return Principal(tenant_id=tenant, user_id="u-1", role=role)


def _resource(tenant="tenant-a", *, tier=Tier.READ, data=DataClass.INTERNAL,
              audience=(), scopes=()):
    return ResourceDescriptor(
        resource_id="uc01_summarization", resource_tenant_id=tenant, tier=tier,
        data_classification=data, audience=audience, required_scopes=scopes)


_AGENT_PERMS = frozenset({"read:all_tickets", "write:ticket", "create:ticket"})
_VIEWER_PERMS = frozenset({"read:own_tickets"})
_ADMIN_PERMS = frozenset({"admin"})


# ── all-clear ────────────────────────────────────────────────────────────


def test_all_rules_pass_yields_no_reasons():
    assert evaluate(_principal(), _resource(), _AGENT_PERMS) == []


# ── Rule 1: tenant isolation ─────────────────────────────────────────────


def test_cross_tenant_access_is_denied():
    reasons = evaluate(_principal(tenant="tenant-a"),
                       _resource(tenant="tenant-b"), _AGENT_PERMS)
    assert len(reasons) == 1
    assert "cross-tenant" in reasons[0]


def test_cross_tenant_short_circuits_other_rules():
    """A tenant mismatch returns exactly one reason — no role/scope rule is
    even evaluated across a tenant boundary."""
    reasons = evaluate(_principal(tenant="tenant-a", role="viewer"),
                       _resource(tenant="tenant-b", tier=Tier.ACTION,
                                 data=DataClass.PII, scopes=("write:cmdb",)),
                       _VIEWER_PERMS)
    assert reasons == [reasons[0]]                   # only the tenant reason


# ── Rule 2: audience ─────────────────────────────────────────────────────


def test_role_outside_audience_is_denied():
    reasons = evaluate(_principal(role="employee"),
                       _resource(audience=("service_desk_agent", "manager")),
                       _VIEWER_PERMS)
    assert any("not in resource audience" in r for r in reasons)


def test_empty_audience_imposes_no_role_gate():
    assert evaluate(_principal(role="employee"),
                    _resource(audience=()), _AGENT_PERMS) == []


def test_role_inside_audience_passes():
    assert evaluate(_principal(role="service_desk_agent"),
                    _resource(audience=("service_desk_agent",)), _AGENT_PERMS) == []


# ── Rule 3: required scopes ──────────────────────────────────────────────


def test_missing_required_scope_is_denied():
    reasons = evaluate(_principal(), _resource(scopes=("write:cmdb",)), _AGENT_PERMS)
    assert any("missing required scope" in r and "write:cmdb" in r for r in reasons)


def test_admin_bypasses_scope_requirement():
    assert evaluate(_principal(role="it_director"),
                    _resource(scopes=("write:cmdb", "write:asset")),
                    _ADMIN_PERMS) == []


def test_held_scope_passes():
    assert evaluate(_principal(), _resource(scopes=("read:all_tickets",)),
                    _AGENT_PERMS) == []


# ── Rule 4: tier ─────────────────────────────────────────────────────────


def test_action_tier_denied_for_read_only_role():
    reasons = evaluate(_principal(role="viewer"),
                       _resource(tier=Tier.ACTION), _VIEWER_PERMS)
    assert any("action-tier" in r for r in reasons)


def test_action_tier_allowed_for_write_role():
    assert evaluate(_principal(), _resource(tier=Tier.ACTION), _AGENT_PERMS) == []


def test_read_tier_imposes_no_write_requirement():
    assert evaluate(_principal(role="viewer"),
                    _resource(tier=Tier.READ), _VIEWER_PERMS) == []


# ── Rule 5: data classification ──────────────────────────────────────────


def test_confidential_data_denied_for_own_records_role():
    reasons = evaluate(_principal(role="viewer"),
                       _resource(data=DataClass.CONFIDENTIAL), _VIEWER_PERMS)
    assert any("confidential data requires" in r for r in reasons)


def test_pii_data_denied_for_own_records_role():
    reasons = evaluate(_principal(role="employee"),
                       _resource(data=DataClass.PII),
                       frozenset({"read:own_tickets", "create:ticket"}))
    assert any("pii data requires" in r for r in reasons)


def test_confidential_data_allowed_with_broad_read():
    assert evaluate(_principal(), _resource(data=DataClass.CONFIDENTIAL),
                    _AGENT_PERMS) == []


def test_confidential_data_allowed_for_admin():
    assert evaluate(_principal(role="it_director"),
                    _resource(data=DataClass.PII), _ADMIN_PERMS) == []


# ── multiple violations ──────────────────────────────────────────────────


def test_every_failed_rule_is_reported_not_just_the_first():
    """A viewer hitting an action-tier, PII, scope-gated, audience-gated
    resource in-tenant fails rules 2,3,4,5 — all four must surface."""
    reasons = evaluate(
        _principal(role="viewer"),
        _resource(tier=Tier.ACTION, data=DataClass.PII,
                  audience=("manager",), scopes=("write:cmdb",)),
        _VIEWER_PERMS)
    joined = " ".join(reasons)
    assert "audience" in joined
    assert "missing required scope" in joined
    assert "action-tier" in joined
    assert "pii data requires" in joined
    assert len(reasons) == 4
