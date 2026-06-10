"""Step 5 — the evaluator `resolve_approvers` (composition logic).

Drives the REAL matrix (data/itsm/approval_policy.json) with injected stub
resolvers so the composition — required-false short-circuit, stage dispatch,
self-approval guard, fail-safe to the (data-defined) service desk, and the
never-auto-approve guard — is tested deterministically without a DB.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from oneops.use_cases.uc08_fulfillment.approval import resolve_approvers

pytestmark = pytest.mark.asyncio

_MATRIX = Path(__file__).resolve().parents[4] / "data" / "itsm" / "approval_policy.json"


def _policies() -> list[dict]:
    raw = json.loads(_MATRIX.read_text(encoding="utf-8"))["policies"]
    return sorted(raw, key=lambda p: p["priority"])


def _group_resolver(by_group: dict[str, list[str]]):
    async def gr(*, owner_group, tenant_id, conn):
        return list(by_group.get(owner_group, []))
    return gr


def _manager_resolver(mgr: str | None):
    async def mr(*, requester_id, tenant_id, conn):
        return mgr
    return mr


async def _run(item, requester="REQ", *, groups=None, mgr=None):
    return await resolve_approvers(
        item=item, requester_id=requester, tenant_id="T001", conn=None,
        policies=_policies(),
        group_resolver=_group_resolver(groups or {}),
        manager_resolver=_manager_resolver(mgr))


# ── Canonical matrix ────────────────────────────────────────────────────────
async def test_laptop_hardware_to_manager() -> None:
    d = await _run({"catalog_item_id": "CAT_HW_X", "category": "hardware", "owner_group": "GRP-ASSET"}, mgr="MGR")
    assert d.required and d.approver_type == "manager_of_requester"
    assert d.approvers == ("MGR",) and not d.fell_back and d.resolved


async def test_vpn_access_to_owning_group() -> None:
    d = await _run({"catalog_item_id": "CAT_AC_VPN", "category": "access", "owner_group": "GRP-NETOPS"},
                   groups={"GRP-NETOPS": ["A", "B"]})
    assert d.approver_type == "owning_group" and d.approvers == ("A", "B")


async def test_password_is_not_required() -> None:
    d = await _run({"catalog_item_id": "CAT_SE_PASSWORD", "category": "security", "owner_group": "GRP-SECOPS"})
    assert d.required is False and d.approvers == () and d.resolved is True


async def test_unknown_category_to_service_desk() -> None:
    d = await _run({"catalog_item_id": "CAT_X", "category": "made-up", "owner_group": None},
                   groups={"GRP-SERVICE-DESK": ["SD1"]})
    assert d.policy_id == "fallback_service_desk" and d.approver_type == "service_desk"
    assert d.approvers == ("SD1",)


# ── Devil's play ────────────────────────────────────────────────────────────
async def test_self_approval_blocked_then_fails_safe() -> None:
    """Requester is their own (only) manager → dropped → escalate to service desk."""
    d = await _run({"catalog_item_id": "CAT_HW_X", "category": "hardware", "owner_group": "GRP-ASSET"},
                   requester="REQ", mgr="REQ", groups={"GRP-SERVICE-DESK": ["SD1"]})
    assert d.fell_back and d.approver_type == "service_desk" and d.approvers == ("SD1",)


async def test_manager_null_fails_safe_to_service_desk() -> None:
    d = await _run({"catalog_item_id": "CAT_HW_X", "category": "hardware", "owner_group": "GRP-ASSET"},
                   mgr=None, groups={"GRP-SERVICE-DESK": ["SD1"]})
    assert d.fell_back and d.approvers == ("SD1",)


async def test_owning_group_empty_fails_safe() -> None:
    d = await _run({"catalog_item_id": "CAT_AC_X", "category": "access", "owner_group": "GRP-NETOPS"},
                   groups={"GRP-NETOPS": [], "GRP-SERVICE-DESK": ["SD1"]})
    assert d.fell_back and d.approver_type == "service_desk" and d.approvers == ("SD1",)


async def test_nobody_can_approve_is_unresolved_never_auto_approved() -> None:
    """Owning group AND service desk both empty → required but unresolved.
    resolved=False so the gate must hold — NEVER auto-approve."""
    d = await _run({"catalog_item_id": "CAT_AC_X", "category": "access", "owner_group": "GRP-NETOPS"},
                   groups={})  # nothing resolves, incl. service desk
    assert d.required is True and d.approvers == () and d.resolved is False


async def test_requester_filtered_from_group() -> None:
    """Self-approval guard drops the requester even from a multi-member group."""
    d = await _run({"catalog_item_id": "CAT_AC_X", "category": "access", "owner_group": "GRP-NETOPS"},
                   requester="B", groups={"GRP-NETOPS": ["A", "B", "C"]})
    assert d.approvers == ("A", "C")  # B (the requester) removed, no fallback
