"""UC-3 tool handlers — search_kb / get_kb_article / search_kb_by_ticket.

Verifies spec conformance: structured output (C8), audience + tenant scoping
(C13/C14), and no silent failure (C17).
"""
from __future__ import annotations

import pytest

from oneops.use_cases._shared.kb_store import InMemoryKbStore, set_kb_store
from oneops.use_cases.uc03_kb_lookup.handlers import (
    get_kb_article,
    search_kb,
    search_kb_by_ticket,
)


@pytest.fixture
def store() -> InMemoryKbStore:
    # Reset the module-global KB singletons so these tests are ORDER-INDEPENDENT:
    # another test elsewhere may have left a real embed_fn / relevance_scorer
    # installed, which would change retrieval behaviour here (the cause of
    # full-suite-only uc03 flakes). Start every test from a clean, deterministic
    # in-memory state.
    from oneops.use_cases.uc03_kb_lookup.kb_embed import (
        set_kb_embed_fn,
        set_kb_relevance_scorer,
    )
    set_kb_embed_fn(None)
    set_kb_relevance_scorer(None)
    s = InMemoryKbStore()
    s.seed(kb_id="KB0001", tenant_id="T1", title="Fix VPN disconnects",
           summary="Resolve VPN tunnel drops", content="update the vpn client",
           tags=["vpn"], state="published", audience="all", helpful_votes=100,
           related_incidents=["INC0001"], related_ci_ids=["CI0009"])
    s.seed(kb_id="KB0002", tenant_id="T1", title="Email password reset",
           summary="Reset your email password", content="open the portal",
           tags=["email"], state="published", audience="all", helpful_votes=5)
    s.seed(kb_id="KB0003", tenant_id="T1", title="VPN internals runbook",
           summary="vpn deep dive", content="vpn internals",
           tags=["vpn"], state="published", audience="technician",
           helpful_votes=50)
    s.seed(kb_id="KB0004", tenant_id="T1", title="Draft VPN note",
           summary="vpn", content="vpn", tags=["vpn"],
           state="draft", audience="all", helpful_votes=1)
    s.seed(kb_id="KB9001", tenant_id="T2", title="Other tenant VPN",
           summary="vpn", content="vpn", tags=["vpn"],
           state="published", audience="all", helpful_votes=1)
    set_kb_store(s)
    return s


# ── search_kb ─────────────────────────────────────────────────────────────


async def test_search_kb_returns_ranked_previews(store):
    out = await search_kb({"query": "vpn disconnects"},
                          {"tenant_id": "T1", "role": "employee"})
    assert out["outcome"] == "found"
    assert out["articles"][0]["kb_id"] == "KB0001"
    assert out["articles"][0]["relevance_score"] == 2


async def test_search_kb_preview_carries_content_for_composer(store):
    out = await search_kb({"query": "vpn"},
                          {"tenant_id": "T1", "role": "employee"})
    assert all("content" in a for a in out["articles"])


async def test_search_kb_end_user_does_not_see_technician_article(store):
    out = await search_kb({"query": "vpn"},
                          {"tenant_id": "T1", "role": "employee"})
    assert "KB0003" not in {a["kb_id"] for a in out["articles"]}


async def test_search_kb_technician_sees_technician_article(store):
    out = await search_kb({"query": "vpn"},
                          {"tenant_id": "T1", "role": "network_engineer"})
    assert "KB0003" in {a["kb_id"] for a in out["articles"]}


async def test_search_kb_no_match_is_explicit(store):
    out = await search_kb({"query": "kubernetes helm chart"},
                          {"tenant_id": "T1", "role": "employee"})
    assert out["outcome"] == "no_match"
    assert out["articles"] == []


async def test_search_kb_missing_query_is_invalid_request(store):
    out = await search_kb({}, {"tenant_id": "T1", "role": "employee"})
    assert out["outcome"] == "invalid_request"


async def test_search_kb_missing_tenant_is_invalid_request(store):
    out = await search_kb({"query": "vpn"}, {"role": "employee"})
    assert out["outcome"] == "invalid_request"


async def test_search_kb_is_tenant_scoped(store):
    out = await search_kb({"query": "vpn"},
                          {"tenant_id": "T1", "role": "employee"})
    assert "KB9001" not in {a["kb_id"] for a in out["articles"]}


# ── get_kb_article ────────────────────────────────────────────────────────


async def test_get_kb_article_returns_full_article(store):
    out = await get_kb_article({"article_id": "KB0001"},
                               {"tenant_id": "T1", "role": "employee"})
    assert out["outcome"] == "found"
    assert out["article"]["content"] == "update the vpn client"


async def test_get_kb_article_strips_tenant_id(store):
    out = await get_kb_article({"article_id": "KB0001"},
                               {"tenant_id": "T1", "role": "employee"})
    assert "tenant_id" not in out["article"]      # restricted — field policy


async def test_get_kb_article_draft_is_not_found(store):
    out = await get_kb_article({"article_id": "KB0004"},
                               {"tenant_id": "T1", "role": "employee"})
    assert out["outcome"] == "not_found"


async def test_get_kb_article_out_of_audience_is_denied(store):
    out = await get_kb_article({"article_id": "KB0003"},
                               {"tenant_id": "T1", "role": "employee"})
    # The store now distinguishes "denied" (article exists but caller lacks
    # the audience tag) from "not_found" (article truly absent). Both keep
    # the article identifier opaque to the caller — no leakage.
    assert out["outcome"] == "denied"


async def test_get_kb_article_other_tenant_is_not_found(store):
    out = await get_kb_article({"article_id": "KB9001"},
                               {"tenant_id": "T1", "role": "employee"})
    assert out["outcome"] == "not_found"


async def test_get_kb_article_missing_id_is_invalid_request(store):
    out = await get_kb_article({}, {"tenant_id": "T1", "role": "employee"})
    assert out["outcome"] == "invalid_request"


# ── search_kb_by_ticket ───────────────────────────────────────────────────


async def test_search_by_ticket_finds_linked_by_incident(store):
    out = await search_kb_by_ticket(
        {"ticket_id": "INC0001", "service_id": "incident"},
        {"tenant_id": "T1", "role": "employee"})
    assert out["outcome"] == "found"
    assert [a["kb_id"] for a in out["articles"]] == ["KB0001"]


async def test_search_by_ticket_finds_linked_by_ci(store):
    out = await search_kb_by_ticket(
        {"ticket_id": "CI0009", "service_id": "cmdb_ci"},
        {"tenant_id": "T1", "role": "employee"})
    assert [a["kb_id"] for a in out["articles"]] == ["KB0001"]


async def test_search_by_ticket_no_link_is_no_match(store):
    out = await search_kb_by_ticket(
        {"ticket_id": "INC9999", "service_id": "incident"},
        {"tenant_id": "T1", "role": "employee"})
    assert out["outcome"] == "no_match"


async def test_search_by_ticket_no_link_searches_by_ticket_symptoms(store, monkeypatch):
    """Production KB model (no incident→KB reference): when no KB is *linked* to
    the ticket, match by MEANING on the ticket's SYMPTOMS — not the user's vague
    phrasing. INC9999 has no linked KB; feeding its symptoms ('vpn disconnects')
    surfaces KB0001 via the hybrid search, whereas 'find KB for the root cause'
    would dead-end at no_match."""
    import oneops.use_cases.uc03_kb_lookup.handlers as H

    async def fake_symptoms(ticket_id: str, tenant_id: str) -> str:
        return "vpn disconnects"

    monkeypatch.setattr(H, "_ticket_symptom_text", fake_symptoms)
    out = await search_kb_by_ticket(
        {"ticket_id": "INC9999", "service_id": "incident",
         "user_message": "find KB for the root cause"},
        {"tenant_id": "T1", "role": "employee"})
    assert out["outcome"] == "found"                       # symptom search found it
    assert "KB0001" in [a["kb_id"] for a in out["articles"]]


async def test_search_by_ticket_missing_id_is_invalid_request(store):
    out = await search_kb_by_ticket(
        {"service_id": "incident"}, {"tenant_id": "T1", "role": "employee"})
    assert out["outcome"] == "invalid_request"
