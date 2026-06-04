"""Smoke — the service boots and its hermetic critical surface is alive.

No external infra (LLM / Postgres / NATS) is required: this exercises app import,
the lifespan boot, and the registry-only endpoints (health, config, fast-path
spec). It is the first sanity gate before/after any change — if this is red the
system is broken at the most basic level. Live-dependency happy paths (a full
chat turn, UC DB reads) are integration-lane, not here.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from oneops.api.app import build_app


@pytest.fixture(scope="module")
def client():
    # Boot the app ONCE for the whole smoke module: each TestClient lifespan
    # starts/stops the embedding worker, and repeated sequential lifespans in one
    # process don't drain cleanly (the suite hangs). One boot is also the right
    # shape for a smoke test — exercise many surfaces against a single live app.
    import os
    prev = os.environ.get("UC_INVOKER_MODE")
    os.environ["UC_INVOKER_MODE"] = "local"  # no embedded NATS worker
    try:
        app = build_app()
        with TestClient(app) as c:
            yield c
    finally:
        if prev is None:
            os.environ.pop("UC_INVOKER_MODE", None)
        else:
            os.environ["UC_INVOKER_MODE"] = prev


def test_app_imports_and_builds():
    # Importing + building the app must not raise (catches import-time side
    # effects, bad wiring, registry load failures).
    assert build_app() is not None


def test_health_ok_with_active_agents(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["active_agents"] >= 1
    assert isinstance(body["fast_path_eligible"], list)


def test_config_reports_all_subsystems(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    cfg = r.json()
    for key in ("cache", "otel", "llm_gateway", "postgres", "nats", "session"):
        assert key in cfg, f"config missing subsystem {key!r}"


def test_every_fast_path_uc_exposes_a_valid_spec(client):
    """Each registry-eligible fast-path UC returns a well-formed spec with a
    human-readable display_name (Rename Option A) and its stable wire uc_id."""
    eligible = client.get("/api/health").json()["fast_path_eligible"]
    assert eligible, "expected at least one fast-path-eligible use case"
    for uc_id in eligible:
        r = client.get(f"/api/fast/{uc_id}/spec")
        assert r.status_code == 200, f"spec failed for {uc_id}"
        spec = r.json()
        assert spec["uc_id"] == uc_id                 # wire id (contract) intact
        assert spec["display_name"]                   # human label present
        assert not spec["display_name"].lower().startswith("uc0")
        assert isinstance(spec["input_fields"], list)


def test_unknown_fast_path_uc_is_404_not_500(client):
    r = client.get("/api/fast/uc99_does_not_exist/spec")
    assert r.status_code == 404
