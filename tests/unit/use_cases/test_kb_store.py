"""KbStore — UC-3 knowledge-base data layer (in-memory backend)."""
from __future__ import annotations

import pytest

from oneops.use_cases._shared.kb_store import InMemoryKbStore, PostgresKbStore

_ALL = ("all", "end_user", "technician")


@pytest.fixture
def store() -> InMemoryKbStore:
    s = InMemoryKbStore()
    s.seed(kb_id="KB0001", tenant_id="T1", title="Fix VPN disconnects",
           summary="Resolve VPN tunnel drops", content="update the vpn client",
           tags=["vpn", "network"], state="published", audience="all",
           helpful_votes=100, related_incidents=["INC0001"], related_ci_ids=[])
    s.seed(kb_id="KB0002", tenant_id="T1", title="VPN client install guide",
           summary="How to install the VPN client", content="download and run",
           tags=["vpn"], state="published", audience="all",
           helpful_votes=10, related_incidents=[], related_ci_ids=["CI0009"])
    s.seed(kb_id="KB0003", tenant_id="T1", title="Reset email password",
           summary="Password reset steps", content="open the portal",
           tags=["email"], state="published", audience="all", helpful_votes=5)
    s.seed(kb_id="KB0004", tenant_id="T1", title="Draft VPN article",
           summary="vpn vpn vpn", content="vpn", tags=["vpn"],
           state="draft", audience="all", helpful_votes=999)
    s.seed(kb_id="KB0005", tenant_id="T1", title="Internal VPN runbook",
           summary="vpn internal", content="vpn", tags=["vpn"],
           state="published", audience="technician", helpful_votes=50)
    s.seed(kb_id="KB9001", tenant_id="T2", title="Other tenant VPN doc",
           summary="vpn", content="vpn", tags=["vpn"],
           state="published", audience="all", helpful_votes=1)
    return s


# ── search — keyword overlap ranking ──────────────────────────────────────


async def test_search_ranks_by_query_term_overlap(store):
    hits = await store.search(query="vpn disconnects", tenant_id="T1",
                              audiences=_ALL)
    # KB0001 matches both terms; KB0002 matches "vpn" only → KB0001 ranks first.
    assert hits[0]["kb_id"] == "KB0001"
    assert hits[0]["relevance_score"] == 2
    assert {h["kb_id"] for h in hits} >= {"KB0001", "KB0002"}


async def test_search_excludes_zero_overlap_articles(store):
    hits = await store.search(query="vpn", tenant_id="T1", audiences=_ALL)
    assert "KB0003" not in {h["kb_id"] for h in hits}   # email article — no "vpn"


async def test_search_excludes_unpublished(store):
    hits = await store.search(query="vpn", tenant_id="T1", audiences=_ALL)
    assert "KB0004" not in {h["kb_id"] for h in hits}   # draft — never returned


async def test_search_respects_audience(store):
    # An end_user audience must not see a technician-only article.
    hits = await store.search(query="vpn", tenant_id="T1",
                              audiences=("all", "end_user"))
    assert "KB0005" not in {h["kb_id"] for h in hits}


async def test_search_is_tenant_scoped(store):
    hits = await store.search(query="vpn", tenant_id="T1", audiences=_ALL)
    assert "KB9001" not in {h["kb_id"] for h in hits}   # T2 article


async def test_search_empty_query_returns_nothing(store):
    assert await store.search(query="   ", tenant_id="T1", audiences=_ALL) == []


async def test_search_honours_limit(store):
    hits = await store.search(query="vpn", tenant_id="T1", audiences=_ALL, limit=1)
    assert len(hits) == 1


# ── get — one article by id ───────────────────────────────────────────────


async def test_get_returns_a_published_article(store):
    art = await store.get(kb_id="KB0001", tenant_id="T1", audiences=_ALL)
    assert art is not None
    assert art["title"] == "Fix VPN disconnects"


async def test_get_misses_a_draft(store):
    assert await store.get(kb_id="KB0004", tenant_id="T1", audiences=_ALL) is None


async def test_get_misses_out_of_audience(store):
    assert await store.get(kb_id="KB0005", tenant_id="T1",
                           audiences=("all", "end_user")) is None


async def test_get_is_tenant_scoped(store):
    assert await store.get(kb_id="KB9001", tenant_id="T1", audiences=_ALL) is None


# ── linked_to — cross-UC KB-for-this-entity ───────────────────────────────


async def test_linked_to_finds_articles_by_incident(store):
    hits = await store.linked_to(entity_id="INC0001", tenant_id="T1",
                                 audiences=_ALL)
    assert [h["kb_id"] for h in hits] == ["KB0001"]


async def test_linked_to_finds_articles_by_ci(store):
    hits = await store.linked_to(entity_id="CI0009", tenant_id="T1",
                                 audiences=_ALL)
    assert [h["kb_id"] for h in hits] == ["KB0002"]


async def test_linked_to_unknown_entity_is_empty(store):
    assert await store.linked_to(entity_id="INC9999", tenant_id="T1",
                                 audiences=_ALL) == []


# ── live backend smoke ────────────────────────────────────────────────────


def test_postgres_backend_exposes_kb_store_protocol():
    # Replaces the older NotImplementedError guard. PostgresKbStore is now a
    # real implementation; this test just asserts the protocol surface is in
    # place. Behavioural tests against a live DB live in integration suites.
    store = PostgresKbStore()
    for name in ("get", "exists", "search", "linked_to"):
        assert callable(getattr(store, name, None)), f"missing {name}"
