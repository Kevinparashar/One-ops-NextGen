"""Step 4 — the approval matrix (`data/itsm/approval_policy.json`) + `match_policy`.

Validates the matrix is well-formed and that evaluation is DETERMINISTIC:
top-down by priority, first match wins, self-service exceptions beat their
category, and the fail-safe catch-all always matches.
"""
from __future__ import annotations

import json
from pathlib import Path

from oneops.use_cases.uc08_fulfillment.approval import match_policy

_MATRIX = (
    Path(__file__).resolve().parents[4] / "data" / "itsm" / "approval_policy.json"
)


def _policies() -> list[dict]:
    """The real matrix, ordered the way the runtime evaluates it (priority asc)."""
    raw = json.loads(_MATRIX.read_text(encoding="utf-8"))["policies"]
    return sorted(raw, key=lambda p: p["priority"])


# ── Schema / structural integrity ──────────────────────────────────────────
def test_every_policy_well_formed() -> None:
    for p in _policies():
        assert isinstance(p["policy_id"], str) and p["policy_id"]
        assert isinstance(p["priority"], int)
        assert isinstance(p["match"], dict)
        assert isinstance(p["required"], bool)
        assert isinstance(p["stages"], list)


def test_priorities_and_ids_unique() -> None:
    pols = _policies()
    prios = [p["priority"] for p in pols]
    ids = [p["policy_id"] for p in pols]
    assert len(prios) == len(set(prios)), "duplicate priority — non-deterministic"
    assert len(ids) == len(set(ids)), "duplicate policy_id"


def test_catch_all_is_last_and_matches_anything() -> None:
    last = _policies()[-1]
    assert last["match"] == {}, "the highest-priority row must be the catch-all"
    assert last["required"] is True and last["stages"], "catch-all must require approval"


def test_required_flag_matches_stages() -> None:
    """required:false → no stages (self-service); required:true → has stages."""
    for p in _policies():
        if p["required"]:
            assert p["stages"], f"{p['policy_id']} requires approval but has no stage"
        else:
            assert not p["stages"], f"{p['policy_id']} is self-service but has stages"


# ── Deterministic evaluation (match_policy) ─────────────────────────────────
def test_self_service_item_beats_its_category() -> None:
    """A password reset (security category) must hit the self-service row, not
    the security category row — proves priority ordering wins."""
    p = match_policy(
        {"catalog_item_id": "CAT_SE_PASSWORD", "category": "security"}, _policies())
    assert p["policy_id"] == "selfservice_password"
    assert p["required"] is False


def test_category_match() -> None:
    p = match_policy({"catalog_item_id": "CAT_HW_X", "category": "hardware"}, _policies())
    assert p["policy_id"] == "cat_hardware"
    assert p["stages"][0]["approver"]["type"] == "manager_of_requester"

    p = match_policy({"catalog_item_id": "CAT_AC_X", "category": "access"}, _policies())
    assert p["stages"][0]["approver"]["type"] == "owning_group"


def test_unknown_category_routes_to_catch_all() -> None:
    p = match_policy({"category": "quantum-teleporter"}, _policies())
    assert p["policy_id"] == "fallback_service_desk"
    assert p["stages"][0]["approver"]["type"] == "service_desk"


def test_no_category_still_matches_catch_all() -> None:
    """An item with no category never dead-ends — the catch-all catches it."""
    p = match_policy({"catalog_item_id": "CAT_WEIRD"}, _policies())
    assert p["policy_id"] == "fallback_service_desk"


def test_priority_strictly_wins() -> None:
    """Two rules match → the lower-priority (earlier) one is returned."""
    policies = [
        {"policy_id": "specific", "priority": 5, "match": {"category": "x"}, "required": True, "stages": [1]},
        {"policy_id": "general", "priority": 50, "match": {}, "required": True, "stages": [2]},
    ]
    assert match_policy({"category": "x"}, policies)["policy_id"] == "specific"
    assert match_policy({"category": "other"}, policies)["policy_id"] == "general"
