"""UC-2 Similar Tickets — `find_similar()` core.

The ONLY place that touches `ai.embeddings_<service>` for UC-2 retrieval.
Button route and chat handler BOTH call this function (via the NATS dispatcher
in production, in-process in dev) so their results cannot diverge.

Pipeline (spec §UC-2):

  1. Anchor lookup       SELECT embedding FROM ai.embeddings_<svc>
                          WHERE tenant_id=$1 AND entity_id=$2
                            AND chunk_type='symptom_anchor'
  2. ANN retrieval        cosine on symptom_anchor, HNSW, over-fetch 3×k
                          + tenant pre-filter + time-window + scope filters
  3. RBAC + metadata JOIN one JOIN to itsm.<svc> on the candidate id-set
  4. Re-rank composite    final = 0.60·sem + 0.25·metadata + 0.15·recency
                                  (spec-mandated weights)
  5. Diagnosis confirm    optional: cosine on diagnosis_trail for top-K;
                          confirmed pairs get +0.05 nudge and `diagnosis_match`
                          in `why_similar`.
  6. Flags + message      likely_duplicate / resolution_available per spec;
                          "no significantly similar tickets found" when empty.

Production discipline:
  • OTel span at every stage with operator-readable attributes.
  • No silent failures — DB errors surface as RuntimeError; the dispatcher /
    route layer translates them to HTTP/NATS error envelopes.
  • Pure async; no blocking calls.
  • No LLM calls inside this function (UC-2 v1 is pure retrieval + math).
  • RBAC is enforced inside the SQL — `RBACFilter` returns a SQL predicate.
"""
from __future__ import annotations

import math
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import asyncpg

from oneops.db.pgvector_hnsw import apply_hardening as _apply_hnsw_hardening
from oneops.observability import get_logger, get_tracer
from oneops.observability.metrics import increment as _metric_inc
from oneops.uc_common import TimeFilter
from oneops.use_cases.uc02_similar_tickets.contracts import (
    PreferStatus,
    ServiceId,
    SimilarFlag,
    SimilarTicket,
    SimilarTicketsResponse,
)

_log = get_logger("oneops.uc02.core")
_tracer = get_tracer("oneops.uc02")

# Spec §UC-2 composite weights — single source of truth.
_W_SEMANTIC = 0.60
_W_METADATA = 0.25
_W_RECENCY = 0.15

# Semantic-confidence gate on the metadata contribution (2026-05-31).
# Closes the failure mode where a candidate with weak text similarity
# (different problem class) climbs to #1 purely on `same_ci` + `same_group`
# because the source ticket itself is low-signal ("Change calendar not
# loading"). We scale the metadata + recency contributions by a factor
# proportional to the semantic score until it reaches `_SEM_FULL_TRUST`.
# Below `_SEM_FLOOR`, metadata is effectively muted; in between it ramps
# linearly. Behaviour above the trust line is unchanged — strong semantic
# matches still benefit from the full metadata + recency boost. The
# semantic component itself is never gated; we trust the embedder.
_SEM_FLOOR = 0.45
_SEM_FULL_TRUST = 0.75

# Spec §UC-2 flag thresholds.
_DUP_SIM = 0.90
_RES_SIM = 0.85

# Implementation tunables (env-overridable, no hard-codes per §2.1).
_OVERFETCH = int(os.getenv("UC02_OVERFETCH_MULTIPLIER", "3"))
_DIAG_BOOST = float(os.getenv("UC02_DIAG_CONFIRM_BOOST", "0.05"))
_DIAG_THRESHOLD = float(os.getenv("UC02_DIAG_CONFIRM_THRESHOLD", "0.70"))
_RECENCY_LAMBDA = float(os.getenv("UC02_RECENCY_LAMBDA", "0.10"))
_LIMITED_CTX_CHARS = int(os.getenv("UC02_LIMITED_CTX_CHARS", "40"))

# pgvector HNSW filter hardening lives in `oneops.db.pgvector_hnsw` — shared
# across UC-2, UC-5, and any future filtered-ANN UC. Configured via
# ONEOPS_HNSW_* env vars; see that module's docstring.

# Per-service literals.
_EMB_TABLE: dict[ServiceId, str] = {
    "incident": "ai.embeddings_incident",
    "request":  "ai.embeddings_request",
}
_BASE_TABLE: dict[ServiceId, str] = {
    "incident": "itsm.incident",
    "request":  "itsm.request",
}
_ID_COL: dict[ServiceId, str] = {
    "incident": "incident_id",
    "request":  "request_id",
}
_OPENED_COL: dict[ServiceId, str] = {
    "incident": "created_at",
    "request":  "created_at",
}
_RESOLVED_COL: dict[ServiceId, str] = {
    "incident": "resolved_at",
    "request":  "fulfilled_at",
}


# ── RBAC predicate hook ──────────────────────────────────────────────────────
#
# UC-2 supports SQL-time RBAC: the caller supplies a tuple
#   (predicate_sql, predicate_args)
# that is AND-ed into the metadata JOIN. The chat/button entry points build
# this from `role-permission-registry` so the predicate is uniform across
# both. The "no predicate" case is OK for service-desk-grade roles that can
# see all tickets in a tenant; end-user roles MUST supply a predicate that
# binds to their user_id.

RbacPredicate = tuple[str, list[Any]]


def _default_rbac(role: str, user_id: str, service_id: ServiceId) -> RbacPredicate:
    """Conservative defaults used when no resolver is wired.

    - end_user / requester_only: restricted to tickets they reported.
    - All other roles: no extra predicate (tenant + table-level RBAC apply).

    Routes / handlers SHOULD pass an explicit predicate from the
    role-permission registry; this default exists so the core never silently
    leaks data when the wiring isn't complete.
    """
    role_l = (role or "").strip().lower()
    if role_l in ("end_user", "requester", "requester_only"):
        col = "reported_by" if service_id == "incident" else "requested_by"
        return (f"{col} = $%d", [user_id])
    return ("TRUE", [])


# ── helpers ──────────────────────────────────────────────────────────────────

def _recency_decay(opened_at: datetime | None, now: datetime) -> float:
    """Log-decay in [0, 1]. 1 for 'today', → 0 as age grows.

    Old high-cosine matches still survive (small denominator), and fresh ones
    win when semantic scores tie. λ is env-tunable for live calibration.
    """
    if opened_at is None:
        return 0.0
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=UTC)
    age_days = max(0.0, (now - opened_at).total_seconds() / 86400.0)
    return 1.0 / (1.0 + _RECENCY_LAMBDA * math.log(age_days + 1.0))


def _vec_literal(vec: list[float]) -> str:
    """pgvector accepts '[1.0,2.0,...]' literal — safer than asyncpg binding."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _metadata_signal(*, source: dict[str, Any], cand: dict[str, Any]) -> tuple[float, list[str]]:
    """Spec §UC-2 metadata overlap component (weight 0.25 of composite).

    Decomposed into observable, additive sub-signals so `why_similar` carries
    real provenance instead of a single opaque number.
    Per signal weights sum to 1.0 to fit cleanly under the 0.25 composite cap.
    """
    score = 0.0
    why: list[str] = []
    if source.get("ci_id") and source.get("ci_id") == cand.get("ci_id"):
        score += 0.45
        why.append("same_ci")
    if source.get("category") and source.get("category") == cand.get("category"):
        score += 0.25
        why.append("same_category")
    if source.get("service_name") and source.get("service_name") == cand.get("service_name"):
        score += 0.15
        why.append("same_service")
    if source.get("assignment_group") and source.get("assignment_group") == cand.get("assignment_group"):
        score += 0.15
        why.append("same_group")
    return min(score, 1.0), why


def _flag_for(*, sem: float, source: dict[str, Any], cand: dict[str, Any]) -> SimilarFlag | None:
    """Spec §UC-2 'Duplicate Detection Rules':

      • likely_duplicate: sem > 0.90 AND same CI AND status='open'
      • resolution_available: sem > 0.85 AND resolved
    Precedence: duplicate wins when both could fire.
    """
    cand_status = (cand.get("status") or "").lower()
    same_ci = bool(source.get("ci_id")) and source.get("ci_id") == cand.get("ci_id")
    if sem > _DUP_SIM and same_ci and cand_status in ("open", "new", "in-progress", "in_progress"):
        return "likely_duplicate"
    if sem > _RES_SIM and cand_status in ("resolved", "closed", "fulfilled"):
        return "resolution_available"
    return None


# ── main ─────────────────────────────────────────────────────────────────────

ConnProvider = Callable[[], Awaitable[asyncpg.Connection]]


async def find_similar(
    *,
    tenant_id: str,
    service_id: ServiceId,
    ticket_id: str,
    user_id: str,
    role: str,
    max_results: int = 5,
    time_filter: TimeFilter | None = None,
    same_category_only: bool = False,
    same_service_only: bool = False,
    prefer_status: PreferStatus = "any",
    min_similarity_score: float = 0.5,
    diagnosis_confirm: bool = True,
    connection_provider: ConnProvider,
    now: datetime | None = None,
    # Per-result content-derived label (rule §UC-2 trust UX, 2026-05-31).
    # Both None ⇒ discriminator pass is skipped; rows return
    # `discriminator=None`. The pass is one batched LLM call; failures
    # are silenced (see `discriminators.generate_discriminators`).
    discriminator_gateway: Any = None,
    discriminator_model: str | None = None,
) -> SimilarTicketsResponse:
    """Return up to `max_results` similar tickets for `(tenant_id, ticket_id)`.

    Caller responsibilities (done by the route/handler, NOT here):
      • ticket_id is already canonicalised via `id_resolver.resolve()`.
      • `service_id` is in {"incident", "request"}.
      • `tenant_id` came from a trusted source (x-tenant-id header or JWT).
      • Cache lookup BEFORE calling this; cache write AFTER on success.

    Edge cases handled here:
      • Source ticket has no symptom_anchor row → 503-equivalent RuntimeError.
      • Source ticket has no diagnosis_trail row → Stage 5 silently skipped.
      • Zero candidates after filters → empty results + `message`.
      • All candidates below `min_similarity_score` → empty + explanatory msg.
      • Limited-context (very short anchor text) → `warning` field set.
    """
    now = now or datetime.now(UTC)
    emb_tbl = _EMB_TABLE[service_id]
    base_tbl = _BASE_TABLE[service_id]
    id_col = _ID_COL[service_id]
    opened_col = _OPENED_COL[service_id]
    resolved_col = _RESOLVED_COL[service_id]

    span = _tracer.start_as_current_span("uc02.core.find_similar", attributes={
        "oneops.tenant_id": tenant_id,
        "uc02.service_id": service_id,
        "uc02.source_ticket_id": ticket_id,
        "uc02.k": max_results,
    })
    with span as s:
        conn = await connection_provider()
        try:
            # Shared pgvector HNSW filter-hardening — see oneops.db.pgvector_hnsw.
            await _apply_hnsw_hardening(conn)

            # ── Stage 0: existence check on the BASE table (404 vs 503) ─────
            # If the ticket doesn't exist in this tenant's base table, we must
            # return "not found" without leaking that it might exist elsewhere
            # (spec UC-2.4 + UC-2.7). Anchor-missing-on-existing-row is the
            # separate "refresh pending" case (503).
            base_exists = await conn.fetchval(
                f"SELECT 1 FROM {base_tbl} "
                f"WHERE tenant_id=$1 AND {id_col}=$2",
                tenant_id, ticket_id,
            )
            if base_exists is None:
                _log.info("uc02.base_row_missing",
                          tenant_id=tenant_id, ticket_id=ticket_id,
                          service_id=service_id)
                _metric_inc("ai.uc02.not_found.total", 1,
                            tenant_id=tenant_id, service_id=service_id)
                raise RuntimeError(
                    f"{service_id} {ticket_id} not found")

            # ── Stage 1: anchor lookup ──────────────────────────────────────
            anchor = await conn.fetchrow(
                f"""
                SELECT embedding, content_text
                FROM {emb_tbl}
                WHERE tenant_id = $1 AND entity_id = $2
                  AND chunk_type = 'symptom_anchor'
                LIMIT 1
                """,
                tenant_id, ticket_id,
            )
            if anchor is None:
                _log.info("uc02.anchor_missing",
                          tenant_id=tenant_id, ticket_id=ticket_id,
                          service_id=service_id)
                _metric_inc("ai.uc02.anchor_missing.total", 1,
                            tenant_id=tenant_id, service_id=service_id)
                # Base row exists but anchor not yet computed — refresh worker
                # will catch up. Route translates to 503 "retry shortly".
                raise RuntimeError(
                    f"no symptom_anchor embedding for "
                    f"{service_id}:{ticket_id} (tenant={tenant_id}); "
                    f"embedding refresh may be pending — retry shortly")

            anchor_vec = anchor["embedding"]
            anchor_text = anchor["content_text"] or ""
            limited_ctx = len(anchor_text.strip()) < _LIMITED_CTX_CHARS

            # ── Stage 1b: optional diagnosis vector for confirm step ────────
            diag_vec = None
            if diagnosis_confirm:
                diag = await conn.fetchrow(
                    f"""
                    SELECT embedding FROM {emb_tbl}
                    WHERE tenant_id = $1 AND entity_id = $2
                      AND chunk_type = 'diagnosis_trail'
                    LIMIT 1
                    """,
                    tenant_id, ticket_id,
                )
                if diag is not None:
                    diag_vec = diag["embedding"]

            # ── Stage 2 + 3: ANN with tenant + filters JOINed to base ───────
            #
            # We push every filter into ONE query so the planner can use both
            # the HNSW index (ORDER BY embedding <=> $vec) and the tenant +
            # service-id-equality predicates without materializing intermediates.
            #
            # Scope filters from the spec:
            #   • time_filter (TimeFilter) → b.<boundary_col> ≥/< predicate
            #   • same_category_only / same_service_only / prefer_status

            limit_n = max(max_results * _OVERFETCH, max_results + 5)

            where = ["e.tenant_id = $1",
                     "e.chunk_type = 'symptom_anchor'",
                     "e.entity_id <> $2",
                     "b.tenant_id = $1"]
            params: list[Any] = [tenant_id, ticket_id]

            # source row — we need its category / service_name / ci_id for
            # filters and re-rank. One extra SELECT keeps the SQL clean.
            src_row = await conn.fetchrow(
                f"""
                SELECT {id_col} AS id, title, category, service_name, ci_id,
                       status, priority, assignment_group, assigned_to,
                       {opened_col} AS opened_at
                FROM {base_tbl}
                WHERE tenant_id = $1 AND {id_col} = $2
                """ if service_id == "incident" else
                f"""
                SELECT {id_col} AS id, title, category,
                       NULL::text AS service_name, ci_id,
                       status, priority, assignment_group, assigned_to,
                       {opened_col} AS opened_at
                FROM {base_tbl}
                WHERE tenant_id = $1 AND {id_col} = $2
                """,
                tenant_id, ticket_id,
            )
            if src_row is None:
                _log.info("uc02.source_row_missing", tenant_id=tenant_id,
                          ticket_id=ticket_id, service_id=service_id)
                raise RuntimeError(
                    f"{service_id} {ticket_id} not found in {base_tbl} "
                    f"(tenant={tenant_id})")
            src = dict(src_row)

            if same_category_only and src.get("category"):
                params.append(src["category"])
                where.append(f"b.category = ${len(params)}")
            if same_service_only and src.get("service_name") and service_id == "incident":
                params.append(src["service_name"])
                where.append(f"b.service_name = ${len(params)}")
            # ── Time scope (TimeFilter only) ─────────────────────────────
            #
            # Structured filter built by `TimeFilterExtractor` (chat) or
            # passed directly (button). Boundary mapping: `TimeFilter.boundary`
            # names canonical columns; for `created_at` we keep the per-
            # service alias (`opened_col`); other boundaries pass through
            # verbatim.
            tf_boundary_col = opened_col
            if time_filter is not None and not time_filter.is_empty():
                tf_boundary_col = (
                    opened_col if time_filter.boundary == "created_at"
                    else time_filter.boundary
                )
                if time_filter.has_relative():
                    params.append(time_filter.relative_days)
                    where.append(
                        f"b.{tf_boundary_col} >= NOW() - "
                        f"(${len(params)}::int * INTERVAL '1 day')")
                else:
                    if time_filter.start_date is not None:
                        params.append(time_filter.start_date)
                        where.append(
                            f"b.{tf_boundary_col} >= ${len(params)}::date")
                    end_inc = time_filter.end_date_inclusive()
                    if end_inc is not None:
                        params.append(end_inc)
                        where.append(
                            f"b.{tf_boundary_col} < ${len(params)}::date")
            if prefer_status == "open":
                where.append(
                    "b.status IN ('open','new','in-progress','in_progress','assigned')")
            elif prefer_status == "resolved":
                where.append("b.status IN ('resolved','closed','fulfilled')")

            # RBAC predicate — bound to base table.
            rbac_sql, rbac_args = _default_rbac(role, user_id, service_id)
            if rbac_sql != "TRUE":
                start = len(params) + 1
                params.extend(rbac_args)
                where.append(
                    "b." + rbac_sql % tuple(range(start, start + len(rbac_args))))

            # pgvector cosine — literal cast avoids the asyncpg vector codec.
            vec_lit = _vec_literal(list(anchor_vec) if not isinstance(anchor_vec, str) else _parse_pgvector(anchor_vec))
            params.append(vec_lit)
            vec_param = f"${len(params)}::vector"
            params.append(limit_n)
            lim_param = f"${len(params)}"

            sql_main = f"""
            SELECT b.{id_col} AS id, b.title, b.status, b.priority,
                   b.category,
                   {'b.service_name' if service_id == 'incident' else 'NULL::text AS service_name'},
                   {'b.subcategory' if service_id == 'incident' else 'NULL::text AS subcategory'},
                   b.ci_id, b.assigned_to, b.assignment_group,
                   b.{opened_col}    AS opened_at,
                   b.{resolved_col}  AS resolved_at,
                   1 - (e.embedding <=> {vec_param}) AS sem_score
            FROM   {emb_tbl} e
            JOIN   {base_tbl} b
              ON   b.{id_col} = e.entity_id AND b.tenant_id = e.tenant_id
            WHERE  {' AND '.join(where)}
            ORDER  BY e.embedding <=> {vec_param}
            LIMIT  {lim_param}
            """
            rows = await conn.fetch(sql_main, *params)
            s.set_attribute("uc02.candidates_count", len(rows))

            # ── TimeFilter observability + before/after recall counts ────────
            # Operators need both numbers to diagnose over-filtering complaints
            # ("nothing in 'last week' but plenty in 'last month'"). We emit
            # the canonical TimeFilter attribute set spec'd by the schema.
            if time_filter is not None and not time_filter.is_empty():
                for k, v in time_filter.otel_attrs().items():
                    if v is not None:
                        s.set_attribute(k, v)
                s.set_attribute(
                    "time_filter.candidates_after_filter", len(rows))
                # `candidates_before_filter`: same query, no time predicate.
                # Cheap — HNSW ANN over the same vector + same other filters.
                try:
                    where_no_time = [
                        p for p in where
                        if not p.startswith(f"b.{tf_boundary_col}")
                    ]
                    sql_no_time = sql_main.replace(
                        " AND ".join(where), " AND ".join(where_no_time))
                    pre_rows = await conn.fetch(sql_no_time, *params)
                    s.set_attribute(
                        "time_filter.candidates_before_filter", len(pre_rows))
                except Exception:                                       # noqa: BLE001
                    # Operator metric only — never fail the request on it.
                    s.set_attribute(
                        "time_filter.candidates_before_filter", -1)

            # ── Stage 4: re-rank ────────────────────────────────────────────
            scored: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                sem = max(0.0, min(1.0, float(d.get("sem_score") or 0.0)))
                # Hard sem safety floor — never score outright garbage.
                # The user-tunable cut is `min_similarity_score` applied
                # to the composite below, which matches the response's
                # `similarity_score` field semantics.
                if sem < 0.10:
                    continue
                meta_score, why = _metadata_signal(source=src, cand=d)
                rec_score = _recency_decay(d.get("opened_at"), now)
                if d.get("status", "").lower() in ("resolved", "closed", "fulfilled"):
                    why.append("resolved")
                # Semantic-confidence gate: when the embedder says the text
                # is not actually close, mute the metadata + recency boost
                # proportionally. Prevents `same_ci`+`same_group` from
                # dragging an unrelated ticket to #1 on a low-signal source.
                if sem >= _SEM_FULL_TRUST:
                    sem_trust = 1.0
                elif sem <= _SEM_FLOOR:
                    sem_trust = 0.0
                else:
                    sem_trust = (sem - _SEM_FLOOR) / (_SEM_FULL_TRUST - _SEM_FLOOR)
                composite = (_W_SEMANTIC * sem
                             + _W_METADATA * meta_score * sem_trust
                             + _W_RECENCY * rec_score * sem_trust)
                # User-tunable composite floor — matches the response's
                # `similarity_score` field semantics. Default 0.5 so the
                # tail never includes <50% match items.
                if composite < min_similarity_score:
                    continue
                d["_sem"] = sem
                d["_meta"] = meta_score
                d["_rec"] = rec_score
                d["_sem_trust"] = sem_trust
                d["_composite"] = composite
                d["_why"] = why
                scored.append(d)

            # ── Stage 5: diagnosis confirm (top-K only) ─────────────────────
            if diag_vec is not None and scored:
                top_ids = [d["id"] for d in scored[:max_results]]
                diag_lit = _vec_literal(list(diag_vec) if not isinstance(diag_vec, str) else _parse_pgvector(diag_vec))
                diag_rows = await conn.fetch(
                    f"""
                    SELECT entity_id,
                           1 - (embedding <=> $1::vector) AS trail_score
                    FROM   {emb_tbl}
                    WHERE  tenant_id = $2 AND chunk_type = 'diagnosis_trail'
                      AND  entity_id = ANY($3::text[])
                    """,
                    diag_lit, tenant_id, top_ids,
                )
                trail_by_id = {r["entity_id"]: float(r["trail_score"])
                               for r in diag_rows}
                for d in scored:
                    t = trail_by_id.get(d["id"])
                    if t is not None and t >= _DIAG_THRESHOLD:
                        # Same sem-confidence gate as metadata/recency
                        # (2026-05-31): a diagnosis-trail match on a
                        # candidate whose primary text is only mildly
                        # similar to the source should not promote it
                        # into the top-K. Gate the boost by `_sem_trust`.
                        d["_composite"] += _DIAG_BOOST * d.get("_sem_trust", 1.0)
                        d["_why"].append("diagnosis_match")

            scored.sort(key=lambda x: x["_composite"], reverse=True)
            top = scored[:max_results]

            # ── Stage 5.5: per-result discriminator labels ──────────────────
            # Closes the "they all look the same" perception when many top-K
            # share the same `why_similar` tags. One batched LLM call returns
            # a short failure-mode label per result. Skipped silently when
            # the LLM gateway isn't wired or any error occurs.
            discriminators: dict[str, str] = {}
            if discriminator_gateway is not None and top:
                top_ids = [d["id"] for d in top]
                desc_rows = await conn.fetch(
                    f"SELECT {id_col} AS id, description "
                    f"FROM {base_tbl} "
                    f"WHERE tenant_id = $1 AND {id_col} = ANY($2::text[])",
                    tenant_id, [ticket_id, *top_ids],
                )
                desc_by_id = {r["id"]: (r["description"] or "")
                              for r in desc_rows}
                from oneops.use_cases.uc02_similar_tickets.discriminators import (
                    generate_discriminators,
                )
                discriminators = await generate_discriminators(
                    gateway=discriminator_gateway,
                    model=discriminator_model or "gpt-4o-mini",
                    source_title=str(src.get("title") or ""),
                    source_desc=desc_by_id.get(ticket_id, ""),
                    candidates=[
                        {
                            "ticket_id": str(d["id"]),
                            "title": str(d.get("title") or ""),
                            "description": desc_by_id.get(d["id"], ""),
                        }
                        for d in top
                    ],
                    tenant_id=tenant_id,
                    user_id=user_id,
                )

            # ── Stage 6: build response with flags + spec messages ──────────
            results: list[SimilarTicket] = []
            for d in top:
                flag = _flag_for(sem=d["_sem"], source=src, cand=d)
                sim = max(0.0, min(1.0, d["_composite"]))
                results.append(SimilarTicket(
                    ticket_id=str(d["id"]),
                    service_id=service_id,
                    title=str(d.get("title") or ""),
                    status=str(d.get("status") or ""),
                    priority=d.get("priority"),
                    category=d.get("category"),
                    subcategory=d.get("subcategory"),
                    service_name=d.get("service_name"),
                    ci_id=d.get("ci_id"),
                    assigned_to=d.get("assigned_to"),
                    assignment_group=d.get("assignment_group"),
                    opened_at=d.get("opened_at"),
                    resolved_at=d.get("resolved_at"),
                    similarity_score=sim,
                    match_pct=int(round(sim * 100)),
                    confidence=d["_sem"],
                    why_similar=d["_why"],
                    discriminator=discriminators.get(str(d["id"])) or None,
                    flag=flag,
                ))

            message: str | None = None
            warning: str | None = None
            if not results:
                # Spec UC-2.6: distinguish "no similar at all" from "none
                # within the requested window". The orchestrator's next-step
                # suggestion differs by case.
                window_label = (
                    time_filter.label
                    if time_filter is not None and time_filter.label
                    else None
                )
                if not rows:
                    if window_label:
                        message = (
                            f"no tickets similar to {ticket_id} were found "
                            f"within {window_label} — try widening the time "
                            f"range")
                    else:
                        message = ("no significantly similar tickets found "
                                   "within the current scope")
                else:
                    message = (f"no candidates met the minimum similarity "
                               f"threshold ({min_similarity_score:.2f})")
            elif results[0].similarity_score < 0.50:
                # UC-2.3 low-signal — top match is weak.
                warning = ("limited context — top similarity is low; "
                           "consider broadening the scope or refining the "
                           "source ticket text")
            if limited_ctx:
                # UC-2.2 vague source ticket warning.
                warning = (warning + " | " if warning else "") + \
                    "source ticket has limited descriptive content"

            _metric_inc("ai.uc02.results.total", len(results),
                        tenant_id=tenant_id, service_id=service_id)
            # Source-ticket snapshot — lets the UI render "you queried X" at
            # the top of the result list without a separate UC-1 round-trip.
            source_snapshot = SimilarTicket(
                ticket_id=str(src.get("id") or ticket_id),
                service_id=service_id,
                title=str(src.get("title") or ""),
                status=str(src.get("status") or ""),
                priority=src.get("priority"),
                category=src.get("category"),
                subcategory=None,  # not in src_row; UI hides if absent
                service_name=src.get("service_name"),
                ci_id=src.get("ci_id"),
                assigned_to=src.get("assigned_to"),
                assignment_group=src.get("assignment_group"),
                opened_at=src.get("opened_at"),
                resolved_at=None,
                similarity_score=1.0,
                match_pct=100,
                confidence=1.0,
                why_similar=[],
                flag=None,
            )

            return SimilarTicketsResponse(
                source_ticket_id=ticket_id,
                service_id=service_id,
                tenant_id=tenant_id,
                results=results,
                total_candidates_considered=len(rows),
                message=message,
                warning=warning,
                cached=False,
                time_filter=(
                    time_filter if time_filter is not None
                    and not time_filter.is_empty()
                    else None
                ),
                source_ticket=source_snapshot,
            )
        finally:
            try:
                await conn.close()
            except Exception:                                       # noqa: BLE001
                pass


def _parse_pgvector(s: str) -> list[float]:
    """Parse a pgvector text repr '[1.0,2.0,...]' into a list[float]."""
    return [float(x) for x in s.strip().strip("[]").split(",") if x]


__all__ = ["find_similar"]
