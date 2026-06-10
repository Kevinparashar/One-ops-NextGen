"""Step 2 — owning-group resolution: SOURCE validation (the runtime JOIN is
verified live in integration).

`data/itsm/group_role_map.json` is the config-as-code source loaded into
`itsm.group_role_map`; the resolver JOINs that table with `sys_user`. These unit
tests validate the SOURCE: it is well-formed and covers every owner_group the
catalog actually uses. The coverage check reads the REAL catalog seed (not a
hand-kept mirror), so a new item with a new owner_group fails CI until mapped —
caught at build, never a silent runtime mis-route.
"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4] / "data" / "itsm"
_MAP = _ROOT / "group_role_map.json"
_CATALOG = _ROOT / "catalog_item.json"


def _groups() -> dict[str, dict]:
    return json.loads(_MAP.read_text(encoding="utf-8"))["groups"]


def _catalog_owner_groups() -> set[str]:
    items = json.loads(_CATALOG.read_text(encoding="utf-8"))
    return {i["owner_group"] for i in items if i.get("owner_group")}


def test_map_is_nonempty() -> None:
    assert _groups()


def test_every_entry_has_exactly_one_criterion() -> None:
    """Each group staffs via role XOR department — never both, never neither
    (the loader and the JOIN both rely on this)."""
    for grp, entry in _groups().items():
        keys = set(entry) & {"role", "department"}
        assert len(keys) == 1, f"{grp} must have exactly one of role/department"


def test_criterion_values_are_nonempty_strings() -> None:
    for grp, entry in _groups().items():
        (k,) = set(entry) & {"role", "department"}
        assert isinstance(entry[k], str) and entry[k].strip(), f"{grp} empty {k}"


def test_every_catalog_owner_group_is_mapped() -> None:
    """Build-time guard against routing gaps: every owning team referenced by
    the catalog seed has a mapping. New owner_group with no entry → FAILS here."""
    unmapped = _catalog_owner_groups() - set(_groups())
    assert not unmapped, (
        f"catalog owner_groups with NO entry in group_role_map.json: "
        f"{sorted(unmapped)} — add a mapping (or wire the IdP sync)."
    )


def test_known_mappings_are_stable() -> None:
    """Spot-check the canonical bridges the resolver depends on."""
    g = _groups()
    assert g["GRP-NETOPS"] == {"role": "network_engineer"}
    assert g["GRP-SECOPS"] == {"role": "security_engineer"}
    assert g["GRP-HR"] == {"department": "HR"}
    assert g["GRP-FINANCE"] == {"department": "Finance"}
