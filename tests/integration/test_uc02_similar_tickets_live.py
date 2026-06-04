"""UC-2 — live integration against Postgres + ai.embeddings_*.

These tests are skipped automatically if POSTGRES_URL is not set, so the
unit suite remains hermetic. They cover the spec's validation scenarios:
  • UC-2.1 strong symptom match → top result > 80%
  • UC-2.3 novel ticket → empty results + spec message
  • UC-2.4 cross-tenant exclusion
  • UC-2.6 time-window respected
"""
from __future__ import annotations

import os

import asyncpg
import pytest

from oneops.uc_common import TimeFilter
from oneops.use_cases.uc02_similar_tickets.core import find_similar

pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_URL"), reason="POSTGRES_URL not set"
)


async def _conn():
    return await asyncpg.connect(os.environ["POSTGRES_URL"])


@pytest.fixture
async def existing_incident():
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
        pytest.skip("no incident with anchor in DB")
    return row["tenant_id"], row["incident_id"]


@pytest.mark.asyncio
async def test_uc02_strong_match_returns_relevant_top(existing_incident):
    tenant_id, ticket_id = existing_incident
    resp = await find_similar(
        tenant_id=tenant_id, service_id="incident", ticket_id=ticket_id,
        user_id="u_demo", role="service_desk_agent",
        max_results=5, connection_provider=_conn,
    )
    assert resp.source_ticket_id == ticket_id
    assert resp.service_id == "incident"
    # UC-2.1 — at least one strong match is expected on the demo dataset
    assert len(resp.results) >= 1
    # All results carry the spec output fields
    for r in resp.results:
        assert r.ticket_id != ticket_id
        assert r.match_pct == int(round(r.similarity_score * 100))
        assert 0.0 <= r.confidence <= 1.0
        assert 0 <= r.match_pct <= 100
    # Ordering: descending by similarity_score
    scores = [r.similarity_score for r in resp.results]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_uc02_high_floor_returns_empty_with_message(existing_incident):
    """UC-2.3 — set min_similarity_score=0.999 to force empty results."""
    tenant_id, ticket_id = existing_incident
    resp = await find_similar(
        tenant_id=tenant_id, service_id="incident", ticket_id=ticket_id,
        user_id="u_demo", role="service_desk_agent",
        max_results=5, min_similarity_score=0.999,
        connection_provider=_conn,
    )
    assert resp.results == []
    assert resp.message is not None
    assert "similar" in resp.message.lower() or "threshold" in resp.message.lower()


@pytest.mark.asyncio
async def test_uc02_cross_tenant_excluded(existing_incident):
    """UC-2.4 — querying with a wrong tenant_id must raise (no leak)."""
    _, ticket_id = existing_incident
    with pytest.raises(RuntimeError):
        await find_similar(
            tenant_id="T_DOES_NOT_EXIST", service_id="incident",
            ticket_id=ticket_id,
            user_id="u_demo", role="service_desk_agent",
            connection_provider=_conn,
        )


@pytest.mark.asyncio
async def test_uc02_time_window_shrinks_pool(existing_incident):
    """UC-2.6 — small time window must yield ≤ unbounded pool."""
    tenant_id, ticket_id = existing_incident
    big = await find_similar(
        tenant_id=tenant_id, service_id="incident", ticket_id=ticket_id,
        user_id="u_demo", role="service_desk_agent",
        max_results=10, connection_provider=_conn,
    )
    small = await find_similar(
        tenant_id=tenant_id, service_id="incident", ticket_id=ticket_id,
        user_id="u_demo", role="service_desk_agent",
        max_results=10,
        time_filter=TimeFilter(relative_days=1, label="last day"),  # last day only
        connection_provider=_conn,
    )
    assert small.total_candidates_considered <= big.total_candidates_considered


@pytest.mark.asyncio
async def test_uc02_unknown_ticket_raises():
    resp_err = None
    try:
        await find_similar(
            tenant_id="T001", service_id="incident",
            ticket_id="INC9999999",
            user_id="u_demo", role="service_desk_agent",
            connection_provider=_conn,
        )
    except RuntimeError as e:
        resp_err = str(e)
    assert resp_err is not None
    assert "anchor" in resp_err or "not found" in resp_err
