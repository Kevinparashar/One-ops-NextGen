"""UC-3 listwise reranker — the precision stage of the KB retrieval pipeline.

WHY THIS EXISTS
Retrieval (hybrid FTS + dense vector, fused by RRF) is a high-RECALL, low-cost
*candidate generator*. Ordering those candidates by the raw bi-encoder cosine
the vector branch produced is NOT a reliable final order: a single embedding
compresses query and document independently, so for in-domain enterprise-IT text
it can only separate candidates inside a narrow ~0.30-0.40 band — relevant and
irrelevant articles overlap, and the wrong one frequently sorts first
(observed 2026-06-12: "Fix CRM 500 errors" out-ranked the database articles for
"not able to access my database"; "Password reset procedure" sorted 4th for
"having login issues").

Every ITSM/RAG vendor that discloses its pipeline solves this the same way: a
cross-encoder / reranker re-scores the shortlist by reading the (query, document)
pair JOINTLY (Microsoft Azure AI Search "L2 semantic ranker", ServiceNow AI
Search "relevancy reranker", Atlassian Rovo cross-encoder, Moveworks multi-signal
reranker, Cohere/Voyage/BGE rerank APIs). We have no self-hosted cross-encoder,
so this stage realises the same idea with one LLM call through the existing
gateway (single egress §2.5 — OTel span, per-tenant cost, policy, retries for
free). The LLM reads the full shortlist and the query together and assigns each
candidate a closed-scale relevance label; the handler then orders by that label
and abstains when the best is below a calibrated floor.

CONTRACT
`rerank()` returns a list of `RerankResult` (kb_id + relevance ∈ [0,1]) ALIGNED
to the input articles, or `None` on any failure / no gateway — the caller then
falls back to the existing cosine order (no silent failure §2.7; a reranker
outage degrades to the old behaviour, never to an empty answer).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

from oneops.observability import get_logger, get_tracer, increment

_log = get_logger("oneops.use_cases.uc03.kb_rerank")
_tracer = get_tracer("oneops.use_cases.uc03.kb_rerank")

# Closed relevance scale the model scores against (Azure AI Search uses an
# analogous 0-4 rerankerScore; ServiceNow an "extremely high confidence" gate).
# A small integer scale with explicit anchors is far more consistent run-to-run
# than asking for a free 0-1 float. Normalised to [0,1] for a single,
# model-agnostic threshold downstream.
_MAX_LABEL = 3
_RERANK_PROMPT = f"""You are a relevance judge for an enterprise IT (ITSM/ITOM) \
knowledge base.

You are given a USER QUERY and a numbered list of candidate KB articles \
(id + title + summary + content excerpt). Score how well EACH article answers \
the user's query, judging the query and the article together — not by keyword \
overlap alone. Consider what the user is actually trying to accomplish.

Use this exact integer scale:
  3 = Directly answers the query / is the correct resolution for it.
  2 = Relevant: same problem area, useful to the user, partial answer.
  1 = Loosely related: adjacent topic, unlikely to resolve the query.
  0 = Irrelevant: different problem, only superficial word overlap.

Rules:
- Judge by MEANING and INTENT. "having login issues" → a password-reset or \
MFA guide is a 3; a "detect a suspicious login attempt" security runbook is \
about a different intent (attack detection, not the user's own access) → 1.
- An article about a different system that merely shares a generic word \
(e.g. "error", "500", "access") with the query is 0 or 1, never 3.
- Score every article independently; ties are allowed.
- Do NOT invent articles. Score only the ids you are given.

Return ONLY a JSON object, no prose, of this exact shape:
{{"rankings": [{{"id": "<kb_id>", "relevance": <0-{_MAX_LABEL}>}}, ...]}}
Include every candidate id exactly once."""


@dataclass(frozen=True)
class RerankResult:
    """One article's reranker verdict. `relevance` is normalised to [0,1]
    (raw 0-3 label / 3) so the abstain threshold is model-agnostic."""

    kb_id: str
    relevance: float
    raw_label: int


class Reranker(Protocol):
    async def rerank(
        self, *, query: str, articles: list[dict[str, Any]],
        tenant_id: str, user_id: str = "", request_id: str = "",
    ) -> list[RerankResult] | None: ...


def _excerpt(article: dict[str, Any], *, limit: int = 700) -> str:
    """Compact, faithful candidate block for the judge. Content is bounded so a
    long article can't blow the attention budget across an 8-candidate list."""
    kb_id = str(article.get("kb_id") or "")
    title = str(article.get("title") or "").strip()
    summary = str(article.get("summary") or "").strip()
    content = str(article.get("content") or "").strip()
    if len(content) > limit:
        content = content[:limit] + "…"
    block = f"id: {kb_id}\nTitle: {title}"
    if summary:
        block += f"\nSummary: {summary}"
    if content:
        block += f"\nContent: {content}"
    return block


def _parse_rankings(
    text: str, articles: list[dict[str, Any]],
) -> list[RerankResult] | None:
    """Parse the model's JSON into results ALIGNED to `articles` order. A
    candidate the model omitted defaults to label 0 (judged irrelevant — safer
    than dropping it from the gate accounting). Returns None on unparseable
    output so the caller falls back to the cosine order."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):] if "{" in raw else raw
    try:
        obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1] or raw)
        rows = obj.get("rankings") if isinstance(obj, dict) else None
        if not isinstance(rows, list):
            return None
    except (ValueError, AttributeError):
        return None
    by_id: dict[str, int] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or r.get("kb_id") or "").strip()
        try:
            label = int(round(float(r.get("relevance", 0))))
        except (TypeError, ValueError):
            label = 0
        if rid:
            by_id[rid] = max(0, min(_MAX_LABEL, label))
    if not by_id:
        return None
    out: list[RerankResult] = []
    for a in articles:
        kb_id = str(a.get("kb_id") or "")
        label = by_id.get(kb_id, 0)
        out.append(RerankResult(kb_id=kb_id,
                                relevance=label / float(_MAX_LABEL),
                                raw_label=label))
    return out


def _candidate_fingerprint(articles: list[dict[str, Any]]) -> str:
    """A stable hash of the EXACT candidate text the reranker scores against.
    Sorted by content so order doesn't matter; includes the article body, so an
    EDITED article changes the fingerprint → a new cache key → never a stale
    ranking (same staleness discipline as the embedding content-hash)."""
    parts = sorted(f"{a.get('kb_id', '')}\x1f{_excerpt(a)}" for a in articles)
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()[:24]


def _rerank_cache_key(
    *, tenant_id: str, model: str, query: str, articles: list[dict[str, Any]],
) -> str:
    q = " ".join((query or "").strip().lower().split())
    h = hashlib.sha256(
        f"{q}\x1f{_candidate_fingerprint(articles)}".encode()).hexdigest()[:32]
    return f"oneops:uc03:rerank:{tenant_id}:{model}:{h}"


class _RerankCache:
    """Cross-session result cache for the deterministic (temp 0) reranker.

    The reranker output is a pure function of (query, candidate texts, model),
    so a repeat of the SAME (query, candidate-set) — even from a different user
    or session — can skip the LLM call entirely. The session-scoped chat-turn
    cache can't catch that cross-session repeat; this can. Staleness-proof: the
    key carries a fingerprint of the exact candidate text (see above). Tenant is
    in the key (no cross-tenant read). Best-effort: any cache error falls through
    to a live call — never blocks or raises."""

    def __init__(self) -> None:
        self._redis: Any = None
        self._lock = asyncio.Lock()
        self._ttl = int(os.getenv("ONEOPS_KB_RERANK_CACHE_TTL_S", "86400"))  # 24h

    async def _client(self) -> Any:
        if self._redis is not None:
            return self._redis
        async with self._lock:
            if self._redis is not None:
                return self._redis
            try:
                import redis.asyncio as aioredis

                from oneops.config import get_settings
                url = getattr(get_settings(), "dragonfly_url",
                              "redis://localhost:6379/0")
                self._redis = aioredis.from_url(url, decode_responses=False)
            except Exception as exc:                          # noqa: BLE001
                _log.warning("kb_rerank.cache.client_init_failed",
                             error=str(exc)[:160])
                self._redis = False  # sentinel: disabled
            return self._redis

    async def get(self, key: str, *, tenant_id: str
                  ) -> list[RerankResult] | None:
        client = await self._client()
        if not client:
            return None
        try:
            raw = await client.get(key)
        except Exception as exc:                              # noqa: BLE001
            _log.warning("kb_rerank.cache.get_failed", error=str(exc)[:160])
            return None
        if raw is None:
            increment("ai.cache.misses.total", cache_name="uc03_kb_rerank",
                      tenant_id=tenant_id)
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            rows = json.loads(raw)
        except Exception:                                     # noqa: BLE001
            return None
        if not isinstance(rows, list):
            return None
        increment("ai.cache.hits.total", cache_name="uc03_kb_rerank",
                  tenant_id=tenant_id)
        return [RerankResult(kb_id=str(r.get("id", "")),
                             relevance=float(r.get("rel", 0.0)),
                             raw_label=int(r.get("lbl", 0)))
                for r in rows if isinstance(r, dict)]

    async def put(self, key: str, results: list[RerankResult]) -> None:
        client = await self._client()
        if not client:
            return
        try:
            payload = json.dumps([
                {"id": r.kb_id, "rel": r.relevance, "lbl": r.raw_label}
                for r in results])
            await client.setex(key, self._ttl, payload)
        except Exception as exc:                              # noqa: BLE001
            _log.warning("kb_rerank.cache.put_failed", error=str(exc)[:160])


_cache = _RerankCache()


class LlmListwiseReranker:
    """Production reranker — one policy-wrapped, OTel-spanned gateway call that
    reads the whole shortlist and the query together and labels each candidate.
    Any failure returns None (caller keeps the cosine order) — never raises."""

    def __init__(self, gateway: Any, *, model: str = "gpt-4o") -> None:
        self._gateway = gateway
        self._model = model

    async def rerank(
        self, *, query: str, articles: list[dict[str, Any]],
        tenant_id: str, user_id: str = "", request_id: str = "",
    ) -> list[RerankResult] | None:
        if not query or not articles or not tenant_id:
            return None
        from oneops.errors import LLMGatewayError
        from oneops.llm import LlmMessage, LlmRequest
        from oneops.policy import Profile, compose

        # Cross-session result cache: a repeat of the same (query, candidate-set)
        # skips the LLM call entirely (deterministic at temp 0). Key carries the
        # candidate-text fingerprint, so edited content invalidates it.
        cache_key = _rerank_cache_key(
            tenant_id=tenant_id, model=self._model, query=query,
            articles=articles)
        cached = await _cache.get(cache_key, tenant_id=tenant_id)
        if cached is not None:
            return cached

        candidate_block = "\n\n".join(
            f"[{i}] {_excerpt(a)}" for i, a in enumerate(articles, 1))
        user_block = (
            f"--- USER QUERY ---\n{query.strip()}\n\n"
            f"--- CANDIDATE ARTICLES ({len(articles)}) ---\n{candidate_block}"
        )
        system_prompt = compose(
            Profile.INTERNAL_AGENT, extra_sections=[_RERANK_PROMPT])
        with _tracer.start_as_current_span(
            "uc03.kb_rerank.rerank",
            attributes={
                "oneops.tenant_id": tenant_id,
                "oneops.user_id": user_id,
                "oneops.kb.candidate_count": len(articles),
            },
        ) as span:
            llm_request = LlmRequest(
                messages=(
                    LlmMessage("system", system_prompt, cache_control=True),
                    LlmMessage("user", user_block),
                ),
                model=self._model,
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
                temperature=0.0,
                max_tokens=400,
            )
            try:
                resp = await self._gateway.call(llm_request)
                text = (resp.content or "").strip()
            except LLMGatewayError as exc:
                span.set_attribute("error", True)
                _log.warning("uc03.kb_rerank.gateway_failed",
                             error=str(exc)[:200])
                return None
            results = _parse_rankings(text, articles)
            if results is None:
                span.set_attribute("oneops.kb.rerank_parse_failed", True)
                _log.warning("uc03.kb_rerank.unparseable",
                             sample=text[:160])
                return None
            span.set_attribute(
                "oneops.kb.rerank_top",
                max((r.relevance for r in results), default=0.0))
            await _cache.put(cache_key, results)
            return results


# Process-wide injection seam — set by app.py at startup; tests override.
_reranker: Reranker | None = None


def set_kb_reranker(impl: Reranker | None) -> None:
    global _reranker
    _reranker = impl


def get_kb_reranker() -> Reranker | None:
    return _reranker


def rerank_min_relevance() -> float:
    """Calibrated abstain floor on the NORMALISED [0,1] reranker score. An
    article must clear this to be surfaced; if none does, the handler returns
    no_match. Default 0.5 == raw label ≥ 1.5 ≈ "relevant (2) or better", which
    keeps genuine partial answers and drops label-0/1 word-overlap noise.
    Env-tunable (no hardcode) — calibrate per the Cohere method (a set of
    borderline-labelled queries), never a guessed global cliff."""
    try:
        return float(os.getenv("UC03_RERANK_MIN_RELEVANCE", "0.5"))
    except ValueError:
        return 0.5


__all__ = [
    "RerankResult",
    "Reranker",
    "LlmListwiseReranker",
    "set_kb_reranker",
    "get_kb_reranker",
    "rerank_min_relevance",
]
