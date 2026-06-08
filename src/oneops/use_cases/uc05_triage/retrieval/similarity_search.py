"""Hybrid retrieval engine for UC-5 Triage.

Patterns proven by UC-3 (locked in memory project_poc5mw1_session_2026_05_27):
  • Parallel FTS + vector retrieval via asyncio.gather
  • RRF rank-only fusion with K=60 (Vespa default) — sidesteps the
    cosine-vs-tsrank score-normalisation problem
  • Minimum fused-score gate — drops low-confidence noise without ever
    forcing the result set to zero
  • Degraded mode — embedding failure → semantic branch returns []; the
    pipeline keeps running on FTS alone instead of erroring out
  • Per-side limit (PER_SIDE) so one branch can't dominate the fusion

UC-5 additions on top of UC-3's pattern:
  • Schema-driven SQL via schema_loader (incident vs request dispatch
    happens through service-schema.json's retrieval_schema block — no
    hardcoded column lists, no separate SQL files per type)
  • Hard pre-filters baked into both branches: same-tenant, same-type
    (single table), status whitelist, age cap
  • Rerank signals on top of the fused score:
      same primary CI       → +0.10
      same service_name     → +0.05  (incident only — request lacks it)
      recency decay         → up to +0.05 within age_filter_days window
  • Threshold gate (default 0.85) — Tool 1 emits a duplicate verdict
    only when the top rerank score clears it. UC-2 will reuse this
    engine with a lower threshold when it ships (its own copy under
    uc02_similar_tickets/, per the isolation rule).

OTel spans:
  uc05.retrieval.embed                LLM embed call (via gateway)
  uc05.retrieval.fts                  FTS branch
  uc05.retrieval.vector               Vector branch
  uc05.retrieval.fuse_rerank          RRF + rerank
"""
from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from oneops.use_cases.uc05_triage.contracts import ScoredNeighbour
from oneops.use_cases.uc05_triage.retrieval.schema_loader import (
    load_retrieval_schema,
)

# ── Tunables (same shape as UC-3) ─────────────────────────────────────────────

PER_SIDE_LIMIT = 20
"""Max rows returned from each branch (FTS / vector) before fusion."""

RRF_K = 60
"""Reciprocal Rank Fusion constant (Vespa default — same as UC-3)."""

MIN_FUSED_SCORE = 0.012
"""Calibrated by UC-3 in production. Below this = noise; drop pre-rerank."""

DEFAULT_DUPLICATE_THRESHOLD = 0.85
"""Rerank-score floor for emitting a 'duplicate' verdict."""

# Rerank weights — additive boosts on top of the fused base score.
SAME_CI_BOOST = 0.10
SAME_SERVICE_BOOST = 0.05  # incident only
RECENCY_MAX_BOOST = 0.05

# ── Connection protocol — keeps the engine swappable in tests ─────────────────

class _Connection(Protocol):
    async def fetch(self, query: str, *args: Any) -> list[Mapping[str, Any]]: ...


EmbedFn = Callable[..., Awaitable[list[float]]]
"""(text: str, *, tenant_id: str, user_id: str = "") -> list[float]"""


# ── SQL builders ─────────────────────────────────────────────────────────────

def _build_fts_sql(schema: dict[str, Any]) -> str:
    cols = ", ".join(schema["neighbour_columns"])
    return (
        f"SELECT {schema['id_column']} AS id, {cols}, "
        f"ts_rank_cd({schema['tsv_column']}, "
        f"  plainto_tsquery('english', $1)) AS fts_score "
        f"FROM {schema['table']} "
        f"WHERE tenant_id = $2 "
        f"  AND status = ANY($3::text[]) "
        f"  AND created_at > now() - make_interval(days => $4)"
        f"ORDER BY fts_score DESC "
        f"LIMIT $5"
    )


def _build_vector_sql(schema: dict[str, Any]) -> str:
    """Vector-search SQL over `ai.embeddings_<service>` (chunk_type=
    'symptom_anchor'), JOIN-ed to the source table to hydrate the neighbour
    columns (title/category/CI…) the reranker needs. The HNSW ORDER BY runs on
    the narrow embeddings table; the source join is lookup-by-PK."""
    src = schema["table"]
    idc = schema["id_column"]
    emb_tbl = schema["embedding_table_v2"]
    chunk = schema["embedding_chunk_type"]
    cols_qualified = ", ".join(f"i.{c}" for c in schema["neighbour_columns"])
    return (
        f"SELECT i.{idc} AS id, {cols_qualified}, "
        f"1 - (e.embedding <=> $1::vector) AS vec_score "
        f"FROM {emb_tbl} e "
        f"JOIN {src} i "
        f"  ON i.{idc} = e.entity_id AND i.tenant_id = e.tenant_id "
        f"WHERE e.tenant_id = $2 "
        f"  AND e.chunk_type = '{chunk}' "
        f"  AND i.status = ANY($3::text[]) "
        f"  AND i.created_at > now() - make_interval(days => $4) "
        f"ORDER BY e.embedding <=> $1::vector "
        f"LIMIT $5"
    )


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


# ── RRF + rerank ─────────────────────────────────────────────────────────────

def _fuse_rrf(
    fts_rows: list[Mapping[str, Any]],
    vec_rows: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion — same shape as UC-3's `_fused_score` accumulator.
    Result rows carry every field from whichever branch surfaced them, plus
    `_fts_score`, `_vec_score`, `_fused_score`, `_sources`.
    """
    fused: dict[str, dict[str, Any]] = {}
    for src_name, rows in (("fts", fts_rows), ("vec", vec_rows)):
        for rank, row in enumerate(rows, start=1):
            rid = row.get("id")
            if rid is None:
                continue
            slot = fused.setdefault(rid, dict(row))
            slot.setdefault("_fused_score", 0.0)
            slot["_fused_score"] += 1.0 / (RRF_K + rank)
            slot.setdefault("_sources", []).append(src_name)
            if src_name == "fts" and "fts_score" in row:
                slot["_fts_score"] = float(row["fts_score"])
            if src_name == "vec" and "vec_score" in row:
                slot["_vec_score"] = float(row["vec_score"])
    return sorted(
        fused.values(),
        key=lambda d: d.get("_fused_score", 0.0),
        reverse=True,
    )


def _rerank(
    fused_rows: list[dict[str, Any]],
    *,
    probe_ci_id: str | None,
    probe_service_name: str | None,
    age_filter_days: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Apply additive rerank boosts. Score is clamped to [0, 1].

    same_ci      → +0.10 if probe_ci_id is set and matches the row's ci_id
    same_service → +0.05 if probe_service_name matches the row's service_name
                     (incident only — request rows won't have it)
    recency      → up to +0.05, linearly decaying from 'now' to age_filter_days
    """
    n = now or datetime.now(UTC)
    window_seconds = max(1, age_filter_days * 86400)
    for row in fused_rows:
        _rerank_one(row, probe_ci_id=probe_ci_id,
                    probe_service_name=probe_service_name,
                    now=n, window_seconds=window_seconds)
    return sorted(
        fused_rows,
        key=lambda d: d.get("_rerank_score", 0.0),
        reverse=True,
    )


def _rerank_one(
    row: dict[str, Any], *, probe_ci_id: str | None,
    probe_service_name: str | None, now: datetime, window_seconds: int,
) -> None:
    """Compute the additive rerank boost for one row and write `_rerank_score`
    (logistic-squashed into [0,1]) + `_rerank_basis` in place."""
    base = float(row.get("_fused_score") or 0.0)
    boost = 0.0
    rationale: list[str] = []
    if probe_ci_id and row.get("ci_id") == probe_ci_id:
        boost += SAME_CI_BOOST
        rationale.append("same_ci")
    if probe_service_name and row.get("service_name") == probe_service_name:
        boost += SAME_SERVICE_BOOST
        rationale.append("same_service")
    created = row.get("created_at")
    if isinstance(created, datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age_s = max(0.0, (now - created).total_seconds())
        recency = max(0.0, 1.0 - (age_s / window_seconds))
        boost += RECENCY_MAX_BOOST * recency
        if recency > 0.0:
            rationale.append(f"recency={recency:.2f}")
    score = base + boost
    # Normalise into [0, 1]. The fused base is small (UC-3 saw 0.012-0.05);
    # rerank boost dominates the absolute value, so a logistic squash gives a
    # stable, interpretable comparable across runs.
    row["_rerank_score"] = 1.0 / (1.0 + math.exp(-12.0 * (score - 0.05)))
    row["_rerank_basis"] = rationale


# ── Public entry ─────────────────────────────────────────────────────────────

async def search_similar(
    conn: _Connection,
    *,
    service_id: str,
    tenant_id: str,
    probe_text: str,
    embed_fn: EmbedFn,
    duplicate_threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
    max_candidates: int = 10,
    probe_ci_id: str | None = None,
    probe_service_name: str | None = None,
    user_id: str = "",
    now: datetime | None = None,
) -> tuple[list[ScoredNeighbour], ScoredNeighbour | None]:
    """Return (top-K candidates, top match if >= threshold else None).

    The top match is the duplicate verdict carrier. Tool 1 uses it to emit
    `duplicate_verdict='duplicate'` when present.

    Degraded-mode contract (matches UC-3): if embed_fn fails the vector
    branch returns []; FTS still runs; fusion + rerank still work.
    """
    schema = load_retrieval_schema(service_id)
    fts_sql = _build_fts_sql(schema)
    vec_sql = _build_vector_sql(schema)

    # Embed once. Same vector serves the vector branch.
    probe_vec: list[float] = []
    if embed_fn is not None and probe_text.strip():
        try:
            probe_vec = await embed_fn(probe_text, tenant_id=tenant_id, user_id=user_id)
        except Exception:
            probe_vec = []

    status_filter = schema["status_filter"]
    age_days = schema["age_filter_days"]

    # Run FTS + vector sequentially on a single connection (asyncpg forbids
    # concurrent ops on one Connection). Callers wanting full parallelism
    # pass a pool-acquired pair via a higher-level helper; the perf gap is
    # ~30-50ms at PER_SIDE_LIMIT=20 which is acceptable for triage UX.
    fts_rows: list[Mapping[str, Any]] = await conn.fetch(
        fts_sql, probe_text, tenant_id, status_filter, age_days, PER_SIDE_LIMIT,
    )
    if probe_vec:
        # pgvector HNSW filter-hardening — prevents silent under-recall on
        # narrow filters (small tenant, tight status set, narrow age window).
        # Shared with UC-2; see `oneops.db.pgvector_hnsw`.
        from oneops.db.pgvector_hnsw import apply_hardening as _hnsw_harden
        await _hnsw_harden(conn)
        vec_rows: list[Mapping[str, Any]] = await conn.fetch(
            vec_sql, _vec_literal(probe_vec), tenant_id, status_filter,
            age_days, PER_SIDE_LIMIT,
        )
    else:
        vec_rows = []

    fused = _fuse_rrf(fts_rows, vec_rows)
    fused = [r for r in fused if r.get("_fused_score", 0.0) >= MIN_FUSED_SCORE]
    reranked = _rerank(
        fused,
        probe_ci_id=probe_ci_id,
        probe_service_name=probe_service_name,
        age_filter_days=age_days,
        now=now,
    )

    # Fix 2 (2026-05-29 PM): dedup near-identical titles BEFORE truncating to
    # max_candidates. Stress-test rows like INC9010054 / INC9010001 that
    # share an identical title pollute kNN voting — we drop duplicates by
    # lowercase title prefix hash, keeping the highest-fused-score copy.
    deduped = _dedup_by_title(reranked)
    top_k_rows = deduped[:max_candidates]
    candidates: list[ScoredNeighbour] = [_to_scored(r) for r in top_k_rows]
    top_match: ScoredNeighbour | None = (
        candidates[0]
        if candidates and candidates[0].fused_score >= duplicate_threshold
        else None
    )
    return candidates, top_match


_TITLE_DEDUP_PREFIX_CHARS = 60
"""Compare the first N chars of the lowercased title (after stripping
trailing '(NNNNN)' serial-number suffix). Catches stress-test fixtures
that copy-paste the same title with a serial number tag."""


def _normalise_title_for_dedup(title: str) -> str:
    """Strip trailing '(<digits>)' suffix using pure string operations.

    No regex (production-hygiene rule, 2026-05-29): regex performance is
    unpredictable on adversarial input. Plain str ops are O(n) bounded.
    Examples normalised:
      'Office Wi-Fi unreachable on one floor (9010054)' -> '...one floor'
      'VPN drops (123)'                                  -> 'vpn drops'
      'VPN drops'                                        -> 'vpn drops'
      'VPN drops (abc)'                                  -> 'vpn drops (abc)'  (kept — not pure digits)
    """
    s = title.strip().lower()
    if s.endswith(")"):
        open_idx = s.rfind("(")
        if open_idx > 0:
            inside = s[open_idx + 1 : -1]
            if inside and inside.isdigit():
                s = s[:open_idx].rstrip()
    return s


def _dedup_by_title(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop near-identical-title duplicates, keeping the highest-fused-score
    row per group. Input is assumed already sorted by _rerank_score desc.
    Preserves order of survivors.

    Dedup key strips a trailing '(NNNNN)' serial — so
    'Office Wi-Fi unreachable on one floor (9010054)' and
    'Office Wi-Fi unreachable on one floor (9010001)' collapse to one row.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        title = str(row.get("title") or "")
        if not title.strip():
            out.append(row)
            continue
        key = _normalise_title_for_dedup(title)[:_TITLE_DEDUP_PREFIX_CHARS]
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _to_scored(row: Mapping[str, Any]) -> ScoredNeighbour:
    """Convert a fused/reranked row dict to a ScoredNeighbour. All non-private
    columns flow into `fields` so downstream aggregation is schema-driven.
    """
    fields = {
        k: v
        for k, v in row.items()
        if not k.startswith("_") and k not in ("id", "fts_score", "vec_score")
    }
    return ScoredNeighbour(
        id=str(row.get("id")),
        fields=fields,
        vec_score=float(row.get("_vec_score") or row.get("vec_score") or 0.0),
        fts_score=float(row.get("_fts_score") or row.get("fts_score") or 0.0),
        fused_score=float(row.get("_rerank_score") or row.get("_fused_score") or 0.0),
    )
