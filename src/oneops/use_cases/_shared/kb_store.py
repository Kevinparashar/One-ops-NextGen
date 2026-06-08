"""Knowledge-base data access for UC-3 — a pluggable backend.

Like `TicketStore`, UC-3's handlers must not hard-wire a database. Data access
is the `KbStore` protocol with two interchangeable backends:

  * `InMemoryKbStore` — deterministic, seeded, no I/O. Search is a deterministic
    keyword-overlap score (no LLM, no vectors). The default; what unit tests and
    the no-infra executor run on.
  * `PostgresKbStore` — the live backend over the `itsm` schema, env-gated.
    Not implemented yet (the live KB backend is a separate, deliberate step);
    it fails loud rather than silently or by importing old-version code.

Visibility is part of the contract: every method takes `tenant_id` and an
`audiences` allow-list, and a backend MUST return only `state = 'published'`
articles whose `audience` is in that list. Tenant isolation and audience/RBAC
scoping happen at the data layer, never as an afterthought in the handler.

Semantic (vector) search is deliberately out of scope here — the `itsm`
`kb_knowledge` table has no embedding column yet. v1 is keyword search; the
deterministic store and the future live store both rank by keyword overlap.
"""
from __future__ import annotations

import os
import re
from typing import Any, Protocol, runtime_checkable

from oneops.observability import get_logger, increment

_log = get_logger("oneops.use_cases.kb_store")

# Telemetry / DB literals (single source — sonar S1192).
_TRACER_NAME = "oneops.kb_store.postgres"
_ATTR_DB_SYSTEM = "db.system"
_ATTR_DB_STMT = "db.statement.name"
_ATTR_TENANT = "oneops.tenant_id"
_METRIC_PG_ERRORS = "ai.postgres.errors.total"
_ATTR_ROW_COUNT = "db.row_count"

# A search token: a run of word characters, lower-cased. Deterministic.
_WORD = re.compile(r"[0-9a-z]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


@runtime_checkable
class KbStore(Protocol):
    """Read access to published knowledge-base articles, tenant + audience
    scoped. Every method returns only `published` articles whose `audience`
    is in the caller-supplied `audiences` allow-list."""

    async def search(
        self, *, query: str, tenant_id: str, audiences: tuple[str, ...],
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Keyword search — articles ranked by query-term overlap, best first.
        Each result carries a `relevance_score` (count of distinct query terms
        matched). Articles with zero overlap are not returned."""
        ...

    async def search_semantic(
        self, *, query_vec: list[float], tenant_id: str,
        audiences: tuple[str, ...], limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Vector search — articles ranked by semantic similarity to the
        pre-computed query embedding. The KbStore does NOT call the LLM
        gateway itself (keeps the data layer pure + testable); the caller
        is responsible for embedding the query through `LlmGateway.embed`,
        which is where the OTel/cost/retry plumbing already lives.

        Returns the same dict shape as `search()`; `relevance_score` is
        bucketed `1..100` from the cosine similarity (higher = closer).
        Articles without an `embedding` are skipped silently — they are
        unreachable by semantic path until back-filled."""
        ...

    async def get(
        self, *, kb_id: str, tenant_id: str, audiences: tuple[str, ...],
    ) -> dict[str, Any] | None:
        """Fetch one article by id, or `None` if it does not exist / is not
        published / is outside the audience allow-list."""
        ...

    async def exists(
        self, *, kb_id: str, tenant_id: str,
    ) -> dict[str, Any] | None:
        """Tenant-scoped existence check, NO audience filter.

        Returns a thin row `{kb_id, state, audience}` when the article
        exists for this tenant in any state and any audience; `None`
        when no such row exists for this tenant. The handler uses this
        to distinguish three cases in the chat reply:

          1. `exists()` returns None             → "no such KB" reply
          2. `exists()` returns row, `get()` None → "exists but you
                                                    can't access" reply
                                                    (audience / state mismatch)
          3. Both return → compose the answer

        This mirrors the ticket-side `id_validator` tri-state contract.
        Tenant isolation is structural — tenant_id is the first
        predicate in every implementation."""
        ...

    async def linked_to(
        self, *, entity_id: str, tenant_id: str, audiences: tuple[str, ...],
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Articles whose `related_incidents` or `related_ci_ids` reference
        `entity_id` — the cross-UC 'KB for this ticket/CI' lookup."""
        ...


class InMemoryKbStore:
    """Deterministic, in-process `KbStore` — the no-infrastructure default.

    Articles are seeded explicitly; nothing is fabricated. Search is a
    deterministic keyword-overlap score so a unit test gets the same ranking
    every run."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    def seed(self, *, kb_id: str, tenant_id: str, **fields: Any) -> None:
        self._rows[(tenant_id, kb_id)] = {
            "kb_id": kb_id, "tenant_id": tenant_id, **fields}

    def clear(self) -> None:
        self._rows.clear()

    def _visible(self, tenant_id: str, audiences: tuple[str, ...]) -> list[dict[str, Any]]:
        return [
            r for (t, _), r in self._rows.items()
            if t == tenant_id
            and r.get("state") == "published"
            and r.get("audience") in audiences
        ]

    @staticmethod
    def _searchable(article: dict[str, Any]) -> set[str]:
        parts = [article.get("title", ""), article.get("summary", ""),
                 article.get("content", "")]
        parts.extend(str(t) for t in article.get("tags", []) or [])
        return _tokens(" ".join(parts))

    async def search(
        self, *, query: str, tenant_id: str, audiences: tuple[str, ...],
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        q = _tokens(query)
        if not q:
            return []
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for article in self._visible(tenant_id, audiences):
            overlap = len(q & self._searchable(article))
            if overlap > 0:
                scored.append((overlap, article.get("helpful_votes", 0), article))
        # Rank: most query terms matched, then most helpful — deterministic.
        scored.sort(key=lambda s: (s[0], s[1], s[2]["kb_id"]), reverse=True)
        return [{**a, "relevance_score": score} for score, _, a in scored[:limit]]

    async def search_semantic(
        self, *, query_vec: list[float], tenant_id: str,
        audiences: tuple[str, ...], limit: int = 5,
    ) -> list[dict[str, Any]]:
        if not query_vec or not tenant_id or not audiences:
            return []
        import math
        qn = math.sqrt(sum(x * x for x in query_vec)) or 1.0
        scored: list[tuple[float, dict[str, Any]]] = []
        for article in self._visible(tenant_id, audiences):
            emb = article.get("embedding")
            if not emb or len(emb) != len(query_vec):
                continue
            dot = sum(a * b for a, b in zip(query_vec, emb, strict=False))
            en = math.sqrt(sum(x * x for x in emb)) or 1.0
            cosine = dot / (qn * en)
            scored.append((cosine, article))
        scored.sort(key=lambda s: (s[0], s[1]["kb_id"]), reverse=True)
        return [
            {**a,
             "relevance_score": max(1, min(100, int(cosine * 100))),
             "cosine_full": float(cosine),
             "_score_source": "vector_branch"}
            for cosine, a in scored[:limit]
        ]

    async def exists(
        self, *, kb_id: str, tenant_id: str,
    ) -> dict[str, Any] | None:
        if not kb_id or not tenant_id:
            return None
        row = self._rows.get((tenant_id, kb_id))
        if row is None:
            return None
        return {"kb_id": kb_id,
                "state": row.get("state", ""),
                "audience": row.get("audience", "")}

    async def fetch_embeddings_by_ids(
        self, *, tenant_id: str, kb_ids: list[str],
    ) -> dict[str, list[float]]:
        """Test parity with `PostgresKbStore.fetch_embeddings_by_ids`.
        Returns the seeded `embedding` field for each requested kb_id
        on the given tenant (matching state='published' and the row's
        audience is irrelevant here — embeddings are not audience-gated
        in storage; visibility is enforced upstream)."""
        if not tenant_id or not kb_ids:
            return {}
        out: dict[str, list[float]] = {}
        for kb_id in kb_ids:
            row = self._rows.get((tenant_id, kb_id))
            if row is None:
                continue
            emb = row.get("embedding")
            if emb and isinstance(emb, (list, tuple)):
                out[kb_id] = [float(x) for x in emb]
        return out

    async def get(
        self, *, kb_id: str, tenant_id: str, audiences: tuple[str, ...],
    ) -> dict[str, Any] | None:
        row = self._rows.get((tenant_id, kb_id))
        if (row is None or row.get("state") != "published"
                or row.get("audience") not in audiences):
            return None
        return dict(row)

    async def linked_to(
        self, *, entity_id: str, tenant_id: str, audiences: tuple[str, ...],
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        hits = [
            a for a in self._visible(tenant_id, audiences)
            if entity_id in (a.get("related_incidents") or [])
            or entity_id in (a.get("related_ci_ids") or [])
        ]
        hits.sort(key=lambda a: (a.get("helpful_votes", 0), a["kb_id"]), reverse=True)
        return [dict(a) for a in hits[:limit]]


class PostgresKbStore:
    """Live `KbStore` over the `itsm.kb_knowledge` table on the NextGen-ai
    Supabase project. Read-only, tenant + audience scoped, async-pool-backed.

    Mirrors `PostgresTicketStore`'s shape:
      * Lazy `asyncpg.Pool` on first call (`POSTGRES_URL`), SSL required,
        bounded by `POSTGRES_POOL_MIN/MAX`.
      * Per-connection `statement_timeout` + `default_transaction_read_only`
        (defence in depth — ADR-0004's incident must never repeat).
      * Tenant predicate is mandatory on every query.
      * Audience predicate `audience = ANY($N)` and state predicate
        `state = 'published'` enforced at the DB layer, never optional.

    `search()` uses Postgres FTS via the stored `content_tsv` column +
    `ts_rank_cd`. The tsvector is `GENERATED ALWAYS … STORED`, so the
    ranking signal is always fresh and the existing GIN index is used.

    `linked_to()` uses the array-overlap operator `&&` against
    `related_incidents` and `related_ci_ids` (both `text[]`).

    The raw Postgres rank (float) is bucketed into a stable integer
    `relevance_score` (1..100) before reaching the handler — UI / logs
    never see Postgres internals.
    """

    def __init__(
        self,
        *,
        dsn: str | None = None,
        pool: Any | None = None,
        statement_timeout_ms: int = 5_000,
        connect_timeout_s: float = 10.0,
    ) -> None:
        from oneops.errors import ConfigError
        self._dsn = dsn or os.getenv("POSTGRES_URL", "").strip()
        if not self._dsn and pool is None:
            raise ConfigError(
                "PostgresKbStore needs a DSN (env POSTGRES_URL) or a pool")
        self._pool = pool
        self._owns_pool = pool is None
        self._statement_timeout_ms = statement_timeout_ms
        self._connect_timeout_s = connect_timeout_s
        self._pool_lock: Any = None

    async def _ensure_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        import asyncio

        from oneops.errors import OneOpsError
        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()
        async with self._pool_lock:
            if self._pool is not None:
                return self._pool
            import asyncpg
            min_size = int(os.getenv("POSTGRES_POOL_MIN", "1"))
            max_size = int(os.getenv("POSTGRES_POOL_MAX", "5"))
            dsn = self._dsn.split("?")[0] if self._dsn else self._dsn

            async def _init_conn(conn: Any) -> None:
                await conn.execute(
                    f"SET statement_timeout = {int(self._statement_timeout_ms)}")
                await conn.execute(
                    "SET default_transaction_read_only = on")

            try:
                self._pool = await asyncpg.create_pool(
                    dsn=dsn, ssl="require",
                    min_size=min_size, max_size=max_size,
                    timeout=self._connect_timeout_s,
                    init=_init_conn,
                )
            except Exception as exc:
                raise OneOpsError(
                    "kb_store.postgres: pool create failed",
                    cause=exc) from exc
            _log.info("kb_store.postgres.pool_opened",
                      min_size=min_size, max_size=max_size)
            return self._pool

    async def close(self) -> None:
        """Graceful shutdown — close the pool if we created it. Idempotent."""
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
            _log.info("kb_store.postgres.pool_closed")
        self._pool = None

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        """asyncpg Records → plain dict (text[] stays Python list,
        timestamptz stays datetime). Matches `InMemoryKbStore`'s shape so
        the handler can't tell the backends apart."""
        return dict(row)

    async def search(
        self, *, query: str, tenant_id: str, audiences: tuple[str, ...],
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        if not query or not query.strip() or not tenant_id or not audiences:
            return []
        from opentelemetry.trace import get_tracer as _gt

        from oneops.errors import OneOpsError
        tr = _gt(_TRACER_NAME)

        pool = await self._ensure_pool()
        # Return `content` along with title/summary so the downstream
        # grounded-answer composer can quote from the article body (not
        # just the title+summary). Body length is capped composer-side
        # so attention budget is bounded regardless of article size.
        sql = """
            SELECT
                kb_id, title, summary, content, category, tags, audience,
                state, helpful_votes, views, related_incidents,
                related_ci_ids, created_at, updated_at,
                ts_rank_cd(content_tsv, plainto_tsquery('english', $2)) AS rank
            FROM itsm.kb_knowledge
            WHERE tenant_id = $1
              AND state = 'published'
              AND audience = ANY($3)
              AND content_tsv @@ plainto_tsquery('english', $2)
            ORDER BY rank DESC, helpful_votes DESC, kb_id ASC
            LIMIT $4
        """
        with tr.start_as_current_span(
            "kb_store.postgres.search",
            attributes={
                _ATTR_DB_SYSTEM: "postgresql",
                _ATTR_DB_STMT: "itsm.kb_knowledge.search",
                _ATTR_TENANT: tenant_id,
                "oneops.kb.query_terms": len(query.split()),
            },
        ) as span:
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        sql, tenant_id, query, list(audiences), limit)
            except Exception as exc:
                span.set_attribute("error", True)
                _log.warning("kb_store.postgres.search_failed",
                             error=str(exc)[:200])
                increment(_METRIC_PG_ERRORS,
                          store="kb_store", op="search",
                          reason=type(exc).__name__)
                raise OneOpsError(
                    "kb_store.postgres: search failed", cause=exc) from exc
            span.set_attribute(_ATTR_ROW_COUNT, len(rows))
            results: list[dict[str, Any]] = []
            for row in rows:
                d = self._row_to_dict(row)
                raw_rank = float(d.pop("rank", 0.0) or 0.0)
                # Bucket float rank → integer 1..100 for stable UI scoring.
                d["relevance_score"] = max(1, min(100, int(raw_rank * 100)))
                results.append(d)
            return results

    async def search_semantic(
        self, *, query_vec: list[float], tenant_id: str,
        audiences: tuple[str, ...], limit: int = 5,
    ) -> list[dict[str, Any]]:
        if not query_vec or not tenant_id or not audiences:
            return []
        from opentelemetry.trace import get_tracer as _gt

        from oneops.errors import OneOpsError
        tr = _gt(_TRACER_NAME)

        pool = await self._ensure_pool()
        # Semantic search reads ai.embeddings_kb_knowledge (chunked: 1 anchor +
        # N body chunks per article). For each article we pick its best-scoring
        # chunk via ROW_NUMBER() — UC-3 returns one row per article — so a query
        # about "Step 3" can match body chunk #3 even if the anchor doesn't.
        # `1 - (embedding <=> $vec)` = cosine similarity (HNSW uses distance `<=>`).
        sql = """
        WITH ranked AS (
            SELECT
                e.entity_id AS kb_id,
                e.chunk_type,
                1 - (e.embedding <=> $2::vector) AS similarity,
                ROW_NUMBER() OVER (
                    PARTITION BY e.entity_id
                    ORDER BY e.embedding <=> $2::vector
                ) AS rn
            FROM ai.embeddings_kb_knowledge e
            WHERE e.tenant_id = $1
        )
        SELECT
            k.kb_id, k.title, k.summary, k.content, k.category, k.tags,
            k.audience, k.state, k.helpful_votes, k.views,
            k.related_incidents, k.related_ci_ids,
            k.created_at, k.updated_at,
            r.similarity
        FROM ranked r
        JOIN itsm.kb_knowledge k
          ON k.kb_id = r.kb_id AND k.tenant_id = $1
        WHERE r.rn = 1
          AND k.state = 'published'
          AND k.audience = ANY($3)
        ORDER BY r.similarity DESC
        LIMIT $4
        """
        # asyncpg sends list[float] as a Postgres array; the explicit
        # ::vector cast (above) lets pgvector coerce it into its native
        # type so the HNSW index gets used.
        vec_literal = "[" + ",".join(repr(float(x)) for x in query_vec) + "]"
        with tr.start_as_current_span(
            "kb_store.postgres.search_semantic",
            attributes={
                _ATTR_DB_SYSTEM: "postgresql",
                _ATTR_DB_STMT: "itsm.kb_knowledge.search_semantic",
                _ATTR_TENANT: tenant_id,
                "oneops.kb.vec_dims": len(query_vec),
            },
        ) as span:
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        sql, tenant_id, vec_literal, list(audiences), limit)
            except Exception as exc:
                span.set_attribute("error", True)
                _log.warning("kb_store.postgres.search_semantic_failed",
                             error=str(exc)[:200])
                increment(_METRIC_PG_ERRORS,
                          store="kb_store", op="search_semantic",
                          reason=type(exc).__name__)
                raise OneOpsError(
                    "kb_store.postgres: search_semantic failed",
                    cause=exc) from exc
            span.set_attribute(_ATTR_ROW_COUNT, len(rows))
            results: list[dict[str, Any]] = []
            for row in rows:
                d = self._row_to_dict(row)
                sim = float(d.pop("similarity", 0.0) or 0.0)
                # Bucket for UI display (1..100). Keep the raw cosine
                # in `cosine_full` so the downstream relevance gate can
                # filter on the SAME signal Postgres ranked by, instead
                # of re-embedding the title at query time. Title-only
                # re-scoring depresses cosine ~0.15 vs full-content
                # which makes the threshold fragile — propagating the
                # original score is both cheaper and more honest.
                d["relevance_score"] = max(1, min(100, int(sim * 100)))
                d["cosine_full"] = sim
                d["_score_source"] = "vector_branch"
                results.append(d)
            return results

    async def fetch_embeddings_by_ids(
        self, *, tenant_id: str, kb_ids: list[str],
    ) -> dict[str, list[float]]:
        """Bulk-fetch stored embeddings for a set of kb_ids on one
        tenant. Used by the relevance gate to score FTS-only candidates
        (articles that surfaced via lexical search but didn't make the
        semantic top-10) without spending a second LLM-embed call per
        article. Returns `{kb_id: [float, ...]}` for every id whose
        row exists, has the right tenant, and has a non-null embedding.
        Missing or null-embedding ids are simply absent from the map.

        Same tenant-isolation contract as every other read here:
        `tenant_id` is mandatory and is the first SQL predicate."""
        if not tenant_id or not kb_ids:
            return {}
        from opentelemetry.trace import get_tracer as _gt

        from oneops.errors import OneOpsError
        tr = _gt(_TRACER_NAME)

        pool = await self._ensure_pool()
        sql = """
            SELECT kb_id, embedding
            FROM itsm.kb_knowledge
            WHERE tenant_id = $1
              AND kb_id = ANY($2)
              AND embedding IS NOT NULL
        """
        with tr.start_as_current_span(
            "kb_store.postgres.fetch_embeddings_by_ids",
            attributes={
                _ATTR_DB_SYSTEM: "postgresql",
                _ATTR_DB_STMT: "itsm.kb_knowledge.embeddings_by_id",
                _ATTR_TENANT: tenant_id,
                "oneops.kb.id_count": len(kb_ids),
            },
        ) as span:
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        sql, tenant_id, list(kb_ids))
            except Exception as exc:
                span.set_attribute("error", True)
                _log.warning(
                    "kb_store.postgres.fetch_embeddings_by_ids_failed",
                    error=str(exc)[:200])
                increment(_METRIC_PG_ERRORS,
                          store="kb_store",
                          op="fetch_embeddings_by_ids",
                          reason=type(exc).__name__)
                raise OneOpsError(
                    "kb_store.postgres: fetch_embeddings_by_ids failed",
                    cause=exc) from exc
            span.set_attribute(_ATTR_ROW_COUNT, len(rows))
            out: dict[str, list[float]] = {}
            for row in rows:
                kb_id = row["kb_id"]
                raw = row["embedding"]
                # pgvector returns embeddings as a string like
                # "[0.1,0.2,...]" via asyncpg by default. Parse it
                # into list[float]. If a future asyncpg version
                # returns a list already, the isinstance branch picks
                # that up.
                if isinstance(raw, str):
                    s = raw.strip()
                    if s.startswith("[") and s.endswith("]"):
                        s = s[1:-1]
                    try:
                        out[kb_id] = [float(x) for x in s.split(",")]
                    except ValueError:
                        continue
                elif isinstance(raw, (list, tuple)):
                    try:
                        out[kb_id] = [float(x) for x in raw]
                    except (ValueError, TypeError):
                        continue
            return out

    async def exists(
        self, *, kb_id: str, tenant_id: str,
    ) -> dict[str, Any] | None:
        """Tenant-scoped existence + (state, audience) probe — no
        audience filter. Used by the handler's 3-stage check
        (exists → audience match → return) to give the user a
        differentiated reply between "no such KB" and "exists but
        you can't access it"."""
        if not kb_id or not tenant_id:
            return None
        from opentelemetry.trace import get_tracer as _gt

        from oneops.errors import OneOpsError
        tr = _gt(_TRACER_NAME)

        pool = await self._ensure_pool()
        sql = ("SELECT kb_id, state, audience FROM itsm.kb_knowledge "
               "WHERE tenant_id = $1 AND kb_id = $2 LIMIT 1")
        with tr.start_as_current_span(
            "kb_store.postgres.exists",
            attributes={
                _ATTR_DB_SYSTEM: "postgresql",
                _ATTR_DB_STMT: "itsm.kb_knowledge.exists",
                _ATTR_TENANT: tenant_id,
                "oneops.kb_id": kb_id,
            },
        ) as span:
            try:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(sql, tenant_id, kb_id)
            except Exception as exc:
                span.set_attribute("error", True)
                _log.warning("kb_store.postgres.exists_failed",
                             error=str(exc)[:200])
                increment(_METRIC_PG_ERRORS,
                          store="kb_store", op="exists",
                          reason=type(exc).__name__)
                raise OneOpsError(
                    "kb_store.postgres: exists failed",
                    cause=exc) from exc
            span.set_attribute("db.row_found", row is not None)
            if row is None:
                return None
            return {"kb_id": row["kb_id"], "state": row["state"],
                    "audience": row["audience"]}

    async def get(
        self, *, kb_id: str, tenant_id: str, audiences: tuple[str, ...],
    ) -> dict[str, Any] | None:
        if not kb_id or not tenant_id or not audiences:
            return None
        from opentelemetry.trace import get_tracer as _gt

        from oneops.errors import OneOpsError
        tr = _gt(_TRACER_NAME)

        pool = await self._ensure_pool()
        sql = """
            SELECT kb_id, title, summary, content, category, tags,
                   audience, state, created_by, created_at, updated_at,
                   views, helpful_votes, related_ci_ids, related_incidents
            FROM itsm.kb_knowledge
            WHERE tenant_id = $1
              AND kb_id = $2
              AND state = 'published'
              AND audience = ANY($3)
            LIMIT 1
        """
        with tr.start_as_current_span(
            "kb_store.postgres.get",
            attributes={
                _ATTR_DB_SYSTEM: "postgresql",
                _ATTR_DB_STMT: "itsm.kb_knowledge.get_by_id",
                _ATTR_TENANT: tenant_id,
                "oneops.kb_id": kb_id,
            },
        ) as span:
            try:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        sql, tenant_id, kb_id, list(audiences))
            except Exception as exc:
                span.set_attribute("error", True)
                _log.warning("kb_store.postgres.get_failed",
                             error=str(exc)[:200])
                increment(_METRIC_PG_ERRORS,
                          store="kb_store", op="get",
                          reason=type(exc).__name__)
                raise OneOpsError(
                    "kb_store.postgres: get failed", cause=exc) from exc
            span.set_attribute("db.row_found", row is not None)
            return self._row_to_dict(row) if row is not None else None

    async def linked_to(
        self, *, entity_id: str, tenant_id: str, audiences: tuple[str, ...],
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        if not entity_id or not tenant_id or not audiences:
            return []
        from opentelemetry.trace import get_tracer as _gt

        from oneops.errors import OneOpsError
        tr = _gt(_TRACER_NAME)

        pool = await self._ensure_pool()
        # `content` is REQUIRED in the SELECT — the LlmAnswerComposer renders
        # the article body from the `content` column (authoritative source).
        # Omitting it produced the 2026-05-28 truncation where ticket-linked
        # KB results rendered only the one-liner `summary` field as a single
        # resolution step, dropping Symptom / Cause / multi-step Resolution
        # entirely. Always parity with `search_semantic` / `search` SELECTs.
        sql = """
            SELECT kb_id, title, summary, content, category, tags, audience,
                   state, helpful_votes, views, related_incidents,
                   related_ci_ids, created_at, updated_at
            FROM itsm.kb_knowledge
            WHERE tenant_id = $1
              AND state = 'published'
              AND audience = ANY($3)
              AND (ARRAY[$2]::text[] && related_incidents
                   OR ARRAY[$2]::text[] && related_ci_ids)
            ORDER BY helpful_votes DESC, kb_id ASC
            LIMIT $4
        """
        with tr.start_as_current_span(
            "kb_store.postgres.linked_to",
            attributes={
                _ATTR_DB_SYSTEM: "postgresql",
                _ATTR_DB_STMT: "itsm.kb_knowledge.linked_to",
                _ATTR_TENANT: tenant_id,
                "oneops.entity_id": entity_id,
            },
        ) as span:
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        sql, tenant_id, entity_id, list(audiences), limit)
            except Exception as exc:
                span.set_attribute("error", True)
                _log.warning("kb_store.postgres.linked_to_failed",
                             error=str(exc)[:200])
                increment(_METRIC_PG_ERRORS,
                          store="kb_store", op="linked_to",
                          reason=type(exc).__name__)
                raise OneOpsError(
                    "kb_store.postgres: linked_to failed", cause=exc) from exc
            span.set_attribute(_ATTR_ROW_COUNT, len(rows))
            return [self._row_to_dict(r) for r in rows]

_store: KbStore | None = None


def _build_default() -> KbStore:
    backend = os.getenv("ONEOPS_KB_BACKEND", "memory").strip().lower()
    if backend == "postgres":
        _log.info("kb_store.backend_selected", backend="postgres")
        return PostgresKbStore()
    _log.info("kb_store.backend_selected", backend="memory")
    return InMemoryKbStore()


def get_kb_store() -> KbStore:
    """The process-wide KB store. In-memory unless `ONEOPS_KB_BACKEND` selects
    the live backend."""
    global _store
    if _store is None:
        _store = _build_default()
    return _store


def set_kb_store(store: KbStore) -> None:
    """Replace the process-wide store — used by tests and FaaS wiring."""
    global _store
    _store = store


__all__ = [
    "KbStore",
    "InMemoryKbStore",
    "PostgresKbStore",
    "get_kb_store",
    "set_kb_store",
]
