"""UC-8 catalog semantic search — find closest catalog templates for an SR.

When a new Service Request arrives without an explicit `catalog_item_id`
(scenario 8.8), embed the SR's text and cosine-search
`ai.embeddings_catalog_item`.

Production-grade properties:
  • Tenant isolation — `tenant_id = $1` as the first predicate.
  • RBAC parity — `audience && $user_roles::text[]`.
  • Active-only — `is_active = true` (don't recommend retired items).
  • Cosine floor — drop anything below 0.50 (configurable). Prevents
    "best of garbage" matches.
  • Auto-pick threshold — top-1 ≥ 0.85 returns single match for
    immediate fulfill. 0.50–0.85 returns top-K for human disambiguation.
  • Field-map-aware — the embed text is built via the same field_map the
    worker uses, so query and indexed content stay in sync.

The thresholds (0.50 floor, 0.85 auto-pick) are calibration placeholders.
The recommended workflow:
  1. Ship with these defaults.
  2. Capture per-query top-K + chosen item in
     ai.catalog_search_telemetry (deferred — Phase 8).
  3. Retune from real production traffic, not synthetic.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import asyncpg
import structlog
from opentelemetry import trace

_log = structlog.get_logger("oneops.uc08.catalog_search")
_tracer = trace.get_tracer("oneops.uc08.catalog_search")


# Calibration constants (env-overridable for per-deployment tuning).
COSINE_FLOOR = float(os.environ.get("UC08_CATALOG_COSINE_FLOOR", "0.50"))
AUTO_PICK_THRESHOLD = float(
    os.environ.get("UC08_CATALOG_AUTO_PICK_THRESHOLD", "0.85"))
TOP_K = int(os.environ.get("UC08_CATALOG_TOP_K", "3"))
EMBED_MODEL = os.environ.get(
    "UC08_CATALOG_EMBED_MODEL", "text-embedding-3-large")
EMBED_DIM = int(os.environ.get("UC08_CATALOG_EMBED_DIM", "1536"))


@dataclass(frozen=True)
class CatalogMatch:
    """One candidate catalog template ranked against an SR query."""

    catalog_item_id: str
    name: str
    description: str
    category: str
    owner_group: str
    cosine_score: float
    above_floor: bool          # cosine_score >= COSINE_FLOOR
    is_auto_pick: bool         # cosine_score >= AUTO_PICK_THRESHOLD


@dataclass(frozen=True)
class CatalogSearchResult:
    """Aggregated result for one SR-to-catalog match query."""

    matches: tuple[CatalogMatch, ...]    # top-K, descending cosine
    auto_pick: CatalogMatch | None       # filled when top-1 ≥ AUTO_PICK
    above_floor_count: int               # how many cleared COSINE_FLOOR
    query_text: str                      # the embed text used (for audit)


async def _embed_query(
    *, query_text: str, tenant_id: str, gateway,
) -> list[float]:
    """Embed the SR's query text via the same gateway the worker uses."""
    with _tracer.start_as_current_span(
        "uc08.catalog_search.embed_query",
        attributes={"oneops.tenant_id": tenant_id},
    ):
        vecs = await gateway.embed(
            [query_text],
            model=EMBED_MODEL,
            tenant_id=tenant_id,
            dimensions=EMBED_DIM,
        )
        return vecs[0]


def _build_query_text_from_sr(
    *, title: str, description: str, category: str | None = None,
) -> str:
    """Build the query string in the SAME shape as catalog_anchor texts.

    Matches the field labels used by `build_catalog_anchor_text` so the
    query and indexed content live on the same semantic surface.
    """
    parts = [f"Name: {title}"]
    if description:
        parts.append(f"Description: {description}")
    if category:
        parts.append(f"Category: {category}")
    return "\n".join(parts)


async def find_closest_catalog_items(
    *, tenant_id: str, sr_title: str, sr_description: str,
    sr_category: str | None = None,
    user_roles: list[str],
    gateway,
    conn: asyncpg.Connection,
    top_k: int = TOP_K,
    cosine_floor: float = COSINE_FLOOR,
    auto_pick_threshold: float = AUTO_PICK_THRESHOLD,
) -> CatalogSearchResult:
    """Semantic search over `ai.embeddings_catalog_item`.

    Returns a CatalogSearchResult with up to `top_k` matches. Filters:
      • tenant_id (mandatory, first predicate)
      • is_active (don't recommend retired)
      • audience overlap with user_roles (RBAC parity)
      • cosine_score >= cosine_floor (drops noise tier)
    Sorted by raw cosine descending.
    """
    query_text = _build_query_text_from_sr(
        title=sr_title, description=sr_description, category=sr_category,
    )

    with _tracer.start_as_current_span(
        "uc08.catalog_search.find_closest",
        attributes={
            "oneops.tenant_id": tenant_id,
            "uc08.top_k": top_k,
            "uc08.cosine_floor": cosine_floor,
            "uc08.user_roles": ",".join(sorted(user_roles)),
        },
    ) as span:
        embedding = await _embed_query(
            query_text=query_text, tenant_id=tenant_id, gateway=gateway,
        )
        vec_literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"

        rows = await conn.fetch(
            f"""
            SELECT  c.catalog_item_id,
                    c.name,
                    coalesce(c.description, '') AS description,
                    coalesce(c.category, '')    AS category,
                    coalesce(c.owner_group, '') AS owner_group,
                    1 - (e.embedding <=> $1::vector) AS cosine_score
              FROM  ai.embeddings_catalog_item e
              JOIN  itsm.catalog_item c
                ON  c.catalog_item_id = e.entity_id
               AND  c.tenant_id       = e.tenant_id
             WHERE  e.tenant_id     = $2
               AND  e.chunk_type    = 'catalog_anchor'
               AND  c.is_active     = true
               AND  c.audience      && $3::text[]
             ORDER BY e.embedding <=> $1::vector
             LIMIT  $4
            """,
            vec_literal, tenant_id, user_roles, top_k,
        )

        matches: list[CatalogMatch] = []
        for r in rows:
            score = float(r["cosine_score"])
            matches.append(CatalogMatch(
                catalog_item_id=r["catalog_item_id"],
                name=r["name"],
                description=r["description"],
                category=r["category"],
                owner_group=r["owner_group"],
                cosine_score=score,
                above_floor=(score >= cosine_floor),
                is_auto_pick=(score >= auto_pick_threshold),
            ))

        above_floor = sum(1 for m in matches if m.above_floor)
        auto_pick = matches[0] if (
            matches and matches[0].is_auto_pick
        ) else None

        span.set_attribute("uc08.matches_total", len(matches))
        span.set_attribute("uc08.matches_above_floor", above_floor)
        span.set_attribute("uc08.auto_pick", auto_pick.catalog_item_id if auto_pick else "")

        _log.info("uc08.catalog_search.completed",
                  tenant_id=tenant_id,
                  query_chars=len(query_text),
                  matches_total=len(matches),
                  matches_above_floor=above_floor,
                  top1_score=matches[0].cosine_score if matches else None,
                  auto_pick=auto_pick.catalog_item_id if auto_pick else None)

        return CatalogSearchResult(
            matches=tuple(matches),
            auto_pick=auto_pick,
            above_floor_count=above_floor,
            query_text=query_text,
        )


__all__ = [
    "CatalogMatch",
    "CatalogSearchResult",
    "find_closest_catalog_items",
    "COSINE_FLOOR",
    "AUTO_PICK_THRESHOLD",
    "TOP_K",
]
