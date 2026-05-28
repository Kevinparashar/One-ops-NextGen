"""Contract tests for the per-service display specs.

Two layers: (a) the Pydantic model rejects malformed specs, (b) the
loader resolves every shipped entity_type cleanly. Both layers prove
the registry-data side of the UC-1 contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from oneops.uc_common.display_spec import (
    DEFAULT_DISPLAY_SPECS_ROOT,
    DisplaySpec,
    RowSpec,
    UnknownEntityTypeError,
    clear_cache,
    load_display_spec,
)
from oneops.uc_common.summary_schema import ENTITY_TYPES, KeyDetailKind


# ── RowSpec / DisplaySpec model rules ──────────────────────────────────


def test_rowspec_minimal():
    r = RowSpec(label="Status", source_field="status", kind=KeyDetailKind.ENUM)
    assert r.optional is True
    assert r.rbac_required == ()


def test_displayspec_rejects_empty_rows():
    with pytest.raises(ValidationError):
        DisplaySpec(entity_type="incident", rows=())


def test_displayspec_rejects_unknown_entity_type():
    with pytest.raises(ValidationError):
        DisplaySpec(
            entity_type="ufo",
            rows=(RowSpec(label="x", source_field="x"),),
        )


def test_displayspec_rejects_duplicate_row_labels():
    with pytest.raises(ValidationError):
        DisplaySpec(
            entity_type="incident",
            rows=(
                RowSpec(label="Status", source_field="status"),
                RowSpec(label="Status", source_field="status2"),
            ),
        )


# ── Shipped specs — every entity_type resolves ────────────────────────


@pytest.fixture(autouse=True)
def _clear():
    clear_cache()
    yield
    clear_cache()


@pytest.mark.parametrize("entity_type", sorted(ENTITY_TYPES))
def test_every_entity_type_loads(entity_type: str):
    spec = load_display_spec(entity_type)
    assert spec.entity_type == entity_type
    assert spec.rows, f"empty rows for {entity_type}"


def test_first_row_is_status_like_required():
    """Every entity_type's first row is Status / Operational Status and
    is `optional=False` — Status is the only row a UC-1 handler must always
    surface (RBAC aside)."""
    for entity_type in sorted(ENTITY_TYPES):
        spec = load_display_spec(entity_type)
        first = spec.rows[0]
        assert first.optional is False, f"{entity_type}: first row not required: {first}"
        assert "status" in first.label.lower(), (
            f"{entity_type}: first row label is {first.label!r}, expected a status row"
        )


def test_unknown_entity_type_raises_typed():
    with pytest.raises(UnknownEntityTypeError):
        load_display_spec("ufo")


def test_loader_is_cached_per_root(tmp_path: Path):
    # Build a minimal valid set of all six specs in tmp_path.
    uc01 = tmp_path / "uc01"
    uc01.mkdir(parents=True)
    for et in ENTITY_TYPES:
        (uc01 / f"{et}.json").write_text(
            json.dumps(
                {
                    "entity_type": et,
                    "version": 1,
                    "rows": [
                        {
                            "label": "Status",
                            "source_field": "status",
                            "kind": "enum",
                            "optional": False,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    # First load populates the cache; mutating the file then re-loading
    # must still return the cached version.
    spec1 = load_display_spec("incident", root=tmp_path)
    (uc01 / "incident.json").write_text(
        json.dumps(
            {
                "entity_type": "incident",
                "version": 99,
                "rows": [
                    {
                        "label": "Status",
                        "source_field": "status",
                        "kind": "enum",
                        "optional": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    spec2 = load_display_spec("incident", root=tmp_path)
    assert spec1.version == spec2.version == 1   # cache held


def test_loader_rejects_incomplete_set(tmp_path: Path):
    uc01 = tmp_path / "uc01"
    uc01.mkdir(parents=True)
    # only one spec — five missing
    (uc01 / "incident.json").write_text(
        json.dumps(
            {
                "entity_type": "incident",
                "version": 1,
                "rows": [
                    {
                        "label": "Status",
                        "source_field": "status",
                        "kind": "enum",
                        "optional": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="incomplete"):
        load_display_spec("incident", root=tmp_path)


def test_default_root_exists_and_has_uc01():
    assert DEFAULT_DISPLAY_SPECS_ROOT.is_dir(), (
        f"shipped display_specs missing at {DEFAULT_DISPLAY_SPECS_ROOT}"
    )
    assert (DEFAULT_DISPLAY_SPECS_ROOT / "uc01").is_dir()


# ── RBAC-flagged rows are explicitly opt-in ────────────────────────────


def test_some_rows_declare_rbac():
    """At least one shipped spec has a row with rbac_required — proves the
    declaration channel works end-to-end. (Cost rows in SR + asset.)"""
    found = False
    for entity_type in sorted(ENTITY_TYPES):
        spec = load_display_spec(entity_type)
        if any(r.rbac_required for r in spec.rows):
            found = True
            break
    assert found, "no shipped display_spec declares rbac_required — RBAC channel untested"
