"""P0-3 — internal exception detail must not leak past the API boundary.

Devil's-advocate: force the engine to raise, then assert the HTTP 500 body
carries an OPAQUE message (+ a request_id for correlation) and NEVER the internal
exception text. The status code (500) is preserved — clients may key on it.
See docs/planning/production-readiness-audit.md P0-3 + change-log Batch B.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import oneops.api.app as app_module
from oneops.api.app import build_app
from oneops.errors import OneOpsError

_SECRET = "INTERNAL_DB_DSN=postgres://user:p4ssw0rd@host/db leaked in exc text"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UC_INVOKER_MODE", "local")
    # AGENT_TRANSPORT=local is forced for all unit tests by the autouse
    # `_hermetic_agent_transport` fixture in tests/unit/conftest.py (so the app
    # lifespan starts no NATS workers that would deadlock teardown).
    app = build_app()
    with TestClient(app) as c:
        yield c


def _headers():
    return {"x-tenant-id": "T001", "x-user-id": "oneops",
            "x-role": "service_desk_agent"}


def test_oneops_error_detail_is_opaque(client, monkeypatch):
    async def _boom(*_a, **_k):
        raise OneOpsError(_SECRET)

    # local mode calls the module-level run_turn — make it raise.
    monkeypatch.setattr(app_module, "run_turn", _boom)

    r = client.post("/api/chat", headers=_headers(),
                    json={"message": "anything", "session_id": "s_leak_1"})
    assert r.status_code == 500
    detail = r.json()["detail"]
    # opaque + correlatable, but the internal text never crosses the boundary.
    assert "engine failure" in detail
    assert "request_id=" in detail
    assert _SECRET not in detail
    assert "postgres://" not in detail
    assert "p4ssw0rd" not in detail


def test_unexpected_error_detail_is_opaque(client, monkeypatch):
    async def _boom(*_a, **_k):
        raise RuntimeError(_SECRET)

    monkeypatch.setattr(app_module, "run_turn", _boom)

    r = client.post("/api/chat", headers=_headers(),
                    json={"message": "anything", "session_id": "s_leak_2"})
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "engine failure" in detail
    # neither the message nor the exception class name leaks.
    assert _SECRET not in detail
    assert "RuntimeError" not in detail
