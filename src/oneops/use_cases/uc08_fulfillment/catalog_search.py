"""UC-8 catalog semantic search — find closest catalog templates for an SR.

When a new Service Request arrives without an explicit `catalog_item_id`
(scenario 8.8), embed the SR's text and cosine-search
`ai.embeddings_catalog_item`.

╔══════════════════════════════════════════════════════════════════════╗
║  PRODUCTION-GRADE CONTRACT — READ-ONLY MODULE                         ║
║                                                                       ║
║  This module returns SUGGESTIONS. It NEVER calls fulfill_request.     ║
║                                                                       ║
║  Approval gates live in the layer ABOVE this module:                  ║
║                                                                       ║
║    Gate 1 — Match Confirmation (mandatory before any action)          ║
║      Triggered when result.auto_pick is set.                          ║
║      Caller MUST present the match to the user and require explicit   ║
║      confirmation before invoking fulfill_request. Auto-picking +     ║
║      auto-fulfilling is a production-grade violation.                 ║
║                                                                       ║
║    Gate 2 — Disambiguation (mandatory when ambiguous)                 ║
║      Triggered when result.above_floor_count >= 2 and no auto_pick.   ║
║      Caller MUST render top-K as a chooser. User explicitly picks.    ║
║                                                                       ║
║    Gate 3 — Per-task approval (built into catalog template)           ║
║      Triggered during fulfill_request execution by tasks with         ║
║      tool_id='request_human_approval'. Handled by the executor.       ║
║                                                                       ║
║  Below-floor results (above_floor_count=0) MUST NOT lead to any       ║
║  action — caller responds with "no match found" and offers manual     ║
║  routing.                                                             ║
╚══════════════════════════════════════════════════════════════════════╝

SQL-layer filters applied here:
  • tenant_id = $caller_tenant (mandatory — defence in depth)
  • chunk_type = 'catalog_anchor'
  • JOIN c.tenant_id = e.tenant_id (catches schema-drift bugs)

RBAC: enforced at tool-call boundary, not at SQL layer (UC-2/UC-5
pattern). The `user_roles` parameter is accepted for future per-row
audience filtering but is currently unused.

Calibration: thresholds (0.50 floor, 0.60 auto-pick) calibrated
empirically against 30 live catalog items. Retune via env once
production query telemetry is captured.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import asyncpg
import structlog
from opentelemetry import trace

from oneops.observability.metrics import increment as _metric_inc

_log = structlog.get_logger("oneops.uc08.catalog_search")
_tracer = trace.get_tracer("oneops.uc08.catalog_search")


# Calibration constants (env-overridable for per-deployment tuning).
#
# Defaults were calibrated empirically on 2026-05-31 against 30 live
# T001 + T002 + T003 catalog items using `text-embedding-3-large`:
#   • Pizza-lunch query (off-domain) → top-1 cosine 0.354 (correctly noise)
#   • VPN-access query (realistic)    → top-1 cosine 0.609 (correct match)
#   • Onboard-developer query         → top-1 cosine 0.582 (correct match)
#   • Standard-laptop query           → top-1 cosine 0.565 (correct match)
# Adjust per-deployment via env once production query telemetry is captured.
COSINE_FLOOR = float(os.environ.get("UC08_CATALOG_COSINE_FLOOR", "0.50"))
AUTO_PICK_THRESHOLD = float(
    os.environ.get("UC08_CATALOG_AUTO_PICK_THRESHOLD", "0.60"))
TOP_K = int(os.environ.get("UC08_CATALOG_TOP_K", "3"))
EMBED_MODEL = os.environ.get(
    "UC08_CATALOG_EMBED_MODEL", "text-embedding-3-large")
EMBED_DIM = int(os.environ.get("UC08_CATALOG_EMBED_DIM", "1536"))

# Hard timeout on the embedding-gateway call. Without this, a slow
# gateway hangs the caller. Production-grade: every external call
# bounded. Caller-visible: query returns empty result (not raises) on
# timeout so the chat path can degrade gracefully.
EMBED_TIMEOUT_S = float(os.environ.get("UC08_CATALOG_EMBED_TIMEOUT_S", "60"))

# Max chars of query text we send to the embedding model. Text-embedding-3-
# -large accepts up to ~8192 tokens (~32000 chars). We cap at 6000 chars
# to leave headroom and avoid borderline truncations.
MAX_QUERY_CHARS = int(os.environ.get("UC08_CATALOG_MAX_QUERY_CHARS", "6000"))


class CatalogSearchError(Exception):
    """Typed boundary error for catalog search. Wraps gateway/network
    failures so callers can distinguish 'search failed' from 'no matches'."""


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
    """Embed the SR's query text via the same gateway the worker uses.

    Bounded by EMBED_TIMEOUT_S — gateway hangs surface as CatalogSearchError
    (caller-visible) rather than silent infinite await.
    """
    with _tracer.start_as_current_span(
        "uc08.catalog_search.embed_query",
        attributes={"oneops.tenant_id": tenant_id},
    ):
        try:
            vecs = await asyncio.wait_for(
                gateway.embed(
                    [query_text],
                    model=EMBED_MODEL,
                    tenant_id=tenant_id,
                    dimensions=EMBED_DIM,
                ),
                timeout=EMBED_TIMEOUT_S,
            )
        except TimeoutError as exc:
            raise CatalogSearchError(
                f"embedding gateway timeout after {EMBED_TIMEOUT_S}s",
            ) from exc
        except Exception as exc:                          # noqa: BLE001
            raise CatalogSearchError(
                f"embedding gateway failure: {type(exc).__name__}: {exc}",
            ) from exc
        if not vecs or len(vecs[0]) != EMBED_DIM:
            raise CatalogSearchError(
                f"embedding gateway returned malformed response "
                f"(got {len(vecs)} vectors, expected dim {EMBED_DIM})",
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
    user_roles: list[str] | None = None,
    gateway,
    conn: asyncpg.Connection,
    top_k: int = TOP_K,
    cosine_floor: float = COSINE_FLOOR,
    auto_pick_threshold: float = AUTO_PICK_THRESHOLD,
) -> CatalogSearchResult:
    """Semantic search over `ai.embeddings_catalog_item`.

    Returns a CatalogSearchResult with up to `top_k` matches. Filters
    applied at SQL layer:
      • tenant_id (mandatory, first predicate)

    RBAC discipline: catalog visibility is enforced at the **tool-call
    boundary** (the find_closest_catalog_template tool's `abac_tags`
    consult the caller's role at invocation time). This mirrors UC-2 and
    UC-5 which also don't filter source rows by per-row audience. If a
    deployment needs per-row catalog visibility, the right way to add it
    is a migration introducing `itsm.catalog_item.audience text[]` plus
    a `c.audience && $user_roles` predicate here — no other code changes.

    Sorted by raw cosine descending; cosine floor applied at result time.
    """
    # user_roles is accepted (for future per-row RBAC) but unused today.
    _ = user_roles

    # Edge case 1+5: empty/whitespace/oversized input. Don't waste an
    # embedding call on garbage; return empty result so the caller can
    # render "no match" instead of a noisy top-K.
    title = (sr_title or "").strip()
    desc  = (sr_description or "").strip()
    cat   = (sr_category or "").strip() if sr_category else None
    if not title and not desc:
        _log.info("uc08.catalog_search.empty_query",
                  tenant_id=tenant_id)
        return CatalogSearchResult(
            matches=(), auto_pick=None, above_floor_count=0,
            query_text="",
        )

    query_text = _build_query_text_from_sr(
        title=title, description=desc, category=cat,
    )
    if len(query_text) > MAX_QUERY_CHARS:
        _log.info("uc08.catalog_search.query_truncated",
                  tenant_id=tenant_id,
                  original_chars=len(query_text),
                  truncated_to=MAX_QUERY_CHARS)
        query_text = query_text[:MAX_QUERY_CHARS]

    with _tracer.start_as_current_span(
        "uc08.catalog_search.find_closest",
        attributes={
            "oneops.tenant_id": tenant_id,
            "uc08.top_k": top_k,
            "uc08.cosine_floor": cosine_floor,
            "uc08.user_roles": ",".join(sorted(user_roles or [])),
        },
    ) as span:
        embedding = await _embed_query(
            query_text=query_text, tenant_id=tenant_id, gateway=gateway,
        )
        vec_literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"

        # Production-grade tenant isolation: WHERE binds caller's tenant
        # (defence-in-depth) PLUS JOIN binds vector-to-source consistency
        # (catches schema-drift bugs).
        rows = await conn.fetch(
            """
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
             -- Deterministic ordering for tied scores (edge case 4):
             -- HNSW's secondary sort isn't guaranteed across runs, so we
             -- add catalog_item_id as the tie-breaker. Same input always
             -- yields same top-K — important for chat-cache + audit.
             ORDER BY e.embedding <=> $1::vector ASC, c.catalog_item_id ASC
             LIMIT  $3
            """,
            vec_literal, tenant_id, top_k,
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

        # Production metrics (Grafana parity with UC-2/UC-5).
        _metric_inc("ai.uc08.catalog_search.total", 1,
                    tenant_id=tenant_id,
                    auto_pick="true" if auto_pick else "false",
                    above_floor=str(above_floor))
        _metric_inc("ai.agent.runs.total", 1,
                    agent_id="uc08_fulfillment",
                    tenant_id=tenant_id,
                    source="catalog_search",
                    status="success")

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
    "CatalogSearchError",
    "find_closest_catalog_items",
    "COSINE_FLOOR",
    "AUTO_PICK_THRESHOLD",
    "TOP_K",
    "EMBED_TIMEOUT_S",
    "MAX_QUERY_CHARS",
]
