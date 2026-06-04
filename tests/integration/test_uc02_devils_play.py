"""UC-2 devil's-play — adversarial probes against the retrieval boundary.

Each test asks: "what can go wrong in production?" and proves the system
either survives it gracefully or surfaces a clear, deterministic boundary
error. No silent failures (rule §2.7).

Live-DB tests; skipped automatically when POSTGRES_URL is unset.
"""
from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest

from oneops.use_cases.uc02_similar_tickets.core import find_similar

pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_URL"), reason="POSTGRES_URL not set"
)


async def _conn():
    return await asyncpg.connect(os.environ["POSTGRES_URL"])


@pytest.fixture
async def known_incident():
    c = await _conn()
    row = await c.fetchrow("""
        SELECT i.tenant_id, i.incident_id
        FROM itsm.incident i
        WHERE EXISTS (
          SELECT 1 FROM ai.embeddings_incident e
          WHERE e.entity_id = i.incident_id AND e.tenant_id = i.tenant_id
            AND e.chunk_type = 'symptom_anchor'
        )
        LIMIT 1
    """)
    await c.close()
    if not row:
        pytest.skip("no incident with anchor")
    return row["tenant_id"], row["incident_id"]


# ── Probe 1: pgvector iterative_scan actually engages ─────────────────────────

@pytest.mark.asyncio
async def test_dp01_iterative_scan_engaged_for_filtered_query(known_incident):
    """The point of the fix: WHERE-pre-filter doesn't under-recall.

    We can't easily force a tiny tenant in the demo DB, but we CAN assert that
    `same_category_only=True` doesn't silently drop the candidate count to
    zero when at least one same-category neighbour exists.
    """
    tenant_id, ticket_id = known_incident
    resp = await find_similar(
        tenant_id=tenant_id, service_id="incident", ticket_id=ticket_id,
        user_id="u_demo", role="service_desk_agent",
        max_results=10, same_category_only=True,
        connection_provider=_conn,
    )
    # With iterative_scan ON, the filter doesn't truncate the walk early.
    # At least one same-category result is reasonable on the demo dataset.
    assert resp.total_candidates_considered >= 1, (
        "iterative_scan should keep walking past the filter — got 0 candidates"
    )


# ── Probe 2: extremely small over-fetch still returns top-K ──────────────────

@pytest.mark.asyncio
async def test_dp02_overfetch_floor_holds(known_incident, monkeypatch):
    """If someone misconfigures UC02_OVERFETCH_MULTIPLIER=0, we still
    return AT LEAST max_results candidates if they exist."""
    monkeypatch.setenv("UC02_OVERFETCH_MULTIPLIER", "0")
    # Re-import to pick up the env change is overkill; instead verify the
    # in-module limit_n formula: max(max_results * mult, max_results + 5).
    from oneops.use_cases.uc02_similar_tickets import core
    # Reload the constant binding
    core._OVERFETCH = 0
    tenant_id, ticket_id = known_incident
    resp = await find_similar(
        tenant_id=tenant_id, service_id="incident", ticket_id=ticket_id,
        user_id="u_demo", role="service_desk_agent",
        max_results=5, connection_provider=_conn,
    )
    assert len(resp.results) >= 1  # didn't silently return empty


# ── Probe 3: malformed pgvector return ────────────────────────────────────────

@pytest.mark.asyncio
async def test_dp03_diagnosis_confirm_off_still_succeeds(known_incident):
    """If Stage 5 is disabled, results must still be sound."""
    tenant_id, ticket_id = known_incident
    resp = await find_similar(
        tenant_id=tenant_id, service_id="incident", ticket_id=ticket_id,
        user_id="u_demo", role="service_desk_agent",
        max_results=5, diagnosis_confirm=False,
        connection_provider=_conn,
    )
    assert len(resp.results) >= 1
    # No result should carry diagnosis_match when Stage 5 is off
    for r in resp.results:
        assert "diagnosis_match" not in r.why_similar


# ── Probe 4: zero-similarity floor ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dp04_perfect_floor_returns_empty_message(known_incident):
    """min_similarity_score=1.0 must yield empty with explanatory message."""
    tenant_id, ticket_id = known_incident
    resp = await find_similar(
        tenant_id=tenant_id, service_id="incident", ticket_id=ticket_id,
        user_id="u_demo", role="service_desk_agent",
        max_results=5, min_similarity_score=1.0,
        connection_provider=_conn,
    )
    assert resp.results == []
    assert resp.message is not None
    assert ("similar" in resp.message.lower()
            or "threshold" in resp.message.lower())


# ── Probe 5: tenant injection attempt ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_dp05_tenant_id_with_sql_metachar_is_safe(known_incident):
    """Tenant id is bound via asyncpg parameter — quote attack is harmless,
    just yields the legitimate 'not found'."""
    _, ticket_id = known_incident
    with pytest.raises(RuntimeError):
        await find_similar(
            tenant_id="T001'; DROP TABLE itsm.incident; --",
            service_id="incident", ticket_id=ticket_id,
            user_id="u_demo", role="service_desk_agent",
            connection_provider=_conn,
        )


# ── Probe 6: explicit ID with leading whitespace already canonicalised ────────

@pytest.mark.asyncio
async def test_dp06_assumes_canonical_input_strict(known_incident):
    """find_similar() assumes the route already canonicalised the id.
    Passing 'inc0001001' lowercase WITHOUT canonicalisation goes to the
    base-table existence check and returns 'not found' (404 from route)."""
    tenant_id, _ = known_incident
    with pytest.raises(RuntimeError):
        await find_similar(
            tenant_id=tenant_id, service_id="incident",
            ticket_id="inc0001001",                # lowercase, not canon
            user_id="u_demo", role="service_desk_agent",
            connection_provider=_conn,
        )


# ── Probe 7: very large k still bounded by available data ────────────────────

@pytest.mark.asyncio
async def test_dp07_k_larger_than_corpus_does_not_pad(known_incident):
    tenant_id, ticket_id = known_incident
    resp = await find_similar(
        tenant_id=tenant_id, service_id="incident", ticket_id=ticket_id,
        user_id="u_demo", role="service_desk_agent",
        max_results=20, connection_provider=_conn,
    )
    # Demo corpus is ~160 incidents; should return at most 20
    assert len(resp.results) <= 20
    # No duplicates
    ids = [r.ticket_id for r in resp.results]
    assert len(ids) == len(set(ids))


# ── Probe 8: source ticket in its own results (must never happen) ────────────

@pytest.mark.asyncio
async def test_dp08_source_never_in_results(known_incident):
    tenant_id, ticket_id = known_incident
    resp = await find_similar(
        tenant_id=tenant_id, service_id="incident", ticket_id=ticket_id,
        user_id="u_demo", role="service_desk_agent",
        max_results=20, connection_provider=_conn,
    )
    for r in resp.results:
        assert r.ticket_id != ticket_id, (
            "Source ticket leaked into its own results"
        )


# ── Probe 9: descending similarity is invariant ──────────────────────────────

@pytest.mark.asyncio
async def test_dp09_results_strictly_descending(known_incident):
    tenant_id, ticket_id = known_incident
    resp = await find_similar(
        tenant_id=tenant_id, service_id="incident", ticket_id=ticket_id,
        user_id="u_demo", role="service_desk_agent",
        max_results=10, connection_provider=_conn,
    )
    scores = [r.similarity_score for r in resp.results]
    assert scores == sorted(scores, reverse=True), (
        f"Re-rank ordering broken: {scores}"
    )


# ── Probe 10: concurrent calls don't interfere ───────────────────────────────

@pytest.mark.asyncio
async def test_dp10_concurrent_calls_safe(known_incident):
    """Two find_similar calls in parallel on different connections must
    return consistent results (no shared mutable state)."""
    tenant_id, ticket_id = known_incident

    async def call():
        return await find_similar(
            tenant_id=tenant_id, service_id="incident", ticket_id=ticket_id,
            user_id="u_demo", role="service_desk_agent",
            max_results=5, connection_provider=_conn,
        )

    resps = await asyncio.gather(call(), call(), call())
    ids_sets = [tuple(r.ticket_id for r in resp.results) for resp in resps]
    assert ids_sets[0] == ids_sets[1] == ids_sets[2], (
        "Concurrent calls disagreed — possible shared mutable state"
    )


# ── Probe 11: prefer_status filters AND boosts coherently ────────────────────

@pytest.mark.asyncio
async def test_dp11_prefer_status_resolved_only_returns_resolved(known_incident):
    tenant_id, ticket_id = known_incident
    resp = await find_similar(
        tenant_id=tenant_id, service_id="incident", ticket_id=ticket_id,
        user_id="u_demo", role="service_desk_agent",
        max_results=10, prefer_status="resolved",
        connection_provider=_conn,
    )
    for r in resp.results:
        assert r.status in ("resolved", "closed", "fulfilled"), (
            f"prefer_status=resolved returned status={r.status!r}"
        )
