"""UC-1 deterministic + LLM tool handlers — Component Spec conformance.

Covers `get_ticket_links`, `get_ticket_timeline`,
`get_ticket_attachment_metadata`, and `summarize_entity`. Verifies structured
output (C8), tenant isolation (C13), role-gated content (C12), and
no-silent-failure (C17 — every invalid/missing path is an explicit outcome).
"""
from __future__ import annotations

from typing import Any

import pytest

from oneops.use_cases._shared.ticket_store import (
    InMemoryTicketStore,
    set_ticket_store,
)
from oneops.use_cases.uc01_summarization.tools import (
    _record_bindable_fields,
    get_ticket_attachment_metadata,
    get_ticket_links,
    get_ticket_timeline,
    set_summarize_llm,
    summarize_entity,
)

_SUB_KEYS = {"outcome", "ticket_id", "service_id", "kind", "message", "items"}
_SUMMARY_KEYS = {"outcome", "ticket_id", "service_id", "message", "summary",
                 "bindable"}


@pytest.fixture
def store() -> InMemoryTicketStore:
    s = InMemoryTicketStore()
    s.seed(
        ticket_id="INC0048213", service_id="incident", tenant_id="tenant-a",
        title="VPN drops every few minutes",
        status="in_progress", priority="P2",
        links=[
            {"link_id": "L1", "type": "related_incident",
             "target_id": "INC0048210"},
            {"link_id": "L2", "type": "parent_problem",
             "target_id": "PRB0001005"},
        ],
        timeline=[
            {"entry_id": "T1", "is_public": True,
             "kind": "comment", "text": "user opened the case"},
            {"entry_id": "T2", "is_public": False,
             "kind": "work_note", "text": "internal triage"},
        ],
        attachments=[
            {"attachment_id": "A1", "name": "trace.pcap",
             "size_bytes": 14_812, "content_type": "application/octet-stream"},
        ],
    )
    set_ticket_store(s)
    return s


# ── C8 — structured output, shared shape across the three deterministic tools


@pytest.mark.parametrize(("handler", "kind"), [
    (get_ticket_links, "links"),
    (get_ticket_timeline, "timeline"),
    (get_ticket_attachment_metadata, "attachments"),
])
async def test_output_has_the_declared_shape(store, handler, kind):
    out = await handler(
        {"ticket_id": "INC0048213", "service_id": "incident"},
        {"tenant_id": "tenant-a"})
    assert set(out) == _SUB_KEYS
    assert out["kind"] == kind
    assert out["outcome"] == "found"
    assert isinstance(out["items"], list)
    assert out["message"]


# ── deterministic results — content is read from the record, not invented ─


async def test_links_returns_the_seeded_links(store):
    out = await get_ticket_links(
        {"ticket_id": "INC0048213", "service_id": "incident"},
        {"tenant_id": "tenant-a"})
    target_ids = {item.get("target_id") for item in out["items"]}
    assert target_ids == {"INC0048210", "PRB0001005"}


async def test_attachments_returns_metadata_only(store):
    out = await get_ticket_attachment_metadata(
        {"ticket_id": "INC0048213", "service_id": "incident"},
        {"tenant_id": "tenant-a"})
    assert len(out["items"]) == 1
    item = out["items"][0]
    assert item["name"] == "trace.pcap"
    assert item["size_bytes"] == 14_812


# ── C12 — role-gated content on the timeline ─────────────────────────────


async def test_timeline_employee_sees_only_public_entries(store):
    out = await get_ticket_timeline(
        {"ticket_id": "INC0048213", "service_id": "incident"},
        {"tenant_id": "tenant-a", "role": "employee"})
    kinds = {item.get("kind") for item in out["items"]}
    assert kinds == {"comment"}                   # internal work_note hidden


async def test_timeline_agent_sees_all_entries(store):
    out = await get_ticket_timeline(
        {"ticket_id": "INC0048213", "service_id": "incident"},
        {"tenant_id": "tenant-a", "role": "service_desk_agent"})
    assert len(out["items"]) == 2


# ── C13 — tenant isolation ───────────────────────────────────────────────


@pytest.mark.parametrize("handler", [
    get_ticket_links, get_ticket_timeline, get_ticket_attachment_metadata,
])
async def test_wrong_tenant_cannot_read_the_sub_collection(store, handler):
    out = await handler(
        {"ticket_id": "INC0048213", "service_id": "incident"},
        {"tenant_id": "tenant-b"})
    assert out["outcome"] == "not_found"
    assert out["items"] is None


# ── empty sub-collections — found with zero items, not "not_found" ───────


async def test_a_record_with_no_links_is_found_with_empty_items(store):
    store.seed(
        ticket_id="INC0099999", service_id="incident", tenant_id="tenant-a",
        title="case with no links")
    out = await get_ticket_links(
        {"ticket_id": "INC0099999", "service_id": "incident"},
        {"tenant_id": "tenant-a"})
    assert out["outcome"] == "found"
    assert out["items"] == []


# ── C17 — invalid inputs are explicit outcomes ───────────────────────────


@pytest.mark.parametrize("handler", [
    get_ticket_links, get_ticket_timeline, get_ticket_attachment_metadata,
])
async def test_missing_ticket_id_is_invalid_request(store, handler):
    out = await handler({"service_id": "incident"}, {"tenant_id": "tenant-a"})
    assert out["outcome"] == "invalid_request"
    assert out["items"] is None


@pytest.mark.parametrize("handler", [
    get_ticket_links, get_ticket_timeline, get_ticket_attachment_metadata,
])
async def test_missing_service_id_is_invalid_request(store, handler):
    out = await handler({"ticket_id": "INC0048213"}, {"tenant_id": "tenant-a"})
    assert out["outcome"] == "invalid_request"


@pytest.mark.parametrize("handler", [
    get_ticket_links, get_ticket_timeline, get_ticket_attachment_metadata,
])
async def test_missing_tenant_is_invalid_request(store, handler):
    out = await handler(
        {"ticket_id": "INC0048213", "service_id": "incident"}, {})
    assert out["outcome"] == "invalid_request"


async def test_unknown_ticket_is_not_found_not_a_raise(store):
    out = await get_ticket_links(
        {"ticket_id": "INC9999999", "service_id": "incident"},
        {"tenant_id": "tenant-a"})
    assert out["outcome"] == "not_found"


# ── summarize_entity — C8 / C11 / C13 / C17 ──────────────────────────────


async def test_summarize_returns_structured_shape_when_llm_wired(store):
    async def fake_llm(record: dict[str, Any], tenant_id: str, model: str,
                        *, user_id: str = "") -> dict[str, Any]:
        assert tenant_id == "tenant-a"
        return {
            "summary": f"Status {record.get('status')}",
            "key_points": ["VPN drops"],
            "model": model or "deterministic",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    set_summarize_llm(fake_llm)
    try:
        out = await summarize_entity(
            {"ticket_id": "INC0048213", "service_id": "incident",
             "model": "test-model"},
            {"tenant_id": "tenant-a"})
        assert set(out) == _SUMMARY_KEYS
        assert out["outcome"] == "summarized"
        assert out["summary"]["summary"].startswith("Status")
        assert out["summary"]["model"] == "test-model"
    finally:
        set_summarize_llm(None)


async def test_summarize_without_llm_wired_is_explicit_outcome(store):
    set_summarize_llm(None)
    out = await summarize_entity(
        {"ticket_id": "INC0048213", "service_id": "incident"},
        {"tenant_id": "tenant-a"})
    assert out["outcome"] == "llm_unavailable"
    assert out["summary"] is None
    assert out["message"]


async def test_summarize_redacts_before_calling_llm(store):
    captured: dict[str, Any] = {}

    async def fake_llm(record: dict[str, Any], tenant_id: str, model: str,
                        *, user_id: str = "") -> dict[str, Any]:
        captured["record"] = record
        return {"summary": "ok", "key_points": [], "model": model, "usage": {}}

    set_summarize_llm(fake_llm)
    try:
        await summarize_entity(
            {"ticket_id": "INC0048213", "service_id": "incident"},
            {"tenant_id": "tenant-a", "role": "employee"})
        # `tenant_id` is classified 'restricted' in field_policy — it must
        # NEVER reach the LLM record.
        assert "tenant_id" not in captured["record"]
    finally:
        set_summarize_llm(None)


async def test_summarize_propagates_cache_hit_to_handler_output(store):
    """When the injected SummarizeFn carries a `_cache.hit=True` signal
    (E3 cache-aside), the handler surfaces `cache_hit=True` + `cache_age_s`
    at the top level so the executor / frontend can count hits."""
    async def cache_hit_fn(record, tenant_id, model, *, user_id=""):
        return {
            "summary": "From the cache.",
            "key_details": {"Status": "open"},
            "model": "cached",
            "usage": {},
            "_cache": {"hit": True, "age_s": 12, "fingerprint": "abc"},
        }
    set_summarize_llm(cache_hit_fn)
    try:
        out = await summarize_entity(
            {"ticket_id": "INC0048213", "service_id": "incident"},
            {"tenant_id": "tenant-a"})
        assert out["outcome"] == "summarized"
        assert out["cache_hit"] is True
        assert out["cache_age_s"] == 12
        # The user-facing message reflects the cache provenance.
        assert "from cache" in out["message"].lower()
        # The synthetic `_cache` marker is consumed (popped), not leaked.
        assert "_cache" not in (out.get("summary") or {})
    finally:
        set_summarize_llm(None)


async def test_summarize_marks_cache_miss_when_signal_says_so(store):
    async def cache_miss_fn(record, tenant_id, model, *, user_id=""):
        return {
            "summary": "Freshly summarised.",
            "key_details": {"Status": "open"},
            "model": "gpt-4o-mini",
            "usage": {"prompt_tokens": 100, "completion_tokens": 60},
            "_cache": {"hit": False, "age_s": None, "fingerprint": "abc"},
        }
    set_summarize_llm(cache_miss_fn)
    try:
        out = await summarize_entity(
            {"ticket_id": "INC0048213", "service_id": "incident"},
            {"tenant_id": "tenant-a"})
        assert out["outcome"] == "summarized"
        assert out["cache_hit"] is False
        # The "(from cache)" suffix is NOT present on miss.
        assert "from cache" not in out["message"].lower()
    finally:
        set_summarize_llm(None)


async def test_summarize_unknown_ticket_is_not_found(store):
    out = await summarize_entity(
        {"ticket_id": "INC9999999", "service_id": "incident"},
        {"tenant_id": "tenant-a"})
    assert out["outcome"] == "not_found"


@pytest.mark.parametrize(("args", "ctx"), [
    ({"service_id": "incident"}, {"tenant_id": "tenant-a"}),
    ({"ticket_id": "INC0048213"}, {"tenant_id": "tenant-a"}),
    ({"ticket_id": "INC0048213", "service_id": "incident"}, {}),
])
async def test_summarize_invalid_inputs(store, args, ctx):
    out = await summarize_entity(args, ctx)
    assert out["outcome"] == "invalid_request"
    assert out["summary"] is None


def test_bindable_excludes_search_embedding_noise_keeps_business_fields():
    """The bindable surface keeps chainable business fields (incl. title /
    description) but drops the search/embedding substrate columns so the
    embedding blob never bloats the response."""
    record = {
        # business fields a downstream step may bind on — must survive
        "incident_id": "INC0001001", "status": "open", "priority": "P2",
        "title": "VPN disconnects", "description": "drops on handoff",
        "related_change": "CHG0004001",
        # substrate noise — must be excluded
        "search_tsv": "'vpn':1A 'wi-fi':4A",
        "embedding": "[" + ",".join(["0.0"] * 3072) + "]",
        "embedding_model": "text-embedding-3-large", "embedding_version": "1.0",
        "content_hash": "abc123", "embedded_at": "2026-04-01T09:10:00Z",
        "_updated_at": "2026-04-01T15:00:00Z",
    }
    out = _record_bindable_fields(record)

    # business fields kept (chaining unaffected)
    for k in ("incident_id", "status", "priority", "title", "description",
              "related_change"):
        assert k in out, f"{k} should remain bindable"
    # noise dropped
    for k in ("search_tsv", "embedding", "embedding_model", "embedding_version",
              "content_hash", "embedded_at", "_updated_at"):
        assert k not in out, f"{k} must not leak into bindable"
