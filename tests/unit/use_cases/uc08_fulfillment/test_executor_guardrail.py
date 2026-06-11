"""Unit coverage for the executor's no-tool guardrail (defence-in-depth).

The load-time validator makes an `automated`-without-tool_id task
unreachable for seeded data; this proves that *if* one ever reaches the
executor (a non-validated write path), it FAILS LOUD instead of silently
being marked done — and that a genuine `manual` task still completes.

Pure unit: `_db.transition_task_state` is monkeypatched, so no DB.
"""
from __future__ import annotations

import pytest

from oneops.use_cases.uc08_fulfillment import executor


class _FakeConn:
    async def close(self) -> None:  # the executor closes the per-task conn
        pass


async def _cp() -> _FakeConn:
    return _FakeConn()


@pytest.fixture
def transitions(monkeypatch):
    """Capture every state transition the executor attempts."""
    calls: list[dict] = []

    async def _fake_transition(*, tenant_id, task_id, from_state, to_state,
                               version, output_payload=None,
                               error_message=None, error_code=None,
                               retry_count=None, conn):
        calls.append({
            "to_state": to_state, "output_payload": output_payload,
            "error_message": error_message, "error_code": error_code,
        })
        return version + 1  # transition committed → new version

    monkeypatch.setattr(executor._db, "transition_task_state", _fake_transition)
    return calls


def _task(**over) -> dict:
    base = {
        "task_id": "T9", "tool_id": None, "task_type": "automated",
        "input_payload": {}, "version": 1, "state": "ready",
        "retry_count": 0, "max_retries": 3, "task_name": "Ghost step",
        "assignment_group": "GRP-X",
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_automated_without_tool_id_fails_loud(transitions) -> None:
    result = await executor._execute_one_task(
        tenant_id="T001", ritm_id="RITM_1",
        task=_task(task_type="automated", tool_id=None),
        adapter=object(), connection_provider=_cp,
    )
    assert result == "failed"
    terminal = transitions[-1]
    assert terminal["to_state"] == "failed"
    assert terminal["error_code"] == "catalog_misconfigured"
    assert "no" in terminal["error_message"] and "tool_id" in terminal["error_message"]
    # The whole point: it is NEVER silently marked done.
    assert all(c["to_state"] != "done" for c in transitions)


@pytest.mark.asyncio
async def test_manual_without_tool_id_completes(transitions) -> None:
    result = await executor._execute_one_task(
        tenant_id="T001", ritm_id="RITM_1",
        task=_task(task_type="manual", tool_id=None),
        adapter=object(), connection_provider=_cp,
    )
    assert result == "done"
    terminal = transitions[-1]
    assert terminal["to_state"] == "done"
    assert terminal["output_payload"] == {"resolved": "manual_no_tool"}


@pytest.mark.asyncio
async def test_inflight_lost_race_returns_current_state(transitions, monkeypatch) -> None:
    # If the in_progress transition is lost (another worker won), the task
    # stays put — no spurious fail/done.
    async def _none(*a, **k):
        return None
    monkeypatch.setattr(executor._db, "transition_task_state", _none)
    result = await executor._execute_one_task(
        tenant_id="T001", ritm_id="RITM_1",
        task=_task(task_type="automated", tool_id=None, state="ready"),
        adapter=object(), connection_provider=_cp,
    )
    assert result == "ready"
