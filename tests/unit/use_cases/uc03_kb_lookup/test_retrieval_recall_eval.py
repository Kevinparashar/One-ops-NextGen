"""(c) Day-1 retrieval scorecard — Recall@K on known symptom→KB pairs.

2026 RAG best practice: evaluate RETRIEVAL quality from day one (does the right
article rank in the top-K), not just generation. This locks KB retrieval quality
and catches regressions early: for known problems with a known-correct article,
that article MUST appear in the top-K of the hybrid (FTS+vector→RRF→gate) search.

Deterministic — runs on the in-memory KB store (no live infra). A live variant
would run the same scorecard against the real centralized KB index.
"""
from __future__ import annotations

import pytest

from oneops.use_cases._shared.kb_store import InMemoryKbStore, set_kb_store
from oneops.use_cases.uc03_kb_lookup.handlers import search_kb

TOP_K = 3
RECALL_FLOOR = 0.75            # ≥3/4 known pairs must surface in top-K

# (symptom query, expected KB id, role) — known-good problem→article pairs.
PAIRS = [
    ("vpn disconnects on wifi handoff", "KB0001", "employee"),
    ("vpn tunnel drops when roaming",   "KB0001", "employee"),
    ("reset my email password",         "KB0002", "employee"),
    ("vpn internals deep dive runbook", "KB0003", "technician"),
]


@pytest.fixture
def store() -> InMemoryKbStore:
    s = InMemoryKbStore()
    s.seed(kb_id="KB0001", tenant_id="T1", title="Fix VPN disconnects on Wi-Fi handoff",
           summary="Resolve VPN tunnel drops when roaming between APs",
           content="apply vpn client profile v2.3 to fix tunnel drops when roaming",
           tags=["vpn"], state="published", audience="all", helpful_votes=100)
    s.seed(kb_id="KB0002", tenant_id="T1", title="Email password reset",
           summary="Reset your email password", content="open the portal and reset",
           tags=["email"], state="published", audience="all", helpful_votes=5)
    s.seed(kb_id="KB0003", tenant_id="T1", title="VPN internals runbook",
           summary="vpn deep dive internals", content="vpn internals deep dive",
           tags=["vpn"], state="published", audience="technician", helpful_votes=50)
    set_kb_store(s)
    return s


async def test_retrieval_recall_at_k(store):
    hits = 0
    misses: list[tuple[str, str, list[str]]] = []
    for query, expected, role in PAIRS:
        out = await search_kb({"query": query}, {"tenant_id": "T1", "role": role})
        ids = [a["kb_id"] for a in out.get("articles", [])][:TOP_K]
        if expected in ids:
            hits += 1
        else:
            misses.append((query, expected, ids))
    recall = hits / len(PAIRS)
    assert recall >= RECALL_FLOOR, (
        f"Recall@{TOP_K} = {recall:.2f} < floor {RECALL_FLOOR}. Misses: {misses}")
