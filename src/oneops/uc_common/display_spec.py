"""DisplaySpec — the per-service row list for UC-1, as registry data.

A DisplaySpec declares, for one `entity_type`, the ordered list of rows a
UC-1 handler must render into `EntitySummary.key_details`. The spec lives
as JSON in `registries/v2/display_specs/uc01/`, NOT in Python — adding a
new entity type or reshuffling a row list is a registry change, no code
deploy, no handler edit (1000-UC discipline).

Rules:
  * Rows are ordered. The renderer/LLM trusts the order; do not re-sort.
  * `source_field` is a dotted path into the raw record the handler fetched.
    The adapter resolves it; missing → row is omitted; truncated counter
    is incremented; if `optional=False`, the handler raises (a contract
    breach the registry author asked for).
  * `rbac_required` lists permission codes ALL of which the caller must
    hold. Missing any → row dropped silently + truncated=True; no
    info-leak (the user is never told what was withheld).
  * `kind` is the `KeyDetailKind` the renderer/LLM uses; the adapter casts
    the resolved raw value to the matching display string.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from oneops.uc_common.summary_schema import ENTITY_TYPES, KeyDetailKind


DEFAULT_DISPLAY_SPECS_ROOT = Path(__file__).resolve().parents[3] / "registries" / "v2" / "display_specs"


class UnknownEntityTypeError(KeyError):
    """Raised by `load_display_spec` when the entity_type has no spec."""


class RowSpec(BaseModel):
    """One row in the schema-prescribed display order for an entity_type."""

    model_config = {"frozen": True, "extra": "ignore"}

    label: str = Field(min_length=1, max_length=80)
    source_field: str = Field(min_length=1, max_length=200)
    kind: KeyDetailKind = KeyDetailKind.TEXT
    optional: bool = True
    rbac_required: tuple[str, ...] = ()


class DisplaySpec(BaseModel):
    """Schema-prescribed row list for a single entity_type.

    Loaded once per process and cached. Hot-reload is a registry concern
    (versioned, like every other registry record); the loader itself is
    side-effect-free and re-callable.

    Field evolution: registry data is data — adding a row is one JSON edit,
    removing a row is a deprecation, renaming a row is "old row + new row,
    one window, drop old." `extra='ignore'` lets a v2 producer ship rows
    with new fields a v1 consumer doesn't know about."""

    model_config = {"frozen": True, "extra": "ignore"}

    entity_type: str = Field(min_length=1, max_length=32)
    version: int = Field(default=1, ge=1)
    rows: tuple[RowSpec, ...]

    @field_validator("entity_type")
    @classmethod
    def _entity_type_known(cls, v: str) -> str:
        if v not in ENTITY_TYPES:
            raise ValueError(
                f"DisplaySpec.entity_type={v!r} unknown; expected one of {sorted(ENTITY_TYPES)}"
            )
        return v

    @field_validator("rows")
    @classmethod
    def _rows_unique_and_nonempty(cls, v: tuple[RowSpec, ...]) -> tuple[RowSpec, ...]:
        if not v:
            raise ValueError("DisplaySpec.rows must be non-empty")
        labels = [r.label for r in v]
        if len(labels) != len(set(labels)):
            raise ValueError("DisplaySpec.rows labels must be unique within a spec")
        return v


# ── Loader ───────────────────────────────────────────────────────────────


_CACHE: dict[Path, dict[str, DisplaySpec]] = {}


def _load_all(root: Path) -> dict[str, DisplaySpec]:
    """Read every uc01/*.json under `root` and validate. Cached per root."""
    if root in _CACHE:
        return _CACHE[root]
    uc01_dir = root / "uc01"
    if not uc01_dir.is_dir():
        raise FileNotFoundError(f"display_specs uc01 directory not found at {uc01_dir}")
    specs: dict[str, DisplaySpec] = {}
    for path in sorted(uc01_dir.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        spec = DisplaySpec.model_validate(raw)
        if spec.entity_type in specs:
            raise ValueError(
                f"duplicate display_spec for entity_type={spec.entity_type!r} "
                f"(second copy at {path})"
            )
        specs[spec.entity_type] = spec
    missing = ENTITY_TYPES - specs.keys()
    if missing:
        raise ValueError(
            f"display_specs uc01 incomplete; missing entity_types: {sorted(missing)}"
        )
    _CACHE[root] = specs
    return specs


def load_display_spec(
    entity_type: str,
    *,
    root: Optional[Path] = None,
) -> DisplaySpec:
    """Return the UC-1 display spec for one entity_type. Cached per root.

    Raises `UnknownEntityTypeError` if the entity_type has no registered
    spec — a wired-but-unsupported entity must fail loudly, never silently
    render an empty key_details list."""
    target_root = root if root is not None else DEFAULT_DISPLAY_SPECS_ROOT
    specs = _load_all(target_root)
    if entity_type not in specs:
        raise UnknownEntityTypeError(entity_type)
    return specs[entity_type]


def clear_cache() -> None:
    """Test hook — wipes the loader's cache."""
    _CACHE.clear()


__all__ = [
    "DEFAULT_DISPLAY_SPECS_ROOT",
    "DisplaySpec",
    "RowSpec",
    "UnknownEntityTypeError",
    "load_display_spec",
    "clear_cache",
]
