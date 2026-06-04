"""Rename Option A — use-case names are human-readable, wire ids unchanged.

Covers the display-name helper, the fast-path session message, and the
`/api/fast/{uc_id}/spec` contract. The `ucNN_` prefix is a stable wire id
(routes/registry); these tests lock that it is NEVER shown to a human while the
id itself stays put. See docs/change-log.md (Rename Option A) + audit.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from oneops.api.app import (
    _humanise_fast_path_request,
    _uc_display_name,
    build_app,
)

# ── fakes ────────────────────────────────────────────────────────────────────


class _Agent:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeAgents:
    def __init__(self, mapping: dict[str, _Agent]) -> None:
        self._m = mapping

    def get_optional(self, uc_id: str):
        return self._m.get(uc_id)


class _FakeRegistry:
    def __init__(self, mapping: dict[str, _Agent]) -> None:
        self.agents = _FakeAgents(mapping)


# ── _uc_display_name — derivation fallback (no registry) ─────────────────────


@pytest.mark.parametrize(("uc_id", "expected"), [
    ("uc01_summarization", "Summarization"),
    ("uc02_similar_tickets", "Similar Tickets"),
    ("uc05_triage", "Triage"),
    ("uc08_fulfillment", "Fulfillment"),
])
def test_display_name_derives_from_uc_id(uc_id, expected):
    assert _uc_display_name(uc_id) == expected


def test_display_name_never_exposes_wire_prefix():
    for uc in ("uc01_summarization", "uc02_similar_tickets",
               "uc05_triage", "uc08_fulfillment"):
        out = _uc_display_name(uc)
        assert not out.lower().startswith("uc0")
        assert "uc0" not in out.lower()


# ── _uc_display_name — registry is the source of truth ───────────────────────


def test_display_name_prefers_registry_name_minus_agent_suffix():
    reg = _FakeRegistry({"uc03_kb_lookup": _Agent("Knowledge Base Lookup Agent")})
    assert _uc_display_name("uc03_kb_lookup", reg) == "Knowledge Base Lookup"


def test_display_name_falls_back_when_agent_missing():
    reg = _FakeRegistry({})  # not found → derive from uc_id
    assert _uc_display_name("uc08_fulfillment", reg) == "Fulfillment"


# ── fast-path session message uses the descriptive name, not the wire id ─────


def test_humanise_fast_path_uses_descriptive_name():
    # uc05 has no custom phrasing → hits the generic fallback (the bug site).
    msg = _humanise_fast_path_request("uc05_triage", {"ticket_id": "INC1"})
    assert msg == "Run Triage: INC1"
    assert "uc05" not in msg.lower()


def test_humanise_fast_path_prefers_registry_name():
    reg = _FakeRegistry({"uc02_similar_tickets": _Agent("Similar Tickets Agent")})
    msg = _humanise_fast_path_request(
        "uc02_similar_tickets", {"ticket_id": "INC1"}, reg)
    assert msg == "Run Similar Tickets: INC1"


# ── /api/fast/{uc_id}/spec — display_name added, uc_id (contract) unchanged ───


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UC_INVOKER_MODE", "local")
    app = build_app()
    with TestClient(app) as c:
        yield c


def test_spec_returns_display_name_and_keeps_wire_id(client):
    r = client.get("/api/fast/uc01_summarization/spec")
    assert r.status_code == 200
    body = r.json()
    # wire id is a CONTRACT — unchanged.
    assert body["uc_id"] == "uc01_summarization"
    # new, human-facing field — descriptive, no prefix.
    assert body["display_name"] == "Summarization"
