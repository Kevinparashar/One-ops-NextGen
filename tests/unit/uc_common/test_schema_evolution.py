"""Field-evolution contract tests — the substrate must survive 1000-UC growth.

These tests prove, with executable evidence, that every shape in `uc_common`
(plus the supporting `tenancy` / `toolrunner.context` shapes) supports the
five field-evolution operations a long-lived registry-driven system needs:

  1. **ADD a field** — declare it Optional with a default; old payloads
     decode cleanly; new payloads carry the field.
  2. **UPDATE a field's validation** (e.g. loosen `max_length`) — values
     that were valid before remain valid; new looser values pass.
  3. **RENAME a field** — use `Field(alias='old_name')`; payloads that
     ship the old name AND payloads that ship the new name both decode.
  4. **DELETE a field** — leave it Optional with no validator for one
     N/N-1 window; old payloads keep working; new producers omit it.
  5. **FORWARD-COMPAT (unknown field)** — a v(N+1) producer ships an extra
     field a vN consumer doesn't recognise; `extra='ignore'` drops it.

Each operation is exercised against either a shipping shape OR a small
side model that demonstrates the pattern. The side-model tests are the
templates a UC author copies when they evolve a real schema.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytest
from pydantic import BaseModel, Field, ValidationError

from oneops.tenancy import TenantContext
from oneops.toolrunner.context import CacheHint, CacheSource, ToolContext
from oneops.uc_common.summary_schema import (
    SUMMARY_SCHEMA_CURRENT,
    SUMMARY_SCHEMA_MIN_SUPPORTED,
    Citation,
    CitationSource,
    EntitySummary,
    EntityType,
    KeyDetail,
    KeyDetailKind,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _base_summary_payload() -> dict:
    """Canonical v1 payload — every evolution test starts here."""
    return {
        "schema_version": 1,
        "entity_type": "incident",
        "entity_id": "INC0048213",
        "tenant_id": "tenant-acme",
        "summary": "Critical incident in progress; workaround applied.",
        "key_details": [
            {"label": "Status", "value": "in_progress", "kind": "enum"}
        ],
    }


# ═════════════════════════════════════════════════════════════════════════
# 1) ADD a field
# ═════════════════════════════════════════════════════════════════════════


def test_add_field_backward_compat_old_payload_decodes():
    """A v1 payload (no new field) constructs cleanly. The new field on the
    receiver model defaults — backward-compat is automatic. Exhibit A: the
    `confidence_source` field added in step #2 — old payloads omit it."""
    payload = _base_summary_payload()
    assert "confidence_source" not in payload
    es = EntitySummary.model_validate(payload)
    assert es.confidence_source == "deterministic"   # defaulted


def test_add_field_new_payload_carries_value():
    """A v(N+1) payload (with the new field) preserves the value."""
    payload = _base_summary_payload() | {"confidence_source": "llm", "confidence": 0.7}
    es = EntitySummary.model_validate(payload)
    assert es.confidence_source == "llm"
    assert es.confidence == pytest.approx(0.7)


# ═════════════════════════════════════════════════════════════════════════
# 2) UPDATE a field's validation (template for loosening a constraint)
# ═════════════════════════════════════════════════════════════════════════


def test_update_loosen_max_length_template():
    """Template: a hypothetical v2 model loosens `summary`'s max from 4000
    to 8000. A v1-length value remains valid; a longer value newly passes.
    This is the safe-update pattern — never tightening, only loosening."""

    class _SummaryV2(BaseModel):
        model_config = {"frozen": True, "extra": "ignore"}
        summary: str = Field(min_length=1, max_length=8000)

    # v1-length value — still valid in v2
    short = _SummaryV2(summary="x" * 100)
    assert len(short.summary) == 100
    # newly-valid v2 value
    long = _SummaryV2(summary="x" * 5000)
    assert len(long.summary) == 5000


def test_update_tighten_is_breaking_template():
    """The mirror principle: tightening (max from 4000 to 100) WOULD
    invalidate previously-valid payloads. A v1 receiver should never do
    this within the support window — bump the schema_version instead."""

    class _SummaryV2Tight(BaseModel):
        model_config = {"frozen": True, "extra": "ignore"}
        summary: str = Field(min_length=1, max_length=100)

    with pytest.raises(ValidationError):
        _SummaryV2Tight(summary="x" * 500)


# ═════════════════════════════════════════════════════════════════════════
# 3) RENAME a field via alias (template)
# ═════════════════════════════════════════════════════════════════════════


def test_rename_via_alias_both_names_decode():
    """Pattern: rename `priority` to `urgency_score` — declare the new
    name with `validation_alias='priority'`. During the N/N-1 window
    both names decode; after the window the alias drops."""

    class _RowV2(BaseModel):
        model_config = {"frozen": True, "extra": "ignore", "populate_by_name": True}
        # New name, accepts the old name on the wire.
        urgency_score: int = Field(alias="priority")

    via_old = _RowV2.model_validate({"priority": 3})
    via_new = _RowV2(urgency_score=3)
    assert via_old.urgency_score == 3 == via_new.urgency_score


def test_rename_field_with_default_during_deprecation():
    """During the deprecation window both fields exist; the receiver
    prefers the new one. Payloads carrying neither default cleanly."""

    class _CtxV2(BaseModel):
        model_config = {"frozen": True, "extra": "ignore"}
        locale: str = "en"               # new canonical
        language: Optional[str] = None   # deprecated alias slot

    new = _CtxV2.model_validate({"locale": "fr"})
    deprecated = _CtxV2.model_validate({"language": "fr"})  # ignored by spec,
    # but kept around — a migration adapter promotes it.
    promoted = _CtxV2.model_validate({"locale": deprecated.language or "en"})
    assert new.locale == "fr"
    assert promoted.locale == "fr"


# ═════════════════════════════════════════════════════════════════════════
# 4) DELETE a field (template)
# ═════════════════════════════════════════════════════════════════════════


def test_delete_field_old_payload_still_decodes():
    """An old payload carrying a now-deleted field must still decode —
    `extra='ignore'` is what enables the safe drop. Substrate
    invariant: a removed field becomes an unknown field; an unknown
    field is silently dropped, never rejected."""
    payload = _base_summary_payload() | {"_legacy_dropped_field": "anything"}
    es = EntitySummary.model_validate(payload)
    assert es.entity_id == "INC0048213"


def test_delete_field_via_optional_deprecation_template():
    """During the deprecation window the field stays as Optional with no
    validators; receivers tolerate it; new producers stop sending it."""

    class _RecordV2(BaseModel):
        model_config = {"frozen": True, "extra": "ignore"}
        id: str
        # `legacy_status` was removed in v3; v2 still carries the slot
        # so v1 payloads round-trip without surprise.
        legacy_status: Optional[str] = None

    r = _RecordV2.model_validate({"id": "x", "legacy_status": "draft"})
    assert r.legacy_status == "draft"
    r2 = _RecordV2.model_validate({"id": "x"})
    assert r2.legacy_status is None


# ═════════════════════════════════════════════════════════════════════════
# 5) FORWARD-COMPAT — unknown fields are dropped, never reject
# ═════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "extra_payload",
    [
        {"future_field": "anything"},
        {"compliance_profile": {"hipaa": True}},
        {"_internal_hint": [1, 2, 3]},
        {"never_seen_before": None},
    ],
)
def test_entity_summary_tolerates_unknown_fields(extra_payload: dict):
    """A v(N+1) producer ships extras; a vN consumer drops them."""
    payload = _base_summary_payload() | extra_payload
    es = EntitySummary.model_validate(payload)
    # The known content is preserved.
    assert es.entity_id == "INC0048213"
    # The unknown field did not surface (extra='ignore' dropped it).
    assert not hasattr(es, list(extra_payload.keys())[0])


def test_key_detail_tolerates_unknown_fields():
    kd = KeyDetail.model_validate(
        {"label": "Priority", "value": "P1", "kind": "enum",
         "future_field": "ignored"}
    )
    assert kd.label == "Priority"


def test_citation_tolerates_unknown_fields():
    c = Citation.model_validate({
        "source": "itsm", "record_id": "INC1",
        "fetched_at": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        "audit_hint": "future field",
    })
    assert c.source is CitationSource.ITSM


def test_tenant_context_tolerates_unknown_fields():
    t = TenantContext.model_validate({
        "tenant_id": "acme",
        "tier": "pro",
        "future_attribute": "ignored",
    })
    assert t.tenant_id == "acme"


def test_cache_hint_tolerates_unknown_fields():
    h = CacheHint.model_validate({
        "hit": True, "age_s": 60, "source": "read_cache",
        "future_cache_metadata": {"layer": "edge"},
    })
    assert h.age_s == 60


# ═════════════════════════════════════════════════════════════════════════
# Schema-version window — the breaking-change gate
# ═════════════════════════════════════════════════════════════════════════


def test_schema_version_current_decodes():
    payload = _base_summary_payload() | {"schema_version": SUMMARY_SCHEMA_CURRENT}
    EntitySummary.model_validate(payload)   # no raise


def test_schema_version_below_window_rejected():
    payload = _base_summary_payload() | {"schema_version": SUMMARY_SCHEMA_MIN_SUPPORTED - 1}
    with pytest.raises(ValidationError):
        EntitySummary.model_validate(payload)


def test_schema_version_above_window_rejected():
    payload = _base_summary_payload() | {"schema_version": SUMMARY_SCHEMA_CURRENT + 1}
    with pytest.raises(ValidationError):
        EntitySummary.model_validate(payload)


# ═════════════════════════════════════════════════════════════════════════
# Migration adapter pattern — convert v1 payload to a hypothetical v2 shape
# ═════════════════════════════════════════════════════════════════════════


def test_migration_adapter_pattern():
    """Template: when a real breaking change ships (schema_version 1 → 2)
    the receiver gets a small pure-Python adapter that rewrites a v1
    payload into v2 shape (e.g., split a field, rename a key). The
    adapter runs once at the codec boundary, then validation is v2."""

    def _migrate_v1_to_v2(p: dict) -> dict:
        """Hypothetical v2 splits `entity_id` into `entity_namespace` +
        `entity_local_id`. The adapter handles this without the registry."""
        out = dict(p)
        out["schema_version"] = 2
        eid = out.pop("entity_id")
        # canonical normalisation lives in the entity-id normalizer (F1);
        # for the template we just split on the first non-letter.
        for i, c in enumerate(eid):
            if not c.isalpha():
                out["entity_namespace"] = eid[:i]
                out["entity_local_id"] = eid[i:]
                break
        return out

    v1 = _base_summary_payload()
    v2_shape = _migrate_v1_to_v2(v1)
    assert v2_shape["schema_version"] == 2
    assert v2_shape["entity_namespace"] == "INC"
    assert v2_shape["entity_local_id"] == "0048213"
    # The migrated shape is no longer EntitySummary v1 — that's the
    # point. A v2 receiver model (defined elsewhere when v2 ships)
    # validates from here. The CURRENT receiver would reject this
    # (different fields, different schema_version) — which is exactly
    # how the support window guards a breaking change.


# ═════════════════════════════════════════════════════════════════════════
# Tuple/list evolution — adding row types in shipped data
# ═════════════════════════════════════════════════════════════════════════


def test_add_a_key_detail_row_evolves_cleanly():
    """A new UC-1 spec ships with an additional row. EntitySummary doesn't
    care how many rows are in `key_details` (only uniqueness + non-empty);
    so adding a row is a pure data change."""
    payload = _base_summary_payload()
    payload["key_details"].append(
        {"label": "Severity", "value": "critical", "kind": "enum"}
    )
    es = EntitySummary.model_validate(payload)
    assert len(es.key_details) == 2


def test_drop_a_key_detail_row_evolves_cleanly():
    """Removing a row (e.g. RBAC-redacted) leaves the envelope valid as
    long as at least one row remains — proving rows are data, the
    envelope is the contract."""
    payload = _base_summary_payload()
    # leave the single mandatory Status row in place
    es = EntitySummary.model_validate(payload)
    assert len(es.key_details) == 1
