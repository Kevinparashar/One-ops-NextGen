"""Contract tests for EntitySummary — the canonical UC-1 response shape.

Each validator in summary_schema.py is exercised both ways: the bad case
must raise ValidationError; the good case must construct cleanly. This
proves the rule, not just the field.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from oneops.uc_common.summary_schema import (
    SUMMARY_SCHEMA_CURRENT,
    ActionRef,
    Citation,
    CitationSource,
    ClaimRef,
    DataFreshness,
    EntitySummary,
    EntityType,
    KeyDetail,
    KeyDetailKind,
    PartyRef,
)

# ── helpers ──────────────────────────────────────────────────────────────


def _valid_kd(label: str = "Status", value: str = "in_progress") -> KeyDetail:
    return KeyDetail(label=label, value=value, kind=KeyDetailKind.ENUM)


def _valid_summary(**overrides) -> EntitySummary:
    payload = dict(
        entity_type=EntityType.INCIDENT,
        entity_id="INC0048213",
        tenant_id="tenant-acme",
        summary="Critical incident in progress; workaround applied via read-replica.",
        key_details=(_valid_kd(),),
    )
    payload.update(overrides)
    return EntitySummary(**payload)


# ── KeyDetail ────────────────────────────────────────────────────────────


def test_key_detail_round_trip():
    kd = KeyDetail(label="Priority", value="P1", kind=KeyDetailKind.ENUM, raw=1)
    assert kd.label == "Priority"
    assert kd.raw == 1
    assert kd.kind is KeyDetailKind.ENUM


def test_key_detail_rejects_whitespace_value():
    with pytest.raises(ValidationError):
        KeyDetail(label="Status", value=" open ")


def test_key_detail_rejects_empty_label():
    with pytest.raises(ValidationError):
        KeyDetail(label="", value="x")


# ── EntitySummary identity + envelope ────────────────────────────────────


def test_entity_summary_round_trip():
    es = _valid_summary()
    assert es.entity_type is EntityType.INCIDENT
    assert es.entity_id == "INC0048213"
    assert es.schema_version == SUMMARY_SCHEMA_CURRENT
    assert es.confidence == 1.0
    assert es.data_freshness is DataFreshness.LIVE


def test_entity_summary_rejects_unknown_entity_type():
    with pytest.raises(ValidationError):
        _valid_summary(entity_type="ufo")  # type: ignore[arg-type]


def test_entity_summary_rejects_out_of_window_schema_version():
    with pytest.raises(ValidationError):
        _valid_summary(schema_version=99)


def test_entity_summary_rejects_zero_schema_version():
    with pytest.raises(ValidationError):
        _valid_summary(schema_version=0)


# ── narrative discipline ────────────────────────────────────────────────


def test_summary_rejects_markdown_bullets():
    with pytest.raises(ValidationError):
        _valid_summary(summary="Header.\n- bullet one\n- bullet two")


def test_summary_rejects_markdown_heading():
    with pytest.raises(ValidationError):
        _valid_summary(summary="intro\n## a heading\nmore")


def test_summary_rejects_code_fence():
    with pytest.raises(ValidationError):
        _valid_summary(summary="intro\n```code```\nmore")


def test_summary_rejects_blank_paragraph():
    with pytest.raises(ValidationError):
        _valid_summary(summary="   ")


def test_summary_allows_inline_newlines_within_paragraph():
    """A wrapped paragraph (single \\n, no bullet marker) is fine."""
    es = _valid_summary(summary="Line one.\nLine two continuing the same thought.")
    assert "Line two" in es.summary


# ── key_details rules ───────────────────────────────────────────────────


def test_key_details_must_be_nonempty():
    with pytest.raises(ValidationError):
        _valid_summary(key_details=())


def test_key_details_labels_must_be_unique():
    with pytest.raises(ValidationError):
        _valid_summary(
            key_details=(_valid_kd("Status", "open"), _valid_kd("Status", "closed"))
        )


# ── cache transparency ─────────────────────────────────────────────────


def test_live_disallows_cache_age():
    with pytest.raises(ValidationError):
        _valid_summary(data_freshness=DataFreshness.LIVE, cache_age_s=120)


def test_cached_requires_cache_age():
    with pytest.raises(ValidationError):
        _valid_summary(data_freshness=DataFreshness.CACHED, cache_age_s=None)


def test_cached_with_age_ok():
    es = _valid_summary(data_freshness=DataFreshness.CACHED, cache_age_s=42)
    assert es.cache_age_s == 42


def test_cache_age_must_be_non_negative():
    with pytest.raises(ValidationError):
        _valid_summary(data_freshness=DataFreshness.CACHED, cache_age_s=-1)


# ── confidence ─────────────────────────────────────────────────────────


def test_confidence_in_range():
    es = _valid_summary(confidence=0.42)
    assert es.confidence == pytest.approx(0.42)


@pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0, -1.0])
def test_confidence_out_of_range_rejected(bad: float):
    with pytest.raises(ValidationError):
        _valid_summary(confidence=bad)


# ── citations / actions / provenance ───────────────────────────────────


def test_citation_minimal():
    c = Citation(
        source=CitationSource.ITSM,
        record_id="INC0048213",
        fetched_at=datetime(2026, 4, 2, 12, 0, tzinfo=UTC),
    )
    assert c.source is CitationSource.ITSM
    assert c.url is None


def test_action_ref_defaults_confirm_true():
    a = ActionRef(action_id="incident.resolve", label="Resolve")
    assert a.requires_confirmation is True
    assert a.requires_slots == ()


def test_claim_ref_round_trip():
    cr = ClaimRef(
        claim="The workaround is a read-only replica.",
        anchor="Workaround",
        anchor_kind="key_detail",
    )
    assert cr.anchor == "Workaround"


def test_party_ref_optional_role():
    p = PartyRef(user_id="USR00007", display_name="A. User")
    assert p.role is None


# ── frozen-ness (immutability is a substrate guarantee) ────────────────


def test_entity_summary_is_frozen():
    es = _valid_summary()
    with pytest.raises(ValidationError):
        es.summary = "tampered"  # type: ignore[misc]


def test_key_detail_is_frozen():
    kd = _valid_kd()
    with pytest.raises(ValidationError):
        kd.value = "tampered"  # type: ignore[misc]


# ── full populated envelope (smoke for downstream consumers) ───────────


def test_full_envelope_smoke():
    es = EntitySummary(
        entity_type=EntityType.INCIDENT,
        entity_id="INC0048213",
        tenant_id="tenant-acme",
        summary=(
            "Critical Payroll-DB timeout; report failed at 5 min; assigned to "
            "USR00004 in GRP-DBA; workaround on read-replica complete in 90s; "
            "permanent fix tracked under CHG0004003."
        ),
        key_details=(
            KeyDetail(label="Status", value="in_progress", kind=KeyDetailKind.ENUM),
            KeyDetail(label="Priority", value="P1", kind=KeyDetailKind.ENUM),
            KeyDetail(label="Reported By", value="USR00007", kind=KeyDetailKind.ID_REF),
            KeyDetail(label="Assigned To", value="USR00004", kind=KeyDetailKind.ID_REF),
        ),
        citations=(
            Citation(
                source=CitationSource.ITSM,
                record_id="INC0048213",
                fetched_at=datetime(2026, 4, 2, 12, 0, tzinfo=UTC),
            ),
        ),
        actions_available=(ActionRef(action_id="incident.resolve", label="Resolve"),),
        assignee=PartyRef(user_id="USR00004", display_name="DBA Lead", role="L2"),
        data_freshness=DataFreshness.CACHED,
        cache_age_s=240,
        truncated=False,
        confidence=0.92,
        confidence_source="llm",
    )
    assert es.cache_age_s == 240
    assert es.assignee
    assert es.assignee.role == "L2"
    assert len(es.actions_available) == 1
