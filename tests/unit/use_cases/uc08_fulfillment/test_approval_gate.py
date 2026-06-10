"""Step 6 — the create-path approval gate (`_apply_approval_gate`).

Hermetic: a fake conn + monkeypatched DB writes + a canned ApprovalDecision drive
the gate's three branches deterministically —
  required+resolved → PARK (write one approval row per approver, set approval_state, NOT dispatched)
  not required      → PROCEED (return None → caller dispatches)
  required+unresolved → HOLD (no approval rows, set approval_state, NOT dispatched, never auto-approve)
The flag-OFF zero-regression is covered by the existing chat-tools suite.
"""
from __future__ import annotations

import pytest

from oneops.use_cases.uc08_fulfillment import approval as _approval
from oneops.use_cases.uc08_fulfillment import db as _db
from oneops.use_cases.uc08_fulfillment import tools
from oneops.use_cases.uc08_fulfillment.approval import ApprovalDecision

pytestmark = pytest.mark.asyncio


class _FakeConn:
    def __init__(self, item: dict) -> None:
        self._item = item
        self.closed = False

    async def fetchrow(self, _q, *_a):
        return self._item

    def transaction(self):
        class _Tx:
            async def __aenter__(_s):
                return None

            async def __aexit__(_s, *_a):
                return False
        return _Tx()

    async def close(self):
        self.closed = True


@pytest.fixture
def wired(monkeypatch):
    """Wire a fake conn + record-only DB writes; yield (inserts, states)."""
    conn = _FakeConn({"category": "hardware", "owner_group": "GRP-ASSET"})

    async def cp():
        return conn
    tools.set_connection_provider(cp)

    inserts: list[dict] = []
    states: list[dict] = []
    lifecycles: list[dict] = []

    async def fake_insert(**kw):
        inserts.append(kw)
        return "APP_X"

    async def fake_state(**kw):
        states.append(kw)

    async def fake_lifecycle(**kw):
        lifecycles.append(kw)

    monkeypatch.setattr(_db, "insert_approval", fake_insert)
    monkeypatch.setattr(_db, "set_ritm_approval_state", fake_state)
    monkeypatch.setattr(_db, "set_request_lifecycle", fake_lifecycle)
    yield inserts, states, lifecycles
    tools.set_connection_provider(None)


def _patch_decision(monkeypatch, decision):
    async def fake_resolve(**_kw):
        return decision
    monkeypatch.setattr(_approval, "resolve_approvers", fake_resolve)


async def _gate():
    return await tools._apply_approval_gate(
        tenant_id="T001", requester_id="REQ", catalog_id="CAT_X",
        ritm_id="RITM_X", request_id="REQ_X")


async def test_parks_when_required(wired, monkeypatch) -> None:
    inserts, states, lifecycles = wired
    _patch_decision(monkeypatch, ApprovalDecision(
        required=True, policy_id="cat_hardware", approver_type="manager_of_requester",
        approval_type="manager", approvers=("MGR1", "MGR2"), rule="any_one",
        reason="hardware needs manager approval", fell_back=False))
    out = await _gate()
    assert out is not None
    assert out["status"] == "pending_approval" and out["dispatched"] is False
    # one approval row per approver, approval_type from the decision (matrix data)
    assert len(inserts) == 2
    assert {i["requested_from"] for i in inserts} == {"MGR1", "MGR2"}
    assert all(i["approval_type"] == "manager" for i in inserts)
    assert states and states[0]["approval_state"] == "requested"
    # parent SR stamped so the requester sees it via UC-1 / TRACK
    assert lifecycles and lifecycles[0]["status"] == "pending_approval"
    assert lifecycles[0]["stage"] == "approval"


async def test_proceeds_when_not_required(wired, monkeypatch) -> None:
    inserts, states, lifecycles = wired
    _patch_decision(monkeypatch, ApprovalDecision(
        required=False, policy_id="selfservice_password", approver_type="none",
        approval_type="", approvers=(), rule="", reason="self-service", fell_back=False))
    out = await _gate()
    assert out is None              # caller dispatches as today
    assert not inserts and not states and not lifecycles


async def test_holds_when_unresolved_never_auto_approves(wired, monkeypatch) -> None:
    inserts, states, lifecycles = wired
    _patch_decision(monkeypatch, ApprovalDecision(
        required=True, policy_id="cat_access", approver_type="service_desk",
        approval_type="catalog_owner", approvers=(), rule="any_one",
        reason="no approver", fell_back=True))
    out = await _gate()
    assert out is not None
    assert out["status"] == "approval_unresolved" and out["dispatched"] is False
    assert not inserts                          # NO approval rows when nobody can approve
    assert states and states[0]["approval_state"] == "requested"  # parked, not dispatched
    # even when held, stamp the SR so the requester sees "pending approval"
    assert lifecycles and lifecycles[0]["status"] == "pending_approval"
