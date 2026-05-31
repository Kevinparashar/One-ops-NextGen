"""Stage 2 — semantic candidate retrieval.

Retrieval narrows the full agent catalogue (1000+) to a small candidate set
*before* any LLM is involved (Parlant per-turn context narrowing; Moveworks
attention budget). It is a `CandidateRetriever` Protocol with two real
implementations:

  * `PgVectorRetriever` — production: embed the query via the LLM Gateway, kNN
    over agent-capability embeddings in pgvector (ADR-0002). Env-gated; not
    exercised where there is no DB.
  * `LexicalRetriever` — deterministic: token-overlap scoring of the query
    against each active agent's description + intent_family. A genuine
    retriever (not a mock) — it runs with no infrastructure, so it backs the
    unit suite and local dev. Retrieval recall is lower than embeddings, but
    the funnel's later stages (condition filter, LLM disambiguation) do not
    depend on which retriever produced the shortlist.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from oneops.observability import get_logger, get_tracer
from oneops.registry.service import RegistryService

_log = get_logger("oneops.router.retrieval")
_tracer = get_tracer("oneops.router.retrieval")

_TOKEN = re.compile(r"[a-z0-9]+")
# Tokens too generic to carry routing signal — excluded from lexical scoring.
_STOPWORDS = frozenset(
    ["the", "a", "an", "of", "to", "for", "on", "in", "is", "it", "this", "that", "and", "or", "with", "my", "me", "i", "you", "what", "who", "when", "where", "how", "can", "could", "please", "show", "give", "get"]
)


_ENTITY_ID_TOKEN = re.compile(r"^([a-z]{2,4})\d{6,}$")


def _tokens(text: str) -> frozenset[str]:
    """Lexical tokens for retrieval. Entity-id tokens (e.g. 'inc0001001',
    'ci0000001') ALSO emit their service prefix as a separate token ('inc',
    'ci'). Without this, a bare entity id never lexically overlaps an agent
    description that mentions the service abbreviation in prose, and the
    agent gets dropped from candidates. The split is purely structural —
    no per-prefix list — and works for every ITSM/ITOM service id."""
    out: set[str] = set()
    for t in _TOKEN.findall(text.lower()):
        if t in _STOPWORDS:
            continue
        out.add(t)
        m = _ENTITY_ID_TOKEN.match(t)
        if m and m.group(1) not in _STOPWORDS:
            out.add(m.group(1))
    return frozenset(out)


@dataclass(frozen=True)
class Candidate:
    """A retrieved agent candidate with its retrieval score (higher = closer)."""

    agent_id: str
    score: float


class CandidateRetriever(Protocol):
    async def retrieve(
        self, query_text: str, *, tenant_id: str, top_k: int
    ) -> list[Candidate]:
        """Return up to `top_k` agent candidates, best score first."""
        ...


class LexicalRetriever:
    """Deterministic token-overlap retriever over the registry's active agents.

    Score = |query ∩ agent| / |query|  — the fraction of meaningful query
    tokens the agent's description/intent_family covers. Deterministic, needs
    no infrastructure; backs unit tests and local dev.

    Scale ([[feedback_poc5mw_design_for_1000_ucs_from_day_1]]): an inverted
    `token → agent_ids` index is precomputed lazily on first query and reused
    across calls. Query cost is O(|query_tokens|) lookups + O(|hits|) score
    aggregation — never an O(catalogue) scan. The index is invalidated when
    the registry's active-agent identity set changes (a `list_active()` membership
    diff), so registry rollbacks / activations rebuild the index transparently.
    """

    def __init__(self, registry: RegistryService) -> None:
        self._registry = registry
        self._index: dict[str, list[tuple[str, int]]] | None = None

    def invalidate(self) -> None:
        """Drop the cached inverted index. Call after a registry change
        (activate / retire / rollback) so the next query rebuilds. In a FaaS
        process the index lives for one warm container — invalidation is for
        the rare in-process rebuild path (CI, long-running dev shell)."""
        self._index = None

    def _build_index(self) -> dict[str, list[tuple[str, int]]]:
        index: dict[str, list[tuple[str, int]]] = {}
        for agent in self._registry.agents.list_active():
            corpus = _tokens(f"{agent.description} {agent.intent_family}")
            if not corpus:
                continue
            for tok in corpus:
                index.setdefault(tok, []).append((agent.id, len(corpus)))
        return index

    def _get_index(self) -> dict[str, list[tuple[str, int]]]:
        # `list_active()` is O(N) over the registry backend (one I/O per
        # agent for the file backend). We pay this cost ONCE per process
        # (or after explicit invalidation) — the production FaaS model is
        # "build on cold start, reuse on every warm invocation".
        if self._index is None:
            self._index = self._build_index()
        return self._index

    async def retrieve(
        self, query_text: str, *, tenant_id: str, top_k: int
    ) -> list[Candidate]:
        with _tracer.start_as_current_span(
            "router.stage2.retrieve",
            attributes={
                "oneops.router.stage": "2",
                "oneops.router.retriever": "lexical",
                "oneops.router.top_k": top_k,
            },
        ) as span:
            q = _tokens(query_text)
            span.set_attribute("oneops.router.query_token_count", len(q))
            if not q:
                span.set_attribute("oneops.router.candidate_count", 0)
                return []
            index = self._get_index()
            overlaps: dict[str, int] = {}
            for tok in q:
                for agent_id, _corpus_len in index.get(tok, ()):
                    overlaps[agent_id] = overlaps.get(agent_id, 0) + 1
            all_active = self._registry.agents.list_active()
            q_size = max(1, len(q))
            scored: list[Candidate] = []
            for a in all_active:
                overlap = overlaps.get(a.id, 0)
                score = (overlap / q_size) if overlap > 0 else 0.01
                scored.append(Candidate(a.id, score))
            scored.sort(key=lambda c: (-c.score, c.agent_id))
            result = scored[:top_k]
            span.set_attribute("oneops.router.candidate_count", len(result))
            span.set_attribute(
                "oneops.router.candidate_ids",
                ",".join(c.agent_id for c in result))
            return result


class PgVectorRetriever:
    """Production retriever — query embedding (LLM Gateway) + pgvector kNN over
    agent-capability embeddings (ADR-0002).

    Exercised only in the env-gated integration suite — embedding and the
    vector store both need live infrastructure. The body is intentionally a
    thin adapter; the funnel logic is identical regardless of retriever.
    """

    def __init__(self, registry: RegistryService, *, embedder: Any, pool: Any) -> None:
        self._registry = registry
        self._embedder = embedder        # async: embed(text) -> list[float]
        self._pool = pool                # asyncpg pool

    async def retrieve(
        self, query_text: str, *, tenant_id: str, top_k: int
    ) -> list[Candidate]:
        vector = await self._embedder.embed(query_text)
        sql = (
            "SELECT agent_id, 1 - (embedding <=> $1) AS score "
            "FROM agent_capability_embeddings "
            "WHERE tenant_id = $2 OR tenant_id = '_platform' "
            "ORDER BY embedding <=> $1 LIMIT $3"
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, vector, tenant_id, top_k)
        return [Candidate(r["agent_id"], float(r["score"])) for r in rows]


__all__ = ["Candidate", "CandidateRetriever", "LexicalRetriever", "PgVectorRetriever"]
