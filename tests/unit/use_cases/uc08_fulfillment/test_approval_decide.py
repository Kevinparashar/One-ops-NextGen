"""Step 7′ — the non-chat approve/reject service (`decide_approval`).

Hermetic: a fake conn + monkeypatched db helpers drive every branch —
approve (releases → should_dispatch), reject (stops), wrong actor (denied),
already-decided (idempotent), bad decision (rejected input).
"""
from __future__ import annotations

import pytest

from oneops.use_cases.uc08_fulfillment import approval as _approval
from oneops.use_cases.uc08_fulfillment import db as _db

pytestmark = pytest.mark.asyncio


class _FakeConn:
    def transaction(self):
        class _Tx:
            async def __aenter__(_s):
                return None

            async def __aexit__(_s, *_a):
                return False
        return _Tx()


@pytest.fixture
def patched(monkeypatch):
    """Record-only db helpers + a controllable approval row."""
    state = {"approval": {"ritm_id": "RITM1", "requested_from": "MGR",
                          "state": "pending"},
             "withdrawn": 0, "outcome": None, "lifecycle": None}

    async def get_approval(**_kw):
        return state["approval"]

    async def update_decision(**_kw):
        return "RITM1" if state["approval"]["state"] == "pending" else None

    async def withdraw(**_kw):
        state["withdrawn"] += 1
        return 1

    async def outcome(**kw):
        state["outcome"] = kw["approved"]
        return "REQ1"  # parent request_id (transitioned)

    async def set_lifecycle(**kw):
        state["lifecycle"] = (kw["status"], kw["stage"])

    monkeypatch.setattr(_db, "get_approval", get_approval)
    monkeypatch.setattr(_db, "update_approval_decision", update_decision)
    monkeypatch.setattr(_db, "withdraw_other_pending_approvals", withdraw)
    monkeypatch.setattr(_db, "apply_approval_outcome", outcome)
    monkeypatch.setattr(_db, "set_request_lifecycle", set_lifecycle)
    return state


async def _decide(decision, decided_by="MGR"):
    return await _approval.decide_approval(
        approval_id="APP1", decision=decision, decided_by=decided_by,
        tenant_id="T001", conn=_FakeConn())


async def test_approve_releases_fulfilment(patched) -> None:
    out = await _decide("approved")
    assert out.ok and out.state == "approved"
    assert out.should_dispatch is True          # caller releases the held NATS
    assert patched["withdrawn"] == 1 and patched["outcome"] is True
    # parent SR stamped so the requester sees it via UC-1 / TRACK
    assert patched["lifecycle"] == ("approved", "fulfillment")


async def test_reject_stops_no_dispatch(patched) -> None:
    out = await _decide("rejected")
    assert out.ok and out.state == "rejected"
    assert out.should_dispatch is False         # nothing fulfilled
    assert patched["outcome"] is False and patched["withdrawn"] == 0
    assert patched["lifecycle"] == ("rejected", "closed")


async def test_wrong_actor_denied(patched) -> None:
    out = await _decide("approved", decided_by="SOMEONE_ELSE")
    assert out.ok is False and out.should_dispatch is False
    assert "not the assigned approver" in out.message
    assert patched["outcome"] is None           # no state change


async def test_already_decided_is_idempotent(patched) -> None:
    patched["approval"]["state"] = "approved"   # already decided
    out = await _decide("approved")
    assert out.ok and out.should_dispatch is False
    assert patched["outcome"] is None


async def test_bad_decision_rejected(patched) -> None:
    out = await _decide("maybe")
    assert out.ok is False and "approved" in out.message
    assert patched["outcome"] is None
