"""Contract tests for TenantContext — the per-request tenant facts.

Validators are tested both ways: bad input must raise, good input must
construct cleanly. Frozenness is asserted directly — the model is
substrate; mutation is a contract bug.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from oneops.tenancy import (
    DEFAULT_LOCALE,
    DEFAULT_REGION,
    DEFAULT_TIER,
    TenantContext,
    Tier,
)

# ── happy path + defaults ────────────────────────────────────────────────


def test_minimal_construction_with_defaults():
    t = TenantContext(tenant_id="tenant-acme")
    assert t.tenant_id == "tenant-acme"
    assert t.tier is DEFAULT_TIER
    assert t.region == DEFAULT_REGION
    assert t.locale == DEFAULT_LOCALE
    assert t.feature_flags == {}
    assert t.residency is None


def test_full_construction():
    t = TenantContext(
        tenant_id="tenant-eu-1",
        tier=Tier.ENTERPRISE,
        region="eu-west-1",
        locale="de-DE",
        feature_flags={"experimental_summary": True, "beta_actions": False},
        residency="EU",
    )
    assert t.tier is Tier.ENTERPRISE
    assert t.region == "eu-west-1"
    assert t.locale == "de-DE"
    assert t.residency == "EU"
    assert t.has_flag("experimental_summary") is True
    assert t.has_flag("beta_actions") is False
    assert t.has_flag("missing_flag") is False


# ── frozen / immutable ──────────────────────────────────────────────────


def test_frozen_blocks_field_mutation():
    t = TenantContext(tenant_id="acme")
    with pytest.raises(ValidationError):
        t.tier = Tier.PRO              # type: ignore[misc]


def test_frozen_blocks_feature_flags_reassignment():
    """Top-level field assignment is blocked by frozen=True. Nested-dict
    mutation discipline (handlers must treat feature_flags as read-only)
    is a code-review concern, not enforced here."""
    t = TenantContext(tenant_id="acme", feature_flags={"feat_x": True})
    with pytest.raises(ValidationError):
        t.feature_flags = {"feat_y": False}    # type: ignore[misc]


# ── validators: tenant_id ──────────────────────────────────────────────


def test_blank_tenant_id_rejected():
    with pytest.raises(ValidationError):
        TenantContext(tenant_id="")


def test_overlong_tenant_id_rejected():
    with pytest.raises(ValidationError):
        TenantContext(tenant_id="x" * 200)


# ── validators: tier ───────────────────────────────────────────────────


def test_unknown_tier_rejected():
    with pytest.raises(ValidationError):
        TenantContext(tenant_id="acme", tier="trial")    # type: ignore[arg-type]


# ── validators: locale (BCP-47-ish) ────────────────────────────────────


@pytest.mark.parametrize("good", ["en", "fr", "de-DE", "en-US", "pt-BR"])
def test_valid_locales_accepted(good: str):
    t = TenantContext(tenant_id="acme", locale=good)
    assert t.locale == good


@pytest.mark.parametrize(
    "bad",
    [
        "english",        # too long, no region split
        "EN",             # case-sensitive
        "en_US",          # underscore not allowed (BCP-47 uses hyphen)
        "en-us",          # region must be uppercase
        "en-USA",         # region must be 2-letter
        "e",              # too short
        "  en  ",         # whitespace
        "en;evil=1",      # injection attempt
    ],
)
def test_invalid_locales_rejected(bad: str):
    with pytest.raises(ValidationError):
        TenantContext(tenant_id="acme", locale=bad)


# ── validators: feature_flags keys ─────────────────────────────────────


def test_feature_flag_key_must_be_snake_case():
    with pytest.raises(ValidationError):
        TenantContext(tenant_id="acme", feature_flags={"FeatureX": True})


def test_feature_flag_key_too_short_rejected():
    with pytest.raises(ValidationError):
        # min 3 chars after the leading lowercase letter — "ab" is too short
        TenantContext(tenant_id="acme", feature_flags={"ab": True})


def test_feature_flag_value_must_be_bool():
    with pytest.raises(ValidationError):
        TenantContext(tenant_id="acme", feature_flags={"feature_x": "yes"})   # type: ignore[arg-type]


# ── validators: region / residency ─────────────────────────────────────


def test_blank_region_rejected():
    with pytest.raises(ValidationError):
        TenantContext(tenant_id="acme", region="")


def test_residency_can_be_none():
    t = TenantContext(tenant_id="acme")
    assert t.residency is None


def test_residency_overlong_rejected():
    with pytest.raises(ValidationError):
        TenantContext(tenant_id="acme", residency="x" * 200)


# ── enum coverage ──────────────────────────────────────────────────────


def test_tier_enum_members_exhaustive():
    """A new tier must be a deliberate change — pin the set."""
    assert {t.value for t in Tier} == {"free", "pro", "enterprise"}
