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
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

from oneops.observability import get_logger, get_tracer, set_langfuse_io
from oneops.registry.service import RegistryService

# Telemetry literals → constants (sonar S1192).
_ONEOPS_ROUTER_CANDIDATE_COUNT = "oneops.router.candidate_count"

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
                span.set_attribute(_ONEOPS_ROUTER_CANDIDATE_COUNT, 0)
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
            span.set_attribute(_ONEOPS_ROUTER_CANDIDATE_COUNT, len(result))
            span.set_attribute(
                "oneops.router.candidate_ids",
                ",".join(c.agent_id for c in result))
            set_langfuse_io(
                span, input=query_text,
                output=[{"agent_id": c.agent_id, "score": round(c.score, 3)}
                        for c in result])
            return result


async def configure_hnsw_connection(conn: Any) -> None:
    """Pool `init` for the retriever's asyncpg connections — tunes pgvector HNSW
    so the filtered ANN query behaves correctly at scale. Best-effort: any
    failure (older pgvector without these GUCs) is swallowed; retrieval still
    works, just untuned.

      * loads the pgvector library first (its GUCs only register after the
        first vector op in a session),
      * iterative_scan='relaxed_order' — REQUIRED for filtered ANN: without it a
        `WHERE domain=… ORDER BY <=> LIMIT N` can return < N rows (filter drops
        index hits); relaxed_order keeps searching to fill the limit (we re-rank
        in the outer query anyway, so relaxed vs strict order is fine + faster),
      * ef_search=200 — search breadth ≥ our candidate-chunk LIMIT for recall.
    """
    try:
        await conn.execute("SELECT '[1,2,3]'::vector")          # load pgvector GUCs
        await conn.execute("SET hnsw.iterative_scan = 'relaxed_order'")
        await conn.execute("SET hnsw.ef_search = 200")
    except Exception:                                            # noqa: BLE001
        pass


class GatewayEmbedder:
    """Embeds query text through the LLM Gateway — the single egress (§2.5).

    Uses the SAME model + dimensions as the stored agent vectors
    (database/agent/worker.py → text-embedding-3-large @ 1536) so the query and
    the corpus live in one embedding space; otherwise cosine is meaningless.
    """

    def __init__(
        self, gateway: Any, *,
        model: str = "text-embedding-3-large", dimensions: int = 1536,
        cache: Any | None = None,
    ) -> None:
        self._gateway = gateway
        self._model = model
        self._dimensions = dimensions
        # Optional QueryEmbeddingCache (router/route_cache.py). The query vector
        # is deterministic per (model, dimensions) and tenant-independent, so a
        # repeated query reuses the vector instead of a gateway round-trip.
        # None ⇒ disabled (no behaviour change).
        self._cache = cache

    async def embed(self, text: str, *, tenant_id: str) -> list[float]:
        key = None
        if self._cache is not None:
            try:
                from oneops.observability.metrics import increment
                from oneops.router.route_cache import embedding_cache_key
                key = embedding_cache_key(
                    text=text, model=self._model, dimensions=self._dimensions)
                cached = await self._cache.get(key=key)
                if cached is not None:
                    increment("oneops.router.embedding_cache.hit")
                    return list(cached)
                increment("oneops.router.embedding_cache.miss")
            except Exception:                                      # noqa: BLE001
                key = None  # cache failure must never break embedding
        vectors = await self._gateway.embed(
            [text], model=self._model, tenant_id=tenant_id,
            dimensions=self._dimensions)
        vector = vectors[0]
        if key is not None:
            try:
                await self._cache.put(key=key, vector=vector)
            except Exception:                                      # noqa: BLE001
                pass
        return vector


class PgVectorRetriever:
    """Production retriever — query embedding (LLM Gateway) + pgvector kNN over
    `ai.embeddings_agent` (the per-facet agent routing vectors, ADR-0002).

    The agent vectors are GLOBAL (no tenant_id — the registry serves every
    tenant); tenant/role scoping happens downstream in the activation-condition
    + ABAC filter, not here (§2.4). Each agent has multiple chunks
    (description / use_when / example), so we take the BEST-matching chunk per
    agent (max cosine similarity) and return the top-K agents.
    """

    # ANN pattern: the INNER `ORDER BY embedding <=> q LIMIT N` is what the HNSW
    # index accelerates (returns the N nearest chunks without scanning the table
    # → flat latency at scale). The OUTER groups those nearest chunks to agents
    # and takes the top-K agents. An aggregate-over-all-rows (max ... GROUP BY)
    # can NEVER use HNSW — it forces a Seq Scan — which is why we retrieve
    # nearest chunks first, then aggregate. N ($5) must be generous enough that
    # every true top-K agent has a chunk in the nearest-N (recall/latency knob).
    _SQL = (
        "SELECT agent_id, max(score) AS score FROM ("
        "  SELECT agent_id, 1 - (embedding <=> $1::vector) AS score "
        "  FROM ai.embeddings_agent "
        "  WHERE embedding_version = $2 "
        "    AND ($4::text IS NULL OR domain = $4) "   # domain scope (NULL = all)
        "  ORDER BY embedding <=> $1::vector "          # ← HNSW ANN (index scan)
        "  LIMIT $5 "                                   # candidate chunks
        ") t "
        "GROUP BY agent_id "
        "ORDER BY score DESC "
        "LIMIT $3::int"
    )

    def __init__(
        self, registry: RegistryService, *,
        embedder: Any, pool: Any, embedding_version: str = "v1",
        candidate_chunks: int = 200,
    ) -> None:
        self._registry = registry
        self._embedder = embedder        # async: embed(text, *, tenant_id) -> list[float]
        self._pool = pool                # asyncpg pool
        self._embedding_version = embedding_version
        # How many nearest chunks the inner ANN fetches before grouping to
        # agents. Bigger = better recall of the true top-K agents, slightly
        # slower. ~200 covers ~15 agents at ~13 chunks each with headroom.
        self._candidate_chunks = candidate_chunks

    async def prewarm_embed(self, query_text: str, *, tenant_id: str) -> None:
        """Populate the query-embedding cache for `query_text` ahead of
        `retrieve()`, so a later retrieve on the same (normalized) text is a
        cache hit instead of a fresh gateway round-trip. The router fires this
        CONCURRENTLY with the decompose/split LLM call (both read only the raw
        message), overlapping the embed with the split. Best-effort: it embeds
        through the same `GatewayEmbedder` (single egress + cache); any error is
        swallowed — `retrieve()` will embed normally. No-op when the text is
        empty or no embedding cache is configured (the embed would not be
        reused, so warming it would be pure waste)."""
        if not query_text.strip():
            return
        if getattr(self._embedder, "_cache", None) is None:
            return
        with suppress(Exception):
            await self._embedder.embed(query_text, tenant_id=tenant_id)

    async def retrieve(
        self, query_text: str, *, tenant_id: str, top_k: int,
        domain: str | None = None,
    ) -> list[Candidate]:
        with _tracer.start_as_current_span(
            "router.stage2.retrieve",
            attributes={
                "oneops.router.stage": "2",
                "oneops.router.retriever": "pgvector",
                "oneops.router.top_k": top_k,
                "oneops.router.domain": domain or "all",
            },
        ) as span:
            if not query_text.strip():
                span.set_attribute(_ONEOPS_ROUTER_CANDIDATE_COUNT, 0)
                return []
            # Single egress (§2.5): query embedded via the gateway, same space
            # as the stored agent vectors. Errors propagate — no silent failure
            # (§2.7); the caller's funnel decides the fallback. domain=None
            # retrieves across all domains; a value scopes to one domain (ITOM).
            vector = await self._embedder.embed(query_text, tenant_id=tenant_id)
            vec_literal = "[" + ",".join(repr(float(x)) for x in vector) + "]"
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    self._SQL, vec_literal, self._embedding_version, top_k,
                    domain, self._candidate_chunks)
            result = [Candidate(r["agent_id"], float(r["score"])) for r in rows]
            span.set_attribute(_ONEOPS_ROUTER_CANDIDATE_COUNT, len(result))
            span.set_attribute(
                "oneops.router.candidate_ids",
                ",".join(c.agent_id for c in result))
            set_langfuse_io(
                span, input=query_text,
                output=[{"agent_id": c.agent_id, "score": round(c.score, 3)}
                        for c in result])
            return result


__all__ = [
    "Candidate", "CandidateRetriever", "GatewayEmbedder",
    "LexicalRetriever", "PgVectorRetriever", "configure_hnsw_connection",
]
