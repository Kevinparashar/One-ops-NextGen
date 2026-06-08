"""UC-3 tool handlers — knowledge-base lookup, built to the Component Spec.

Three deterministic tools, the P7 `(arguments, context)` contract:

  * `search_kb`            — keyword search of published articles, ranked.
  * `get_kb_article`       — fetch one article by id, full content.
  * `search_kb_by_ticket`  — articles linked to a given incident / CI.

Spec conformance:
  * C8  — structured output: each returns a declared result object, never a
          free-form dict.
  * C10 — deterministic: keyword search + a policy filter; no LLM. (The LLM
          that *composes the user-facing answer* from this output is the
          feature-agent step — a separate component, separately policy-wired.)
  * C12 — no static catalogs: audience visibility is registry-driven via the
          field policy.
  * C13 / C14 — tenant-scoped + audience/RBAC-scoped: tenant_id from the
          envelope; only `published` articles in the caller's audience.
  * C17 — no silent failure: every path returns an explicit outcome + message.
  * C21 — pluggable backend: data access via `KbStore` (in-memory default).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from oneops.observability import get_logger
from oneops.use_cases._shared.field_policy import get_field_policy
from oneops.use_cases._shared.kb_store import get_kb_store
from oneops.use_cases.uc03_kb_lookup.answer_composer import (
    DeterministicComposer,
    get_kb_answer_composer,
)
from oneops.use_cases.uc03_kb_lookup.kb_embed import get_kb_embed_fn

_log = get_logger("oneops.use_cases.uc03.handlers")

# Repeated literals → constants (sonar S1192).
_NO_TENANT_SCOPE_WAS_SUPPLIED_FOR_THIS_REQUEST = "No tenant scope was supplied for this request."

_EMPTY: tuple[Any, ...] = (None, "", [], {})


@dataclass(frozen=True)
class KbSearchResult:
    """Structured output of `search_kb` / `search_kb_by_ticket` (Spec C8).

    `articles` are compact previews (no full content — attention budget, C9);
    full content is fetched per-article via `get_kb_article`."""

    outcome: str          # "found" | "no_match" | "invalid_request"
    message: str
    articles: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "message": self.message,
                "articles": [dict(a) for a in self.articles]}


@dataclass(frozen=True)
class KbArticleResult:
    """Structured output of `get_kb_article` (Spec C8)."""

    outcome: str          # "found" | "not_found" | "invalid_request"
    article_id: str
    message: str
    article: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "article_id": self.article_id,
                "message": self.message, "article": self.article}


def _search(outcome: str, message: str,
            articles: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    return KbSearchResult(outcome=outcome, message=message,
                          articles=articles).to_dict()


def _article(outcome: str, article_id: str, message: str,
             article: dict[str, Any] | None = None) -> dict[str, Any]:
    return KbArticleResult(outcome=outcome, article_id=article_id,
                           message=message, article=article).to_dict()


# The exact signature of the composer's no-match template (answer_composer.py).
# The LLM composer is INSTRUCTED to render present articles and emit this only
# for an empty list — but being an LLM it can disobey on vaguely-worded queries.
_COMPOSER_NO_MATCH_SIGNATURE = "No matching knowledge-base article was found"


def _composer_wrongly_said_no_match(text: str) -> bool:
    """True when a composer reply is empty or a no-match template even though
    articles ARE present. The found/no-match OUTCOME is the handler's decision
    (it knows the article list), not the LLM's — this lets the handler ENFORCE
    that contract in code: articles present ⇒ the user sees them, never a
    composer-emitted 'no match'."""
    t = (text or "").strip()
    return (not t) or (_COMPOSER_NO_MATCH_SIGNATURE in t)


async def _render_present_articles(
    query: str, hits: list[dict[str, Any]], context: dict[str, Any],
) -> str:
    """Deterministic, reliable rendering of articles the gate already passed —
    used when the LLM composer empty/no-matched despite present articles."""
    return await DeterministicComposer().compose(
        query=query, articles=[dict(h) for h in hits],
        tenant_id=str(context.get("tenant_id") or ""),
        user_id=str(context.get("user_id") or ""))


async def _ticket_symptom_text(ticket_id: str, tenant_id: str) -> str:
    """The ticket's OWN symptom text — title + description + category — used as
    the semantic KB query when no KB is *linked* to the ticket.

    This is the production KB model (centralized / no-incident-reference): a KB
    article carries no ticket id, so "KB for this ticket" is matched by MEANING
    on the ticket's symptoms (which carry the technical terms + error codes the
    hybrid retriever keys on), NOT by a stored link or the user's vague phrasing.
    Returns '' when the ticket can't be fetched (caller falls back to the user's
    query) — never raises."""
    try:
        from oneops.router.entity_id import EntityIdNormalizer
        from oneops.use_cases._shared.ticket_store import get_ticket_store
        extracted = EntityIdNormalizer.from_registry_file().extract(ticket_id)
        if not extracted.entities:
            return ""
        e = extracted.entities[0]
        tstore = get_ticket_store()
        if tstore is None:
            return ""
        record = await tstore.get(ticket_id=e.entity_id, service_id=e.service_id,
                                  tenant_id=tenant_id)
        if not record:
            return ""
        parts = [
            str(record.get("title") or record.get("short_description") or ""),
            str(record.get("description") or ""),
            str(record.get("category") or ""),
        ]
        return " ".join(p for p in parts if p).strip()
    except Exception as exc:                                  # noqa: BLE001
        _log.info("uc03.ticket_symptom_lookup_skipped", error=str(exc)[:120])
        return ""


def _preview(article: dict[str, Any]) -> dict[str, Any]:
    """A compact, citation-ready preview of one article for a result list.

    Includes the article `content` so the composer (LLM or deterministic
    fallback) has the FULL article body to ground its answer in — without
    this, the LLM could only see `summary` (the title-line) and produced
    1-sentence non-answers (2026-05-30 user complaint). `content` may be
    several paragraphs; the composer decides what to render verbatim.
    """
    out: dict[str, Any] = {
        "kb_id": article.get("kb_id", ""),
        "title": article.get("title", ""),
        "summary": article.get("summary", ""),
        "content": article.get("content", ""),
        "category": article.get("category", ""),
        "tags": list(article.get("tags") or []),
    }
    if "relevance_score" in article:
        out["relevance_score"] = article["relevance_score"]
    return {k: v for k, v in out.items() if v not in _EMPTY}


def _audiences_for(context: dict[str, Any]) -> tuple[str, ...]:
    role = str(context.get("role") or "").strip()
    return get_field_policy().kb_audiences_for(role)


_KB_ID_RE = re.compile(r"\b[A-Z]{2,4}\d{6,}\b")
_KB_DANGLING_RE = re.compile(
    r"\b(?:for|regarding|about|on|of|in|to|with|re)\s*$", re.IGNORECASE)


def _kb_search_text(query: str) -> str:
    """Strip canonical record ids (INC…/REQ…/PBM…/CHG…) from a KB query.

    Knowledge-base articles are GENERAL content, not tied to a specific
    record — an id attached upstream by the focus channel / rewriter (e.g.
    "database connection fails for INC0001021") is pure noise that pollutes
    both the embedding and the FTS match. Remove it plus any connector word
    ("… for") left dangling. Never returns empty — falls back to the
    original if stripping would clear the whole query (e.g. a bare id, which
    should not have routed here anyway).
    """
    cleaned = _KB_ID_RE.sub("", query)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,")
    prev = ""
    while prev != cleaned:
        prev = cleaned
        cleaned = _KB_DANGLING_RE.sub("", cleaned).strip(" ,")
    return cleaned or query


def _search_kb_config() -> tuple[int, float, int]:
    """Env-tunable retrieval knobs → (per_side, min_answer_relevance, max_results).
    Retrieve broad per branch, then the relevance gate + top_k narrow."""
    try:
        per_side = max(1, int(os.getenv("UC03_RETRIEVE_PER_SIDE", "25")))
    except ValueError:
        per_side = 25
    try:
        min_score = float(os.getenv("UC03_MIN_ANSWER_RELEVANCE_SCORE", "0.50"))
    except ValueError:
        min_score = 0.50
    try:
        top_k = int(os.getenv("UC03_MAX_RESULTS", "3"))
    except ValueError:
        top_k = 3
    return per_side, min_score, top_k


async def _compose_kb_answer(
    query: str, hits: list[dict[str, Any]], context: dict[str, Any], *,
    tenant_id: str, force_deterministic: bool,
) -> dict[str, Any]:
    """Grounded-answer compose for the gate-passing articles. Degraded mode
    forces the deterministic list composer (no synthesis on un-verified
    relevance). Code-enforced contract: articles that passed the gate MUST be
    surfaced even if the LLM composer emitted a false no-match."""
    composer = (DeterministicComposer() if force_deterministic
                else get_kb_answer_composer() or DeterministicComposer())
    try:
        grounded = await composer.compose(
            query=query, articles=[dict(h) for h in hits],
            tenant_id=tenant_id,
            user_id=str(context.get("user_id") or ""),
            request_id=str(context.get("request_id") or ""))
    except Exception as exc:                          # noqa: BLE001 — boundary
        _log.warning("uc03.search_kb.compose_failed", error=str(exc)[:200])
        grounded = ""

    if not hits:
        # Defensive — should be unreachable after the gate. CASE B template.
        return _search(
            "no_match",
            grounded or ("No published knowledge-base article matched "
                         "that query."))
    # The LLM composer writes phrasing only; if it emitted a false no-match
    # despite present articles, render them deterministically.
    if _composer_wrongly_said_no_match(grounded):
        _log.warning("uc03.search_kb.composer_no_match_override",
                     articles=len(hits))
        grounded = await _render_present_articles(query, hits, context)

    articles = tuple(_preview(h) for h in hits)
    return _search(
        "found",
        grounded or f"Found {len(articles)} knowledge-base article(s).",
        articles)


def _score_fts_only(
    hits: list[dict[str, Any]], query_vec: list[float],
    stored_embeddings: dict[str, list[float]],
) -> None:
    """For FTS-only candidates (no cosine_full from the vector branch), compute
    cosine vs the query vector from their stored embedding (0.0 when missing —
    the gate then drops them). Mutates each hit's cosine_full / _score_source."""
    import math
    qn = math.sqrt(sum(x * x for x in query_vec)) or 1.0
    for h in hits:
        if "cosine_full" in h:
            continue                                    # vector-branch already scored
        emb = stored_embeddings.get(h.get("kb_id", ""))
        if not emb or len(emb) != len(query_vec):
            h["cosine_full"] = 0.0
            h["_score_source"] = "fts_only_no_embedding"
            continue
        en = math.sqrt(sum(x * x for x in emb)) or 1.0
        dot = sum(qx * ex for qx, ex in zip(query_vec, emb, strict=False))
        h["cosine_full"] = float(max(-1.0, min(1.0, dot / (qn * en))))
        h["_score_source"] = "fetched_for_fts_only"


async def _relevance_gate(
    hits: list[dict[str, Any]], query_vec: list[float], store: Any, *,
    tenant_id: str, min_score: float, top_k: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Full-content cosine relevance gate → (hits, force_deterministic_composer).

    Degraded mode (no query embedding) → pass the RRF top-K straight through
    with force_deterministic=True (honest list-style answer, never a silent
    no-match on an LLM outage). Otherwise score every candidate against the
    query vector (re-using stored embeddings for FTS-only candidates via one
    batched fetch — same embedding distribution ⇒ one threshold is valid),
    keep those >= min_score (top-K), and stamp a 1-100 relevance_score. An
    EMPTY return list = no passing candidate (caller surfaces no_match)."""
    if not query_vec and hits:
        _log.warning("uc03.search_kb.gate_bypass_degraded",
                     tenant_id=tenant_id, reason="no_query_embedding",
                     retrieved=len(hits))
        return hits[:top_k], True

    fts_only_ids = [h.get("kb_id", "") for h in hits
                    if "cosine_full" not in h and h.get("kb_id")]
    stored_embeddings: dict[str, list[float]] = {}
    if fts_only_ids and hasattr(store, "fetch_embeddings_by_ids"):
        try:
            stored_embeddings = await store.fetch_embeddings_by_ids(
                tenant_id=tenant_id, kb_ids=fts_only_ids)
        except Exception as exc:
            _log.warning("uc03.search_kb.fetch_embeddings_failed",
                         error=str(exc)[:160])
            stored_embeddings = {}
    _score_fts_only(hits, query_vec, stored_embeddings)

    scored = sorted(hits, key=lambda d: d.get("cosine_full", 0.0), reverse=True)
    passing = [h for h in scored if h.get("cosine_full", 0.0) >= min_score]
    passing = passing[:max(1, top_k)]
    per_candidate = [
        {
            "kb_id": h.get("kb_id"),
            "score": round(float(h.get("cosine_full", 0.0)), 4),
            "source": h.get("_score_source", "unknown"),
            "passed": float(h.get("cosine_full", 0.0)) >= min_score,
        }
        for h in scored
    ]
    _log.info("uc03.search_kb.relevance_gate",
              tenant_id=tenant_id, threshold=min_score, top_k=top_k,
              retrieved=len(hits), passed=len(passing), candidates=per_candidate)
    if not passing:
        return [], False
    out: list[dict[str, Any]] = []
    for h in passing:
        row = dict(h)
        row["relevance_score"] = max(1, min(100, int(h["cosine_full"] * 100)))
        out.append(row)
    return out, False


async def _embed_query(
    embed_fn: Any, query: str, *, tenant_id: str, user_id: str,
) -> list[float]:
    """Embed the query once (serves the semantic branch AND the relevance
    gate). Empty list on no embedder / failure — both downstream uses then
    degrade gracefully (semantic → [], gate → bypass)."""
    if embed_fn is None:
        return []
    try:
        return await embed_fn(query, tenant_id=tenant_id, user_id=user_id)
    except Exception as exc:                  # noqa: BLE001
        _log.warning("uc03.search_kb.query_embed_failed", error=str(exc)[:160])
        return []


def _rrf_fuse(
    fts_hits: list[dict[str, Any]], sem_hits: list[dict[str, Any]], *,
    min_fused_score: float, top_k: int,
) -> list[dict[str, Any]]:
    """Reciprocal-Rank-Fusion of the lexical + semantic lists (rank-only, k=60
    — sidesteps the FTS-rank vs cosine-distance scale mismatch). Preserves any
    cosine_full the semantic branch attached. Returns the top-K fused
    candidates above `min_fused_score`, each carrying _fused_score/_sources."""
    rrf_k = 60
    fused: dict[str, dict[str, Any]] = {}
    for src, lst in (("fts", fts_hits), ("sem", sem_hits)):
        for rank, h in enumerate(lst, start=1):
            kb_id = h.get("kb_id") or ""
            if not kb_id:
                continue
            slot = fused.setdefault(kb_id, dict(h))
            slot.setdefault("_fused_score", 0.0)
            slot["_fused_score"] += 1.0 / (rrf_k + rank)
            slot.setdefault("_sources", []).append(src)
            # Copy a both-branches candidate's semantic score across.
            if src == "sem" and "cosine_full" in h and "cosine_full" not in slot:
                slot["cosine_full"] = h["cosine_full"]
                slot["_score_source"] = "vector_branch"
            if src == "fts" and "relevance_score" in h:
                slot.setdefault("relevance_score", h["relevance_score"])
    ranked = sorted(fused.values(),
                    key=lambda d: d.get("_fused_score", 0.0), reverse=True)
    ranked = [d for d in ranked if d.get("_fused_score", 0.0) >= min_fused_score]
    return ranked[:top_k]


async def search_kb(
    arguments: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Keyword search of the published knowledge base, audience-scoped."""
    query = _kb_search_text(str(arguments.get("query") or "").strip())
    tenant_id = str(context.get("tenant_id") or "").strip()

    if not query:
        return _search("invalid_request", "A search query is required.")
    if not tenant_id:
        return _search("invalid_request",
                       _NO_TENANT_SCOPE_WAS_SUPPLIED_FOR_THIS_REQUEST)

    audiences = _audiences_for(context)
    store = get_kb_store()
    # ── Hybrid retrieval (Phase 5a) ───────────────────────────────────
    # Run lexical (FTS over `content_tsv`) AND semantic (HNSW cosine on
    # `embedding`) in PARALLEL, then fuse with Reciprocal Rank Fusion.
    # RRF: doc_score = Σ 1 / (k + rank_in_list), k=60 (Vespa default).
    # Rank-only fusion sidesteps the absolute-score-normalisation problem
    # (FTS rank vs cosine distance live on different scales). Top-K with
    # a minimum-score gate keeps low-confidence noise out of the
    # composer's prompt; truncation never happens — we cap per-article
    # content length, never the result-set count to a hard 0.
    import asyncio as _asyncio
    embed_fn = get_kb_embed_fn()
    # Candidate pool per branch (lexical / semantic) before RRF + the relevance
    # gate. Research consensus: retrieve BROAD, then narrow — a thin pool drops
    # the right article before the gate ever sees it. Env-tunable (not hardcoded);
    # the gate + top_k still narrow the final answer set.
    PER_SIDE, min_score, top_k = _search_kb_config()
    MIN_FUSED_SCORE = 0.012
    FUSE_TOP_K = 5

    # Embed the query ONCE. The same vector serves the semantic branch
    # (step 2b) AND the relevance gate (step 4, for FTS-only
    # candidates whose stored embedding will be cosined against this
    # vector). If embedding fails or is unwired, both downstream uses
    # degrade gracefully — semantic returns [], gate enters bypass.
    query_vec = await _embed_query(
        embed_fn, query, tenant_id=tenant_id,
        user_id=str(context.get("user_id") or ""))

    async def _semantic() -> list[dict[str, Any]]:
        if not query_vec:
            return []
        return await store.search_semantic(
            query_vec=query_vec, tenant_id=tenant_id,
            audiences=audiences, limit=PER_SIDE)

    fts_hits, sem_hits = await _asyncio.gather(
        store.search(query=query, tenant_id=tenant_id,
                     audiences=audiences, limit=PER_SIDE),
        _semantic(),
    )

    # RRF fusion. Preserves any `cosine_full` field already attached by
    # the semantic branch — fusion is rank-only and never overwrites
    # data. FTS-only candidates have no `cosine_full` until step 4's
    # batch-fetch fills it from the stored vector.
    hits = _rrf_fuse(fts_hits, sem_hits,
                     min_fused_score=MIN_FUSED_SCORE, top_k=FUSE_TOP_K)
    if hits:
        _log.info("uc03.search_kb.hybrid_fused",
                  tenant_id=tenant_id, fts_n=len(fts_hits),
                  sem_n=len(sem_hits), fused_n=len(hits),
                  top_score=hits[0].get("_fused_score", 0.0))
    for h in hits:
        h.pop("_fused_score", None)
        h.pop("_sources", None)

    # ── Step 4 — Relevance gate (full-content cosine, no re-embed) ────
    # Use the cosine similarity ALREADY computed against full-document
    # embeddings (propagated through RRF as `cosine_full`). For
    # candidates that surfaced only via FTS and therefore lack a
    # propagated score, batch-fetch their STORED embedding from
    # Postgres (one SQL round-trip, no LLM-embed) and compute cosine
    # against the same query vector. Same embedding distribution for
    # every candidate ⇒ one threshold is valid for all.
    #
    # Degraded mode: if the query embedding is empty (gateway down or
    # unwired) we have NO basis to score either kind of candidate.
    # Bypass the gate entirely and pass the RRF top-K to a
    # DETERMINISTIC composer (no LLM synthesis) so the user gets an
    # honest list-style fallback instead of a silent CASE B on every
    # query. The fix for "LiteLLM outage silently turns every KB query
    # into 'no match found'" found during code review 2026-05-27.
    hits, force_deterministic_composer = await _relevance_gate(
        hits, query_vec, store,
        tenant_id=tenant_id, min_score=min_score, top_k=top_k)
    if not hits:
        return _search(
            "no_match",
            f"No matching knowledge-base article was found for "
            f"\"{query}\". Try rephrasing it (use different terms or "
            f"more specific symptoms), or contact your IT support "
            f"team for help.")

    return await _compose_kb_answer(
        query, hits, context, tenant_id=tenant_id,
        force_deterministic=force_deterministic_composer)


async def _check_kb_access(
    store: Any, article_id: str, tenant_id: str,
    audiences: Any, role: str,
) -> dict[str, Any] | None:
    """Stages 1-2 of the KB fetch discipline: probe existence (tenant-scoped,
    no audience filter), then check state + audience (RBAC). Each stage emits
    its own designed reply so the user never gets a stale article from
    focus-bleed on a non-existent id. Returns an early-response dict to
    short-circuit, or None to proceed to the full fetch. A store without
    `exists` skips this (the stage-3 fetch supplies its own fallback)."""
    if not hasattr(store, "exists"):
        return None
    try:
        probe = await store.exists(kb_id=article_id, tenant_id=tenant_id)
    except Exception as exc:
        _log.warning("uc03.get_kb_article.exists_failed",
                     article_id=article_id, error=str(exc)[:200])
        probe = None
    if probe is None:
        _log.info("uc03.get_kb_article.not_found",
                  article_id=article_id, stage="exists")
        return _article(
            "not_found", article_id,
            f"No knowledge-base article with id {article_id} "
            f"exists in your tenant. Double-check the id, or run "
            f"a search to find what you need.")

    # The article exists; can THIS user / role see it?
    state = (probe.get("state") or "").lower()
    audience = (probe.get("audience") or "").lower()
    if state != "published":
        _log.info("uc03.get_kb_article.unpublished",
                  article_id=article_id, state=state)
        return _article(
            "not_found", article_id,
            f"Knowledge-base article {article_id} exists but is "
            f"not published. Contact the article owner or your "
            f"IT support team if you need access.")
    if audience and audience not in audiences:
        _log.info("uc03.get_kb_article.audience_denied",
                  article_id=article_id, audience=audience,
                  user_role=role)
        return _article(
            "denied", article_id,
            f"Knowledge-base article {article_id} exists but is "
            f"restricted to a different audience. Your role does "
            f"not have access. Contact the article owner or your "
            f"IT support team if access is required.")
    return None


async def get_kb_article(
    arguments: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Fetch one published knowledge-base article by id, full content."""
    article_id = str(arguments.get("article_id") or "").strip()
    tenant_id = str(context.get("tenant_id") or "").strip()

    if not article_id:
        return _article("invalid_request", article_id,
                        "A knowledge-base article id is required.")
    if not tenant_id:
        return _article("invalid_request", article_id,
                        _NO_TENANT_SCOPE_WAS_SUPPLIED_FOR_THIS_REQUEST)

    store = get_kb_store()
    audiences = _audiences_for(context)

    # ── Stages 1-2: existence + state/audience (RBAC) ─────────────────
    early = await _check_kb_access(
        store, article_id, tenant_id, audiences, context.get("role", ""))
    if early is not None:
        return early

    # ── Stage 3: full fetch + compose ─────────────────────────────────
    row = await store.get(
        kb_id=article_id, tenant_id=tenant_id, audiences=audiences)
    if row is None:
        # Defensive fallback if the store doesn't implement `exists`
        # (in-memory tests without the new method) — same designed
        # response.
        _log.info("uc03.get_kb_article.not_found_fallback",
                  article_id=article_id)
        return _article(
            "not_found", article_id,
            f"No knowledge-base article with id {article_id} is "
            f"available to you. Double-check the id, or run a search "
            f"to find what you need.")

    exposed = get_field_policy().expose(row)
    article = {k: v for k, v in exposed.items() if v not in _EMPTY}
    composer = get_kb_answer_composer() or DeterministicComposer()
    try:
        grounded = await composer.compose(
            query=f"show me article {article_id}",
            articles=[dict(article)],
            tenant_id=tenant_id,
            user_id=str(context.get("user_id") or ""),
            request_id=str(context.get("request_id") or ""))
    except Exception as exc:                          # noqa: BLE001 — boundary
        _log.warning("uc03.get_kb_article.compose_failed",
                     error=str(exc)[:200])
        grounded = ""
    if not grounded:
        grounded = f"Retrieved knowledge-base article {article_id}."
    return _article("found", article_id, grounded, article)


async def kb_backstop_answer(message: str, context: dict[str, Any]) -> str:
    """Domain-oracle probe for the scope gates (control_gate / boundary).

    Returns the composed KB answer when `message` genuinely matches authored
    content (so it IS in-domain), else "". The published KB corpus is the
    authoritative, data-driven domain signal — a query the KB can answer is
    never wrongly refused as "out of scope", and a true off-topic query
    matches nothing (search_kb's own relevance gate) so it is unaffected.
    Deterministic (embedding similarity), so it removes the LLM scope
    verdict's run-to-run / phrasing flip-flop. Best-effort — never raises.
    """
    msg = (message or "").strip()
    if not msg:
        return ""
    try:
        res = await search_kb({"query": msg}, context)
        if isinstance(res, dict) and res.get("outcome") == "found":
            return str(res.get("message") or "").strip()
    except Exception as exc:                                # noqa: BLE001
        _log.warning("uc03.kb_backstop.error", error=str(exc)[:160])
    return ""


async def search_kb_by_ticket(
    arguments: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Knowledge-base articles linked to a given incident or CI (cross-UC).

    Composer parity (2026-05-28): the linked-record path now runs the SAME
    grounded composer as `search_kb` (text-search path), so the user gets
    rendered article content — title, key resolution steps, source citation
    — instead of just a bare "Found N article(s) linked to X." count. The
    composer's input contract is identical (query, articles, tenant_id,
    user_id, request_id), so we reuse it 1:1; the deterministic fallback
    matches `search_kb`'s degraded-mode behaviour."""
    ticket_id = str(arguments.get("ticket_id") or "").strip()
    tenant_id = str(context.get("tenant_id") or "").strip()
    # The router binds `user_message` (and `query` as an alias) into chat-
    # path arguments so the composer can synthesise around the user's exact
    # phrasing. For the button path the message will be the synthetic
    # "<verb> <ticket_id>" form — also valid composer input.
    user_query = (str(arguments.get("user_message") or "").strip()
                  or str(arguments.get("query") or "").strip()
                  or f"knowledge articles linked to {ticket_id}")

    if not ticket_id:
        return _search("invalid_request",
                       "A ticket or CI id is required to find linked articles.")
    if not tenant_id:
        return _search("invalid_request",
                       _NO_TENANT_SCOPE_WAS_SUPPLIED_FOR_THIS_REQUEST)

    hits = await get_kb_store().linked_to(
        entity_id=ticket_id, tenant_id=tenant_id, audiences=_audiences_for(context))
    if not hits:
        # No KB is *linked* to this ticket. In the production KB model this is
        # the NORMAL case (KB carries no incident reference), so we don't
        # dead-end: we match a KB by MEANING on the ticket's own SYMPTOMS
        # (title + description + category) — which carry the technical terms and
        # error codes the hybrid retriever keys on. This is the right query, not
        # the user's vague phrasing ("find KB for the root cause"). Fall back to
        # the user's topic words only when the ticket can't be fetched. Runs
        # through search_kb's full hybrid → RRF → relevance-gate stack.
        symptom_q = await _ticket_symptom_text(ticket_id, tenant_id)
        user_topic = _kb_search_text(user_query)
        content_q = symptom_q or user_topic
        if content_q:
            _log.info("uc03.search_kb_by_ticket.semantic_on_symptoms",
                      ticket_id=ticket_id, query=content_q[:80],
                      source=("symptoms" if symptom_q else "user_topic"))
            fb = await search_kb({"query": content_q}, context)
            # search_kb has its own relevance gate — only return it when it
            # genuinely matched.
            if isinstance(fb, dict) and fb.get("outcome") == "found":
                return fb
        return _search(
            "no_match",
            f"No knowledge-base article matches the symptoms of {ticket_id}.")

    # ── Semantic relevance gate (2026-05-29) ────────────────────────────
    # The linked_to() SQL is a HARD TAG JOIN — articles whose
    # `related_incidents` array contains this ticket id. Tags can be
    # over-broad in source data (an article marginally relevant to one CI
    # ends up tagged to every incident that touches that CI). Apply the
    # same relevance scorer the text-search path uses (kb_embed
    # `build_relevance_scorer`) to drop tagged-but-topically-distant
    # articles before the composer sees them.
    #
    # Relevance query = the focused record's TITLE when available
    # (semantically specific) + the user's natural query (general
    # signal). Article texts = title + summary + content. Cosine gate
    # at the same `UC03_MIN_ANSWER_RELEVANCE_SCORE` floor (0.50 by
    # default, env-configurable) the text-search path uses.
    #
    # Fail-OPEN: any scorer error → keep all candidates (current
    # behaviour, no regression).
    import os as _os
    try:
        min_score = float(_os.getenv("UC03_MIN_ANSWER_RELEVANCE_SCORE", "0.50"))
    except ValueError:
        min_score = 0.50
    try:
        top_k = int(_os.getenv("UC03_MAX_RESULTS", "3"))
    except ValueError:
        top_k = 3
    from oneops.use_cases.uc03_kb_lookup.kb_embed import (
        get_kb_relevance_scorer,
    )
    scorer = get_kb_relevance_scorer()
    if scorer is not None and len(hits) > 0:
        # Build the relevance query: focus record's title (when we can
        # cheaply look it up) is the strongest semantic signal. Fall
        # back to the user_query when the title isn't readily available.
        focus_title = ""
        try:
            from oneops.router.entity_id import EntityIdNormalizer
            from oneops.use_cases._shared.ticket_store import get_ticket_store
            extracted = EntityIdNormalizer.from_registry_file().extract(ticket_id)
            if extracted.entities:
                e = extracted.entities[0]
                tstore = get_ticket_store()
                if tstore is not None:
                    record = await tstore.get(
                        ticket_id=e.entity_id,
                        service_id=e.service_id,
                        tenant_id=tenant_id)
                    if record:
                        focus_title = (record.get("title")
                                       or record.get("short_description")
                                       or "")
        except Exception as exc:                          # noqa: BLE001
            _log.info("uc03.linked_to.focus_title_lookup_skipped",
                      error=str(exc)[:120])
        rel_query = focus_title or user_query
        doc_texts = [
            " ".join(filter(None, [
                str(h.get("title") or ""),
                str(h.get("summary") or ""),
                str(h.get("content") or "")[:1500],
            ])).strip()
            for h in hits
        ]
        try:
            scores = await scorer(
                rel_query, doc_texts,
                tenant_id=tenant_id,
                user_id=str(context.get("user_id") or ""),
            )
        except Exception as exc:                          # noqa: BLE001
            _log.warning("uc03.linked_to.relevance_gate.error",
                         error=str(exc)[:200])
            scores = []
        if scores and len(scores) == len(hits):
            for h, s in zip(hits, scores, strict=False):
                h["relevance_cosine"] = float(s)
            per_candidate = [
                {"kb_id": h.get("kb_id"),
                 "score": round(float(h.get("relevance_cosine") or 0.0), 4),
                 "passed": float(h.get("relevance_cosine") or 0.0) >= min_score}
                for h in hits
            ]
            passing = sorted(
                (h for h in hits
                 if float(h.get("relevance_cosine") or 0.0) >= min_score),
                key=lambda h: float(h.get("relevance_cosine") or 0.0),
                reverse=True,
            )[:max(1, top_k)]
            _log.info(
                "uc03.linked_to.relevance_gate",
                ticket_id=ticket_id, tenant_id=tenant_id,
                threshold=min_score, top_k=top_k,
                retrieved=len(hits), passed=len(passing),
                relevance_query=rel_query[:120],
                candidates=per_candidate,
            )
            if not passing:
                return _search(
                    "no_match",
                    f"No knowledge-base article linked to {ticket_id} "
                    f"is topically relevant to this record. "
                    f"Try a topic search instead.",
                )
            hits = passing

    # Run the grounded composer over the linked-record hits — same shape as
    # the text-search path. Failure falls back loudly to the bare count
    # (never silent: the deterministic composer is a real implementation,
    # not a mock, and produces an honest article-list when LLM is down).
    composer = get_kb_answer_composer() or DeterministicComposer()
    try:
        grounded = await composer.compose(
            query=user_query, articles=[dict(h) for h in hits],
            tenant_id=tenant_id,
            user_id=str(context.get("user_id") or ""),
            request_id=str(context.get("request_id") or ""))
    except Exception as exc:                              # noqa: BLE001 — boundary
        _log.warning("uc03.search_kb_by_ticket.compose_failed",
                     error=str(exc)[:200])
        grounded = ""

    # CODE-ENFORCED CONTRACT: these articles are LINKED to the ticket AND passed
    # the relevance gate — the found/no-match OUTCOME is decided here, not by the
    # LLM. The composer writes phrasing only; it must not flip a found result to
    # a no-match. If it emitted empty/no-match anyway (LLM disobeyed its
    # render-linked-articles instruction on a vague query), render the present
    # articles deterministically so the user always sees the article we found.
    if _composer_wrongly_said_no_match(grounded):
        _log.warning("uc03.search_kb_by_ticket.composer_no_match_override",
                     ticket_id=ticket_id, articles=len(hits))
        grounded = await _render_present_articles(user_query, hits, context)

    articles = tuple(_preview(h) for h in hits)
    return _search(
        "found",
        grounded or (f"Found {len(articles)} knowledge-base article(s) linked to "
                     f"{ticket_id}."),
        articles)


__all__ = [
    "KbSearchResult", "KbArticleResult",
    "search_kb", "get_kb_article", "search_kb_by_ticket",
]
