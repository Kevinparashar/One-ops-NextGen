"""FieldPolicy — schema-driven field exposure (Component Spec C12)."""
from __future__ import annotations

import pytest

from oneops.errors import ConfigError
from oneops.use_cases._shared.field_policy import FieldPolicy


@pytest.fixture
def policy() -> FieldPolicy:
    return FieldPolicy.from_registry_file()


def test_loads_from_the_registry_file(policy):
    # tenant_id is declared 'restricted' in registries/v2/field_policy.json.
    assert policy.classification_of("tenant_id") == "restricted"


def test_restricted_field_is_not_exposable(policy):
    assert policy.is_exposable("tenant_id") is False


def test_unlisted_field_defaults_to_exposable(policy):
    # An unlisted field falls back to the default classification ('internal').
    assert policy.is_exposable("title") is True
    assert policy.is_exposable("status") is True


def test_expose_drops_only_withheld_fields(policy):
    record = {"tenant_id": "T1", "title": "x", "status": "open", "priority": "P2"}
    exposed = policy.expose(record)
    assert "tenant_id" not in exposed
    assert exposed == {"title": "x", "status": "open", "priority": "P2"}


def test_a_field_below_the_threshold_is_exposable():
    p = FieldPolicy(default_classification="internal",
                    withhold_at_or_above="confidential",
                    classifications={"a": "public", "b": "internal",
                                     "c": "confidential", "d": "restricted"})
    assert p.is_exposable("a")
    assert p.is_exposable("b")
    assert not p.is_exposable("c")
    assert not p.is_exposable("d")


def test_unknown_classification_is_rejected():
    with pytest.raises(ConfigError, match="unknown classification"):
        FieldPolicy(default_classification="internal",
                    withhold_at_or_above="confidential",
                    classifications={"x": "ultra-secret"})


def test_unknown_threshold_is_rejected():
    with pytest.raises(ConfigError, match="not a known classification"):
        FieldPolicy(default_classification="internal",
                    withhold_at_or_above="bogus", classifications={})


def test_missing_policy_file_raises():
    with pytest.raises(ConfigError, match="not found"):
        FieldPolicy.from_registry_file("/nonexistent/field_policy.json")


# ── internal-content visibility (private work_notes etc.) ─────────────────

_NOTES = {"work_notes": [{"is_public": True, "text": "a"},
                         {"is_public": False, "text": "b"}]}


def test_end_user_role_loses_internal_items(policy):
    out = policy.redact_internal_content(dict(_NOTES), "employee")
    assert [n["text"] for n in out["work_notes"]] == ["a"]


def test_privileged_role_keeps_internal_items(policy):
    out = policy.redact_internal_content(dict(_NOTES), "service_desk_agent")
    assert [n["text"] for n in out["work_notes"]] == ["a", "b"]


def test_unrecognised_role_is_default_denied(policy):
    for role in ("", "made_up_role"):
        out = policy.redact_internal_content(dict(_NOTES), role)
        assert [n["text"] for n in out["work_notes"]] == ["a"], role


def test_item_missing_the_flag_is_treated_as_internal(policy):
    rec = {"work_notes": [{"text": "no flag"}, {"is_public": True, "text": "ok"}]}
    out = policy.redact_internal_content(rec, "employee")
    assert [n["text"] for n in out["work_notes"]] == ["ok"]
