"""Durable session — UC-1 contract task D.

Two turns under the same `session_id` must:
  * persist the first turn to the SessionEventStore (cold + hot)
  * load it as conversation history at the start of the second turn
  * keep tenant isolation structural — a different tenant on the same
    session_id sees an empty history (no transcript leak)

Plus the rehydrate endpoint `/api/session/{id}/history` returns the events
the next page-load can render.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from oneops.api.app import build_app


def _unique_session_id(prefix: str) -> str:
    """Each test gets its own session id so Dragonfly-persisted sessions
    from prior runs don't leak into the assertion. Without this, the
    durable backend correctly accumulates messages across CI runs and
    the per-test count grows unboundedly."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def client(monkeypatch):
    # Unit tests run the app in-process; force UC_INVOKER_MODE=local so the
    # lifespan does NOT try to attach an embedded GraphWorker to NATS.
    # NATS-mode is exercised by integration tests against a live broker.
    monkeypatch.setenv("UC_INVOKER_MODE", "local")
    app = build_app()
    with TestClient(app) as c:
        yield c


def _headers(tenant="T001", user="oneops", role="service_desk_agent"):
    return {"x-tenant-id": tenant, "x-user-id": user, "x-role": role}


# ── persist + rehydrate via the public history endpoint ────────────────


def test_session_history_starts_empty_for_new_session(client):
    r = client.get("/api/session/sess_does_not_exist/history",
                   headers=_headers())
    assert r.status_code == 200
    assert r.json()["events"] == []


# Integration-class: these three run the FULL chat pipeline, which calls the live
# LLM gateway — they hang offline (no stub), so they are not unit tests. Marked
# `integration` to run in the integration lane (live LLM / OpenAI routing). Future
# improvement: stub the gateway and return them to the unit lane. See P0-1 in
# docs/production-readiness-audit.md.
@pytest.mark.integration
def test_two_chat_turns_on_one_session_id_persist_both(client):
    session_id = _unique_session_id("sess_e2e_test_durable")
    # Turn 1
    r1 = client.post(
        "/api/chat", headers=_headers(),
        json={"message": "first question", "session_id": session_id})
    assert r1.status_code == 200
    assert r1.json()["session_id"] == session_id

    # Turn 2 — same session
    r2 = client.post(
        "/api/chat", headers=_headers(),
        json={"message": "follow up question", "session_id": session_id})
    assert r2.status_code == 200

    # History endpoint sees BOTH user messages and BOTH assistant replies.
    hist = client.get(
        f"/api/session/{session_id}/history", headers=_headers()).json()
    events = hist["events"]
    user_msgs = [e for e in events if e["role"] == "user"]
    asst_msgs = [e for e in events if e["role"] == "assistant"]
    assert len(user_msgs) == 2
    assert len(asst_msgs) == 2
    # turn_index is monotonically increasing per the persist contract.
    assert all(events[i]["turn_index"] <= events[i + 1]["turn_index"]
               for i in range(len(events) - 1))
    assert user_msgs[0]["content"] == "first question"
    assert user_msgs[1]["content"] == "follow up question"


@pytest.mark.integration
def test_history_is_tenant_isolated(client):
    session_id = _unique_session_id("sess_e2e_tenant_iso")
    # Tenant A writes a turn
    client.post(
        "/api/chat", headers=_headers(tenant="T001"),
        json={"message": "tenant-A turn", "session_id": session_id})
    # Tenant B asks for the same session_id — must see NOTHING.
    other = client.get(
        f"/api/session/{session_id}/history",
        headers=_headers(tenant="T002")).json()
    assert other["events"] == []


# ── fast-path turns also persist to the same session ───────────────────


@pytest.mark.integration
def test_fast_path_turn_persists_into_session_too(client):
    session_id = _unique_session_id("sess_e2e_fastpath")
    r = client.post(
        "/api/fast/uc01_summarization", headers=_headers(),
        json={"inputs": {"ticket_id": "INC0001001"}, "session_id": session_id})
    assert r.status_code == 200
    hist = client.get(
        f"/api/session/{session_id}/history", headers=_headers()).json()
    events = hist["events"]
    # Fast-path stamps a synthetic user message ("(uc01_summarization
    # fast-path: …)") plus the assistant reply.
    assert any(e["role"] == "user" for e in events)
    assert any(e["role"] == "assistant" for e in events)


# ── config endpoint reports the session subsystem state ────────────────


def test_config_reports_session_wired(client):
    cfg = client.get("/api/config").json()
    assert cfg["session"]["wired"] is True
    assert cfg["session"]["durable_across_reload"] is True
    assert "SessionEventStore" in cfg["session"]["backend"]
