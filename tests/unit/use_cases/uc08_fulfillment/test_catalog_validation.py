"""Unit + devil's-play coverage for the catalog completeness guardrail.

These are the durable root-cause guard behind the split-brain onboarding
bug: they prove a malformed catalog (an `automated` task with no tool_id,
an unknown tool_id, or a tool task whose input_template can't be
dispatched) is rejected at load time rather than silently no-op'ing at
runtime. Pure unit — no DB, no LLM.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from oneops.use_cases.uc08_fulfillment.adapters.protocol import (
    APPROVAL_TOOL_ID,
    COMPENSATION_TOOL_IDS,
    FORWARD_TOOL_IDS,
    VALID_TASK_TOOL_IDS,
    IntegrationAdapter,
)
from oneops.use_cases.uc08_fulfillment.catalog_validation import (
    CatalogValidationError,
    required_params,
    validate_catalog_items,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


# ── The tool surface is the single source of truth ──────────────────────────


def _protocol_methods() -> set[str]:
    attrs = getattr(IntegrationAdapter, "__protocol_attrs__", None)
    if attrs is None:  # pragma: no cover - py<3.12 fallback
        attrs = {a for a in dir(IntegrationAdapter) if not a.startswith("_")}
    return set(attrs)


def test_forward_tool_ids_match_protocol() -> None:
    """FORWARD/COMPENSATION sets must exactly cover the Protocol methods —
    a renamed/added integration can't silently drift from the validator."""
    assert _protocol_methods() == FORWARD_TOOL_IDS | COMPENSATION_TOOL_IDS
    assert FORWARD_TOOL_IDS.isdisjoint(COMPENSATION_TOOL_IDS)


def test_valid_task_tool_ids_is_forward_plus_approval() -> None:
    assert FORWARD_TOOL_IDS | {APPROVAL_TOOL_ID} == VALID_TASK_TOOL_IDS


def test_required_params_introspected_from_protocol() -> None:
    # Excludes the framework-supplied tenant_id / idempotency_key.
    assert required_params("create_directory_account") == {
        "user_full_name", "email_suggested"}
    assert required_params("notify_milestone") == {
        "recipient_user_id", "message", "level"}
    assert required_params("grant_vpn_access") == {"user_id"}
    assert required_params("not_a_tool") == frozenset()


# ── Valid catalogs pass ─────────────────────────────────────────────────────


def test_manual_only_item_passes() -> None:
    validate_catalog_items([
        {"catalog_item_id": "CAT_M", "tasks": [
            {"task_id": "T1", "type": "manual", "owner_group": "GRP-X"},
            {"task_id": "T2", "type": "manual", "owner_group": "GRP-Y"},
        ]},
    ])


def test_tool_task_with_matching_template_passes() -> None:
    validate_catalog_items([
        {"catalog_item_id": "CAT_O", "tasks": [
            {"task_id": "T1", "type": "automated",
             "tool_id": "grant_vpn_access",
             "input_template": {"user_id": "{requested_for}"}},
            {"task_id": "T2", "type": "automated",
             "tool_id": "notify_milestone",
             "input_template": {"recipient_user_id": "{requested_for}",
                                "message": "done", "level": "info"}},
        ]},
    ])


def test_approval_pseudo_tool_skips_template_check() -> None:
    # request_human_approval is valid but is NOT an integration, so it is
    # exempt from the input_template parameter match.
    validate_catalog_items([
        {"catalog_item_id": "CAT_A", "tasks": [
            {"task_id": "T1", "type": "automated",
             "tool_id": APPROVAL_TOOL_ID},
        ]},
    ])


def test_manual_task_without_tool_id_is_fine() -> None:
    validate_catalog_items([
        {"catalog_item_id": "CAT_M", "tasks": [
            {"task_id": "T1", "type": "manual"}]},
    ])


def test_empty_and_taskless_items_pass() -> None:
    validate_catalog_items([{"catalog_item_id": "CAT_E"}, {"catalog_item_id": "CAT_E2", "tasks": []}])


# ── Devil's play — every malformed shape is rejected with a precise reason ──


@pytest.mark.parametrize("task, needle", [
    # automated task with no tool_id → the silent-no-op defect
    ({"task_id": "T1", "type": "automated"},
     "type=automated but no tool_id"),
    ({"task_id": "T1", "type": "automated", "tool_id": ""},
     "type=automated but no tool_id"),
    ({"task_id": "T1", "type": "automated", "tool_id": None},
     "type=automated but no tool_id"),
    # stale / typo'd tool_id (the R-17 rename class)
    ({"task_id": "T1", "type": "automated", "tool_id": "provision_mailbox"},
     "not a known integration"),
    ({"task_id": "T1", "type": "manual", "tool_id": "frobnicate"},
     "not a known integration"),
    # a compensation method is NOT a forward task tool
    ({"task_id": "T1", "type": "automated", "tool_id": "revoke_vpn_access"},
     "not a known integration"),
    # tool task whose template can't be dispatched (extra key)
    ({"task_id": "T1", "type": "automated", "tool_id": "grant_vpn_access",
      "input_template": {"user_id": "{x}", "bogus": "y"}},
     "input_template for 'grant_vpn_access'"),
    # tool task missing a required param
    ({"task_id": "T1", "type": "automated", "tool_id": "notify_milestone",
      "input_template": {"message": "hi"}},
     "input_template for 'notify_milestone'"),
    # tool task with no template at all (all params missing)
    ({"task_id": "T1", "type": "automated", "tool_id": "order_hardware_asset"},
     "input_template for 'order_hardware_asset'"),
])
def test_devils_play_rejects(task: dict, needle: str) -> None:
    with pytest.raises(CatalogValidationError) as exc:
        validate_catalog_items([{"catalog_item_id": "CAT_X", "tasks": [task]}])
    assert needle in str(exc.value)
    assert "CAT_X" in str(exc.value)


def test_all_problems_aggregated_not_fail_on_first() -> None:
    bad = {"catalog_item_id": "CAT_X", "tasks": [
        {"task_id": "T1", "type": "automated"},                       # no tool
        {"task_id": "T2", "type": "automated", "tool_id": "bogus"},   # unknown
        {"task_id": "T3", "type": "automated", "tool_id": "grant_vpn_access",
         "input_template": {"wrong": "x"}},                           # bad tmpl
    ]}
    with pytest.raises(CatalogValidationError) as exc:
        validate_catalog_items([bad])
    msg = str(exc.value)
    assert "3 catalog task invariant violation" in msg
    assert "T1" in msg and "T2" in msg and "T3" in msg


def test_one_bad_item_in_a_good_batch_still_fails() -> None:
    good = {"catalog_item_id": "CAT_OK", "tasks": [
        {"task_id": "T1", "type": "manual"}]}
    bad = {"catalog_item_id": "CAT_BAD", "tasks": [
        {"task_id": "T1", "type": "automated"}]}
    with pytest.raises(CatalogValidationError):
        validate_catalog_items([good, bad])


# ── Integration on the SHIPPED data — the real catalog honours the invariant ─


def test_shipped_catalog_json_is_valid() -> None:
    items = json.loads((REPO_ROOT / "data/itsm/catalog_item.json").read_text())
    # Must not raise — every seeded catalog item is dispatchable.
    validate_catalog_items(items)


def test_shipped_catalog_has_no_automated_without_tool() -> None:
    items = json.loads((REPO_ROOT / "data/itsm/catalog_item.json").read_text())
    offenders = [
        f"{it['catalog_item_id']}/{t['task_id']}"
        for it in items for t in it.get("tasks", [])
        if t.get("type") == "automated" and not t.get("tool_id")
    ]
    assert offenders == [], offenders


# ── Integration — the loader's validate hook blocks the write on bad data ────


@pytest.mark.asyncio
async def test_loader_hook_rejects_before_any_db_write(tmp_path) -> None:
    """End-to-end of the guardrail wiring: a malformed catalog file fed
    through `load_table(..., validate=validate_catalog_items)` raises
    BEFORE the executemany — the DB is never touched."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "database"))
    from _lib._loader import load_table  # noqa: E402  (mirrors load_data.py)

    (tmp_path / "catalog_item.json").write_text(json.dumps([
        {"tenant_id": "T", "catalog_item_id": "CAT_BAD",
         "tasks": [{"task_id": "T1", "type": "automated"}]},
    ]))

    class _BoomConn:
        async def executemany(self, *a, **k):
            raise AssertionError("DB write must not happen for invalid data")

    with pytest.raises(CatalogValidationError):
        await load_table(
            _BoomConn(), "catalog_item",
            [("tenant_id", "s"), ("catalog_item_id", "s"), ("tasks", "J[]")],
            data_dir=tmp_path, validate=validate_catalog_items,
        )
