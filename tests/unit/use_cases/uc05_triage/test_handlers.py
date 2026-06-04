"""UC-5 standard registry handlers (B-refactor Phase 1) — wiring contract.

Tests the standard `(arguments, context) -> dict` wrappers: dependency gating,
input validation, tenant-scoped row load, adapter build, and serialization. The
underlying tool impls are mocked here (they have their own suites) — this locks
the HANDLER contract the executor will dispatch. Hermetic (no DB/gateway).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

import oneops.use_cases.uc05_triage.handlers as h


class _Result:
    """Stub tool result with model_dump() (handlers call .model_dump())."""
    def __init__(self, payload: dict[str, Any]) -> None:
        self._p = payload

    def model_dump(self) -> dict[str, Any]:
        return self._p


class _Store:
    def __init__(self, row: dict | None) -> None:
        self._row = row

    async def get_ticket(self, *, service_id: str, ticket_id: str, tenant_id: str):
        if self._row is None:
            raise KeyError(ticket_id)
        return self._row


async def _provider():
    return MagicMock(name="conn")


@pytest.fixture
def wired(monkeypatch):
    """Wire dependencies + stub the 3 tool impls; restore after."""
    h.set_uc05_gateway(MagicMock(name="gateway"))
    h.set_uc05_connection_provider(_provider)
    h.set_uc05_ticket_store(_Store({"incident_id": "INC0000001", "title": "x"}))
    monkeypatch.setattr(h, "check_duplicate_candidates",
                        lambda **k: _ok({"candidates": [], "verdict": "no_duplicate"}))
    monkeypatch.setattr(h, "prioritize_entity",
                        lambda **k: _ok({"priority": "Medium"}))
    monkeypatch.setattr(h, "recommend_assignment",
                        lambda **k: _ok({"assignment_group": "GRP-X"}))
    yield
    h.set_uc05_gateway(None)
    h.set_uc05_connection_provider(None)
    h.set_uc05_ticket_store(None)


def _ok(payload):
    async def _coro(**_kwargs):
        return _Result(payload)
    return _coro()  # return an awaitable


_CTX = {"tenant_id": "T001", "user_id": "u1", "role": "service_desk_agent"}


# ── dependency gating ────────────────────────────────────────────────────────


async def test_handlers_report_dependency_unavailable_when_unwired():
    h.set_uc05_gateway(None); h.set_uc05_connection_provider(None)
    h.set_uc05_ticket_store(None)
    out = await h.check_duplicates({"service_id": "incident", "ticket_id": "INC1"}, _CTX)
    assert out["outcome"] == "dependency_unavailable"


# ── input validation ─────────────────────────────────────────────────────────


async def test_check_duplicates_invalid_request(wired):
    out = await h.check_duplicates({"service_id": "incident"}, _CTX)  # no ticket_id
    assert out["outcome"] == "invalid_request"


async def test_prioritize_invalid_request(wired):
    out = await h.prioritize({"ticket_id": "INC1"}, _CTX)             # no service_id
    assert out["outcome"] == "invalid_request"


# ── not-found (tenant-scoped, no leak) ───────────────────────────────────────


async def test_check_duplicates_not_found(wired):
    h.set_uc05_ticket_store(_Store(None))                            # row missing
    out = await h.check_duplicates(
        {"service_id": "incident", "ticket_id": "INC9"}, _CTX)
    assert out["outcome"] == "not_found"


# ── happy path: handler loads row, calls impl, returns model_dump dict ───────


async def test_check_duplicates_returns_serialized_result(wired):
    out = await h.check_duplicates(
        {"service_id": "incident", "ticket_id": "INC0000001"}, _CTX)
    assert out == {"candidates": [], "verdict": "no_duplicate"}   # the impl's model_dump


async def test_prioritize_returns_serialized_result(wired):
    out = await h.prioritize(
        {"service_id": "incident", "ticket_id": "INC0000001",
         "suggested_category": "network"}, _CTX)
    assert out == {"priority": "Medium"}


async def test_recommend_assignment_accepts_bound_candidates(wired):
    # candidates arrive as dicts (bound from Tool 1's serialized output)
    out = await h.recommend_assignment_handler(
        {"candidates": [], "probe_text": "vpn"}, _CTX)
    assert out == {"assignment_group": "GRP-X"}
