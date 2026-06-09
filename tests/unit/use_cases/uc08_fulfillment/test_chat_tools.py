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

from oneops.executor import nodes as _executor_nodes
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
    """Snapshot + restore the module globals each test touches, and clear the
    conductor's per-flow memo so module state never leaks across tests."""
    saved = (tools._gateway, tools._nats_client, tools._connection_provider)
    for m in (tools._search_memo, tools._fields_memo, tools._draft_memo):
        m.clear()
    yield
    tools._gateway, tools._nats_client, tools._connection_provider = saved
    for m in (tools._search_memo, tools._fields_memo, tools._draft_memo):
        m.clear()


# ── get_service_request_list ────────────────────────────────────────────────


async def test_list_shows_topk_when_best_is_relevant():
    # When the BEST match clears the floor, show the whole top-K so the USER
    # has a few candidates to choose from (they are the relevance judge).
    rows = [
        {"catalog_item_id": "CAT_AC_VPN", "name": "VPN / Remote Access",
         "description": "Request VPN remote access", "category": "access",
         "owner_group": "network", "cosine_score": 0.71},
        {"catalog_item_id": "CAT_HW_LAPTOP_STD", "name": "Standard Laptop",
         "description": "A standard business laptop", "category": "hardware",
         "owner_group": "hardware", "cosine_score": 0.45},  # below floor, still shown
    ]
    tools.set_gateway(_FakeGateway())
    tools.set_connection_provider(_provider(_FakeConn(fetch=rows)))

    out = await tools.get_service_request_list(
        {"service_catalogs": ["VPN", "remote access"]}, _CTX)

    assert out["ok"] is True
    ids = [m["catalog_id"] for m in out["matches"]]
    assert ids == ["CAT_AC_VPN", "CAT_HW_LAPTOP_STD"]   # top-K, user chooses
    assert out["count"] == 2


async def test_list_no_match_when_best_below_floor():
    # The best match is below the floor → genuine no-match (not a noisy list).
    rows = [
        {"catalog_item_id": "CAT_X", "name": "Something", "description": "x",
         "category": "c", "owner_group": "g", "cosine_score": 0.30},
    ]
    tools.set_gateway(_FakeGateway())
    tools.set_connection_provider(_provider(_FakeConn(fetch=rows)))
    out = await tools.get_service_request_list({"query": "pizza party"}, _CTX)
    assert out["ok"] is True
    assert out["matches"] == []
    assert "incident" in out["display_text"].lower()


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


# ════════════════════════════════════════════════════════════════════════════
#  request_catalog_item — the conductor (runbook Playbook 3 sequence).
#  The 4 tool handlers + the 3 interrupt helpers are monkeypatched so the
#  conductor's SEQUENCING + fallback + cancel paths are tested deterministically.
# ════════════════════════════════════════════════════════════════════════════


def _stub_flow(monkeypatch, *, matches, fields, selection, inputs, confirmed,
               draft=None):
    """Wire the conductor's collaborators. Returns a dict capturing calls."""
    calls: dict[str, Any] = {"created": None, "interrupts": []}

    async def _list(args, ctx):
        return {"ok": True, "matches": matches, "count": len(matches)}

    async def _fields(args, ctx):
        return {"ok": True, "catalog_id": args["catalog_id"], "fields": fields,
                "required": [f["field_name"] for f in fields
                             if f.get("required")]}

    async def _create(args, ctx):
        calls["created"] = args
        return {"ok": True, "request_id": "REQ0000000001",
                "ritm_id": "RITM1", "dispatched": True,
                "display_text": "Done — service request REQ0000000001 submitted."}

    async def _draft(*, query, schema, tenant_id, user_id, **_kw):
        return dict(draft or {})

    monkeypatch.setattr(tools, "get_service_request_list", _list)
    monkeypatch.setattr(tools, "get_service_request_fields", _fields)
    monkeypatch.setattr(tools, "create_service_request", _create)
    monkeypatch.setattr(tools, "_draft_field_values", _draft)

    def _sel(prompt, options, **kw):
        calls["interrupts"].append(("selection", options))
        return selection

    # `inputs` may be a single answer (returned every round) or a list of
    # answers consumed one per form round (for the re-prompt loop).
    _seq = list(inputs) if isinstance(inputs, list) else None

    def _inp(prompt, flds):
        calls["interrupts"].append(("input", flds))
        if _seq is not None:
            return _seq.pop(0) if _seq else {"fields": {}}
        return inputs

    def _conf(summary, action):
        calls["interrupts"].append(("confirmation", summary))
        return confirmed

    monkeypatch.setattr(_executor_nodes, "interrupt_for_selection", _sel)
    monkeypatch.setattr(_executor_nodes, "interrupt_for_input", _inp)
    monkeypatch.setattr(_executor_nodes, "interrupt_for_confirmation", _conf)
    return calls


_MATCHES = [
    {"catalog_id": "CAT_HW_LAPTOP_STD", "name": "Standard Business Laptop",
     "description": "A standard laptop", "category": "hardware"},
    {"catalog_id": "CAT_HW_LAPTOP_DEV", "name": "Dev Laptop",
     "description": "High-perf laptop", "category": "hardware"},
]
_FIELDS2 = [
    {"field_name": "model", "label": "Model", "type": "text", "required": True},
]


async def test_conductor_happy_path_runs_full_sequence(monkeypatch):
    calls = _stub_flow(
        monkeypatch, matches=_MATCHES, fields=_FIELDS2,
        selection={"selected": {"id": "CAT_HW_LAPTOP_DEV", "label": "Dev Laptop"}},
        inputs={"fields": {"model": "XPS 15"}},
        confirmed={"confirmed": True})
    out = await tools.request_catalog_item({"query": "I need a laptop"}, _CTX)
    # full runbook order: selection → input → confirmation → create
    kinds = [k for k, _ in calls["interrupts"]]
    assert kinds == ["selection", "input", "confirmation"]
    assert calls["created"] == {"catalog_id": "CAT_HW_LAPTOP_DEV",
                                "fields": {"model": "XPS 15"}}
    assert out["ok"] is True and out["request_id"] == "REQ0000000001"


async def test_conductor_ai_drafts_and_prefills_single_editable_form(monkeypatch):
    # runbook "draft → present → approve": the AI pre-fills what it can from the
    # request; the human gets ONE editable form (not field-by-field) pre-filled
    # with the draft, edits, then approves.
    fields3 = [
        {"field_name": "employee_name", "label": "Name", "type": "text",
         "required": True},
        {"field_name": "start_date", "label": "Start", "type": "date",
         "required": True},
        {"field_name": "role", "label": "Role", "type": "text",
         "required": False},
    ]
    calls = _stub_flow(
        monkeypatch, matches=_MATCHES, fields=fields3,
        selection={"selected": {"id": "CAT_HR_ONBOARD", "label": "Onboarding"}},
        # the human's final (edited) values come back from the form:
        inputs={"fields": {"employee_name": "Jane Doe",
                           "start_date": "2026-07-01", "role": "Engineer"}},
        confirmed={"confirmed": True},
        # the AI inferred two of the three from the query:
        draft={"employee_name": "Jane Doe", "start_date": "2026-07-01"})
    out = await tools.request_catalog_item(
        {"query": "onboard Jane Doe starting 2026-07-01"}, _CTX)
    kinds = [k for k, _ in calls["interrupts"]]
    # ONE form for all fields (not one interrupt per field), then confirm.
    assert kinds == ["selection", "input", "confirmation"]
    form = [flds for k, flds in calls["interrupts"] if k == "input"][0]
    assert len(form) == 3
    by_name = {f["name"]: f for f in form}
    assert by_name["employee_name"]["value"] == "Jane Doe"   # AI-drafted
    assert by_name["start_date"]["value"] == "2026-07-01"     # AI-drafted
    assert by_name["role"]["value"] == ""                     # not inferable → blank
    assert out["ok"] is True
    assert calls["created"]["fields"]["role"] == "Engineer"   # human-supplied


async def test_conductor_reprompts_form_until_required_filled(monkeypatch):
    # If a required field comes back blank, the conductor RE-SHOWS the form
    # (loop-back) instead of dead-ending at create. Here the first submission
    # omits the required manager_email; the second supplies it.
    fields = [
        {"field_name": "employee_name", "label": "Name", "type": "text",
         "required": True},
        {"field_name": "manager_email", "label": "Manager email",
         "type": "email", "required": True},
    ]
    calls = _stub_flow(
        monkeypatch, matches=_MATCHES, fields=fields,
        selection={"selected": {"id": "CAT_HR_ONBOARD", "label": "Onboarding"}},
        inputs=[
            {"fields": {"employee_name": "Jane", "manager_email": ""}},   # incomplete
            {"fields": {"employee_name": "Jane",
                        "manager_email": "m@x.com"}},                      # completed
        ],
        confirmed={"confirmed": True},
        draft={})
    out = await tools.request_catalog_item({"query": "onboard Jane"}, _CTX)
    kinds = [k for k, _ in calls["interrupts"]]
    # the form is shown TWICE (re-prompt), then confirm, then create succeeds.
    assert kinds == ["selection", "input", "input", "confirmation"]
    assert calls["created"]["fields"]["manager_email"] == "m@x.com"
    assert out["ok"] is True


async def test_conductor_no_match_declines_without_interrupting(monkeypatch):
    calls = _stub_flow(
        monkeypatch, matches=[], fields=[], selection=None, inputs=None,
        confirmed=None)
    out = await tools.request_catalog_item({"query": "pizza party"}, _CTX)
    assert out["outcome"] == "no_match"
    assert calls["interrupts"] == []          # never paused
    assert calls["created"] is None           # never created
    assert "incident isn't available" in out["display_text"].lower() or \
           "follow up" in out["display_text"].lower()


async def test_conductor_user_declines_selection(monkeypatch):
    calls = _stub_flow(
        monkeypatch, matches=_MATCHES, fields=_FIELDS2,
        selection={"selected": None},          # allow_none → declined
        inputs=None, confirmed=None)
    out = await tools.request_catalog_item({"query": "laptop"}, _CTX)
    assert out["outcome"] == "cancelled"
    assert calls["created"] is None
    # only the selection interrupt fired
    assert [k for k, _ in calls["interrupts"]] == ["selection"]


async def test_conductor_user_declines_confirmation_does_not_create(monkeypatch):
    calls = _stub_flow(
        monkeypatch, matches=_MATCHES, fields=_FIELDS2,
        selection={"selected": {"id": "CAT_HW_LAPTOP_STD", "label": "Std Laptop"}},
        inputs={"fields": {"model": "T14"}},
        confirmed={"confirmed": False})        # user cancels at review
    out = await tools.request_catalog_item({"query": "laptop"}, _CTX)
    assert out["outcome"] == "cancelled"
    assert calls["created"] is None            # confirmation gate held
    assert [k for k, _ in calls["interrupts"]] == \
           ["selection", "input", "confirmation"]


async def test_conductor_skips_input_when_item_has_no_form(monkeypatch):
    calls = _stub_flow(
        monkeypatch, matches=_MATCHES, fields=[],   # no form fields
        selection={"selected": {"id": "CAT_HW_LAPTOP_STD", "label": "Std Laptop"}},
        inputs=None, confirmed={"confirmed": True})
    out = await tools.request_catalog_item({"query": "laptop"}, _CTX)
    # input step skipped; selection → confirmation → create
    assert [k for k, _ in calls["interrupts"]] == ["selection", "confirmation"]
    assert calls["created"] == {"catalog_id": "CAT_HW_LAPTOP_STD", "fields": {}}
    assert out["ok"] is True
