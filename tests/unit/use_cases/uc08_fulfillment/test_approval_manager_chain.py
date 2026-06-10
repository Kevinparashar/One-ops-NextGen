"""Manager-chain integrity guard for `manager_of_requester` resolution.

Production data-quality invariant over the WHOLE `sys_user` seed (not a
demo-specific check): every manager reference must resolve to a real, same-tenant
user, with no self-reference and no cycles. A broken org chart would silently
misroute approvals, so this fails CI on any seed edit that corrupts it.
"""
from __future__ import annotations

import json
from pathlib import Path

_SEED = Path(__file__).resolve().parents[4] / "data" / "itsm" / "sys_user.json"


def _users() -> list[dict]:
    return json.loads(_SEED.read_text(encoding="utf-8"))


def _index() -> dict[tuple[str, str], dict]:
    return {(u["tenant_id"], u["user_id"]): u for u in _users()}


def test_every_manager_ref_is_a_real_same_tenant_user() -> None:
    """No dangling manager_id — every reference resolves within its tenant."""
    idx = _index()
    dangling = [
        (u["tenant_id"], u["user_id"], u["manager_id"])
        for u in _users()
        if u.get("manager_id")
        and (u["tenant_id"], u["manager_id"]) not in idx
    ]
    assert not dangling, f"manager_id pointing at a non-existent user: {dangling}"


def test_no_self_managed_user() -> None:
    """A user can never be their own manager."""
    selfmgr = [
        (u["tenant_id"], u["user_id"])
        for u in _users()
        if u.get("manager_id") == u["user_id"]
    ]
    assert not selfmgr, f"users managing themselves: {selfmgr}"


def test_manager_chains_terminate_no_cycles() -> None:
    """Walking manager_id upward always terminates (no cycle)."""
    idx = _index()
    for u in _users():
        seen: set[str] = set()
        cur = u
        while cur.get("manager_id"):
            uid = cur["user_id"]
            assert uid not in seen, (
                f"manager cycle through {u['tenant_id']}/{uid}"
            )
            seen.add(uid)
            cur = idx.get((cur["tenant_id"], cur["manager_id"]))
            if cur is None:  # dangling caught by the other test
                break


def test_active_non_top_users_have_a_manager() -> None:
    """Coverage: an active user should have a manager unless they are a
    top-of-chain director (it_director). Catches the gap class Step 3 fixed —
    an active requester with no manager would force the resolver to fail-safe."""
    TOP_ROLES = {"it_director"}
    orphans = [
        (u["tenant_id"], u["user_id"], u.get("role"))
        for u in _users()
        if u.get("is_active", True)
        and not u.get("manager_id")
        and u.get("role") not in TOP_ROLES
    ]
    assert not orphans, f"active non-director users with no manager: {orphans}"
