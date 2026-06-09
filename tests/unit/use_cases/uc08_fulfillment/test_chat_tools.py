"""UC-8 chat catalog tools — the 4 runbook tools (Playbook 3).

Fast-lane unit tests for `tools.get_service_request_list`,
`get_service_request_fields`, `create_service_request`,
`update_service_request`. Everything external is faked — no live DB,
gateway, or NATS — so these run in the default unit gate.

What they pin down:
  • list      — above-floor matches only; has-form search; no-gateway degrade.
  • fields    — schema returned; unknown item → typed error (not a raise).
  • create    — required-field validation; SR-open → fulfil → dispatch; the
                result states whether the DAG was dispatched (no silent drop).
  • update    — shallow field merge; unknown SR → typed error.
  • boundary  — every handler binds tenant from context, never arguments.
"""
from __future__ import annotations

from typing import Any

import pytest

from oneops.use_cases.uc08_fulfillment import tools

pytestmark = pytest.mark.asyncio

_CTX = {"tenant_id": "T001", "user_id": "u-1", "role": "service_desk_agent"}
_EMBED_DIM = 1536


# ── Fakes ───────────────────────────────────────────────────────────────────


class _FakeGateway:
    """Returns a fixed unit-dim vector for any embed() call."""

    async def embed(self, texts, *, model, tenant_id, dimensions):
        return [[0.0] * _EMBED_DIM for _ in texts]


class _FakeConn:
    """Programmable asyncpg-ish connection. Each hook is a callable
    (query, *args) -> value, or a static value."""

    def __init__(self, *, fetch=None, fetchrow=None, fetchval=None):
        self._fetch = fetch
        self._fetchrow = fetchrow
        self._fetchval = fetchval
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    @staticmethod
    def _resolve(hook, query, args):
        return hook(query, *args) if callable(hook) else hook

    async def fetch(self, query, *args):
        return self._resolve(self._fetch, query, args) or []

    async def fetchrow(self, query, *args):
        return self._resolve(self._fetchrow, query, args)

    async def fetchval(self, query, *args):
        return self._resolve(self._fetchval, query, args)

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "INSERT 0 1"

    async def close(self):
        pass


def _provider(conn: _FakeConn):
    async def _cp():
        return conn
    return _cp


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Snapshot + restore the module globals each test touches."""
    saved = (tools._gateway, tools._nats_client, tools._connection_provider)
    yield
    tools._gateway, tools._nats_client, tools._connection_provider = saved


# ── get_service_request_list ────────────────────────────────────────────────


async def test_list_returns_only_above_floor_matches():
    rows = [
        {"catalog_item_id": "CAT_AC_VPN", "name": "VPN / Remote Access",
         "description": "Request VPN remote access", "category": "access",
         "owner_group": "network", "cosine_score": 0.71},
        {"catalog_item_id": "CAT_HW_LAPTOP_STD", "name": "Standard Laptop",
         "description": "A standard business laptop", "category": "hardware",
         "owner_group": "hardware", "cosine_score": 0.40},  # below floor
    ]
    tools.set_gateway(_FakeGateway())
    tools.set_connection_provider(_provider(_FakeConn(fetch=rows)))

    out = await tools.get_service_request_list(
        {"service_catalogs": ["VPN", "remote access"]}, _CTX)

    assert out["ok"] is True
    ids = [m["catalog_id"] for m in out["matches"]]
    assert ids == ["CAT_AC_VPN"]          # the 0.40 row is filtered out
    assert out["count"] == 1


async def test_list_no_match_offers_incident_path():
    tools.set_gateway(_FakeGateway())
    tools.set_connection_provider(_provider(_FakeConn(fetch=[])))
    out = await tools.get_service_request_list({"query": "pizza party"}, _CTX)
    assert out["ok"] is True
    assert out["matches"] == []
    assert "incident" in out["display_text"].lower()


async def test_list_degrades_without_gateway():
    tools.set_gateway(None)
    out = await tools.get_service_request_list(
        {"service_catalogs": ["VPN"]}, _CTX)
    assert out["ok"] is False
    assert out["error_code"] == "UC08_SEARCH_UNAVAILABLE"


async def test_list_requires_keywords():
    tools.set_gateway(_FakeGateway())
    out = await tools.get_service_request_list({"service_catalogs": []}, _CTX)
    assert out["ok"] is False
    assert out["error_code"] == "UC08_BAD_REQUEST"


# ── get_service_request_fields ──────────────────────────────────────────────


_FORM = [
    {"field_name": "model", "label": "Model", "type": "text", "required": True},
    {"field_name": "notes", "label": "Notes", "type": "textarea",
     "required": False},
]


async def test_fields_returns_schema_and_required():
    conn = _FakeConn(fetchrow={"name": "Standard Laptop",
                               "request_fields": _FORM})
    tools.set_connection_provider(_provider(conn))
    out = await tools.get_service_request_fields(
        {"catalog_id": "CAT_HW_LAPTOP_STD"}, _CTX)
    assert out["ok"] is True
    assert out["required"] == ["model"]
    assert out["field_count"] == 2


async def test_fields_unknown_item_is_typed_error_not_raise():
    conn = _FakeConn(fetchrow=None)           # no such row
    tools.set_connection_provider(_provider(conn))
    out = await tools.get_service_request_fields(
        {"catalog_id": "CAT_NOPE"}, _CTX)
    assert out["ok"] is False
    assert "CAT_NOPE" in out["display_text"]


async def test_fields_requires_catalog_id():
    out = await tools.get_service_request_fields({}, _CTX)
    assert out["ok"] is False
    assert out["error_code"] == "UC08_BAD_REQUEST"


# ── create_service_request ──────────────────────────────────────────────────


class _StubOutcome:
    ritm_id = "RITM0000000001"

    def model_dump(self, mode="json"):
        return {"ritm_id": self.ritm_id, "outcome": "in_progress",
                "tasks_total": 3}


def _create_conn():
    """fetchrow → form schema (load_request_fields); fetchval → catalog name."""
    return _FakeConn(
        fetchrow={"name": "Standard Laptop", "request_fields": _FORM},
        fetchval="Standard Laptop",
    )


async def test_create_blocks_on_missing_required_field(monkeypatch):
    tools.set_connection_provider(_provider(_create_conn()))
    # fields omit the required "model"
    out = await tools.create_service_request(
        {"catalog_id": "CAT_HW_LAPTOP_STD", "fields": {"notes": "hi"}}, _CTX)
    assert out["ok"] is False
    assert out["error_code"] == "UC08_MISSING_FIELDS"
    assert out["missing_fields"] == ["model"]


async def test_create_opens_sr_fulfils_and_dispatches(monkeypatch):
    tools.set_connection_provider(_provider(_create_conn()))

    async def _fake_fulfil(req, **kw):
        # the SR must have been opened first → request_id present
        assert req.request_id.startswith("REQ")
        assert req.variables == {"model": "Dell XPS"}
        return _StubOutcome()

    dispatched = {}

    async def _fake_dispatch(*, nats, tenant_id, ritm_id, trace_id=None):
        dispatched["ritm_id"] = ritm_id

    monkeypatch.setattr(tools._core, "fulfill_request", _fake_fulfil)
    monkeypatch.setattr(tools._nats_dispatcher, "dispatch_execute",
                        _fake_dispatch)
    tools.set_nats_client(object())          # non-None → dispatch attempted

    out = await tools.create_service_request(
        {"catalog_id": "CAT_HW_LAPTOP_STD", "fields": {"model": "Dell XPS"}},
        _CTX)

    assert out["ok"] is True
    assert out["request_id"].startswith("REQ")
    assert out["ritm_id"] == "RITM0000000001"
    assert out["dispatched"] is True
    assert dispatched["ritm_id"] == "RITM0000000001"


async def test_create_reports_undispatched_when_nats_absent(monkeypatch):
    tools.set_connection_provider(_provider(_create_conn()))

    async def _fake_fulfil(req, **kw):
        return _StubOutcome()

    monkeypatch.setattr(tools._core, "fulfill_request", _fake_fulfil)
    tools.set_nats_client(None)              # no NATS

    out = await tools.create_service_request(
        {"catalog_id": "CAT_HW_LAPTOP_STD", "fields": {"model": "Dell XPS"}},
        _CTX)
    assert out["ok"] is True
    assert out["dispatched"] is False        # honest, not silent


async def test_create_requires_catalog_id():
    out = await tools.create_service_request({"fields": {"model": "x"}}, _CTX)
    assert out["ok"] is False
    assert out["error_code"] == "UC08_BAD_REQUEST"


# ── update_service_request ──────────────────────────────────────────────────


async def test_update_merges_fields():
    conn = _FakeConn(fetchrow={
        "request_id": "REQ123", "catalog_item_id": "CAT_HW_LAPTOP_STD",
        "status": "requested", "fields": {"model": "Dell XPS", "notes": "x"}})
    tools.set_connection_provider(_provider(conn))
    out = await tools.update_service_request(
        {"request_id": "REQ123", "fields": {"notes": "urgent"}}, _CTX)
    assert out["ok"] is True
    assert "REQ123" in out["display_text"]


async def test_update_unknown_request_is_typed_error():
    conn = _FakeConn(fetchrow=None)          # UPDATE ... RETURNING → no row
    tools.set_connection_provider(_provider(conn))
    out = await tools.update_service_request(
        {"request_id": "REQ_NOPE", "fields": {"notes": "x"}}, _CTX)
    assert out["ok"] is False
    assert "REQ_NOPE" in out["display_text"]


async def test_update_requires_request_id_and_fields():
    out1 = await tools.update_service_request({"fields": {"a": 1}}, _CTX)
    assert out1["ok"] is False and out1["error_code"] == "UC08_BAD_REQUEST"
    out2 = await tools.update_service_request({"request_id": "REQ1"}, _CTX)
    assert out2["ok"] is False and out2["error_code"] == "UC08_BAD_REQUEST"


# ── boundary: tenant comes from context, never arguments ────────────────────


async def test_tenant_bound_from_context_not_arguments():
    captured = {}

    def _fetchrow(query, *args):
        captured["tenant_id"] = args[0]      # first predicate is tenant
        return {"name": "X", "request_fields": _FORM}

    tools.set_connection_provider(_provider(_FakeConn(fetchrow=_fetchrow)))
    # An attacker-supplied tenant_id in arguments must be ignored.
    await tools.get_service_request_fields(
        {"catalog_id": "CAT_X", "tenant_id": "T999_EVIL"}, _CTX)
    assert captured["tenant_id"] == "T001"   # from _CTX, not arguments
