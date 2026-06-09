"""Router — the routing funnel (docs/architecture/ARCHITECTURE.md §3).

    user message
      │  stage 0a  decompose into sub-queries          (LLM, or passthrough)
      ▼
    for each sub-query:
      │  stage 0b  rewrite — resolve references         (LLM, or passthrough)
      │  stage 1   glossary normalization               (deterministic)
      │  stage 2   semantic retrieval → top-K            (deterministic — no LLM)
      │  stage 3   condition + ABAC filter               (deterministic — no LLM)
      │  stage 4   LLM disambiguation over survivors     (the only routing LLM)
      ▼
    merge per-sub-query selections → one plan DAG
      ▼
    RouteResult — a plan DAG, or a non-routed outcome (→ boundary responder)

Most of the funnel is deterministic; the LLM sees only an already-narrowed,
already-eligible candidate set. No stage consults a phrase catalogue — stage 3
evaluates the registry's declarative `ActivationCondition` and the P4 ABAC
rules. A compound message fans out into sub-queries that each route on their
own; the plan stitches them with dependency edges.

Non-routed outcomes are explicit, never silent:
  * `NO_CONFIDENT_MATCH` — nothing routed → the boundary responder answers.
  * `POLICY_DENIED`      — a candidate was eliminated by an ABAC denial → the
    boundary responder voices the refusal.
A compound message where *some* sub-queries route stays `ROUTED`; the parts
that did not route are reported in `RouteResult.unrouted`.
"""
from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from oneops.authz.descriptors import from_agent_record
from oneops.authz.models import Principal
from oneops.authz.service import AuthzService
from oneops.observability import get_logger, get_tracer, set_langfuse_io
from oneops.observability.metrics import increment
from oneops.registry.service import RegistryService
from oneops.router.conditions import evaluate, survives_filter
from oneops.router.decompose import Decomposer, PassthroughDecomposer
from oneops.router.disambiguation import Disambiguator
from oneops.router.glossary import Glossary
from oneops.router.plan import RouteResult, SubQueryRoute, assemble_plan
from oneops.router.retrieval import CandidateRetriever
from oneops.router.rewrite import ConversationTurn, PassthroughRewriter, Rewriter
from oneops.router.signals import RequestSignals, Ternary, with_intents

_log = get_logger("oneops.router")
_tracer = get_tracer("oneops.router")

DEFAULT_TOP_K = 10


def parallel_embed_enabled() -> bool:
    """Latency flag (default OFF). When ON, the router pre-warms the query
    embedding CONCURRENTLY with the decompose/split LLM call — both read only
    the raw message, so the embed round-trip overlaps the split instead of
    running sequentially after it (Stage-2 retrieve then hits the warm
    embedding cache). Structure- and quality-preserving: NO stage is removed,
    NO prompt changes, NO routing decision changes — only the scheduling.
    Set ONEOPS_ROUTER_PARALLEL_EMBED to 1/true/yes/on to enable."""
    return os.getenv("ONEOPS_ROUTER_PARALLEL_EMBED", "0").strip().lower() in (
        "1", "true", "yes", "on")


@dataclass
class _FunnelOutcome:
    """The funnel result for one sub-query — either a routed selection or a
    reason it did not route (`agent_ids` empty)."""

    agent_ids: list[str]
    parameters_by_agent: dict[str, dict[str, str]]
    reason: str
    policy_denied: bool


class Router:
    """Drives the routing funnel and emits a `RouteResult`."""

    def __init__(
        self,
        registry: RegistryService,
        glossary: Glossary,
        retriever: CandidateRetriever,
        disambiguator: Disambiguator,
        authz: AuthzService,
        *,
        decomposer: Decomposer | None = None,
        rewriter: Rewriter | None = None,
        unified_splitter: Any | None = None,
        top_k: int = DEFAULT_TOP_K,
        route_cache: Any | None = None,
    ) -> None:
        self._registry = registry
        self._glossary = glossary
        self._retriever = retriever
        self._disambiguator = disambiguator
        self._authz = authz
        # Decomposer/rewriter default to the deterministic passthrough — a
        # single self-contained sub-query, the common case, needing no LLM.
        self._decomposer = decomposer or PassthroughDecomposer()
        self._rewriter = rewriter or PassthroughRewriter()
        # Latency: when an LlmUnifiedSplitter is injected (flag on), ONE call
        # does reference-resolution + splitting, replacing decompose + the
        # speculative rewrite. None ⇒ the two-call path (default, unchanged).
        self._unified_splitter = unified_splitter
        self._top_k = top_k
        # Route-decision cache (router/route_cache.py). None ⇒ disabled (no
        # behaviour change). When set, a hit returns the funnel verdict without
        # running decompose+rewrite+disambiguate; the plan is rebuilt fresh via
        # assemble_plan and still EXECUTED fresh downstream, so data is current.
        self._route_cache = route_cache

    async def route(
        self,
        query_text: str,
        *,
        principal: Principal,
        signals: RequestSignals,
        conversation_history: list[ConversationTurn] | None = None,
        request_ctx: dict | None = None,
    ) -> RouteResult:
        request_ctx = request_ctx or {}
        history = conversation_history or []
        diag: list[str] = []

        with _tracer.start_as_current_span(
            "router.route",
            attributes={"oneops.tenant_id": principal.tenant_id,
                        "oneops.user_id": getattr(principal, "user_id", "") or "",
                        "router.role": principal.role},
        ) as span:
            if not query_text or not query_text.strip():
                return RouteResult.no_match("empty query", ["stage0: empty query"])

            # ── Route-decision cache lookup (before any LLM) ─────────────
            # The route is the most cross-session-stable thing in the system.
            # On a hit we skip decompose+rewrite+disambiguate (3 LLM calls)
            # and rebuild the plan deterministically — execution still runs
            # fresh downstream, so the answer is current. Key = normalized
            # query + routing signals + focus + domain + role + conversation
            # digest + registry fingerprint (every input the funnel reads).
            cache_key = self._route_cache_key(query_text, principal, signals,
                                              history, request_ctx)
            cached = await self._route_cache_lookup(cache_key, principal, span)
            if cached is not None:
                return cached

            # ── Speculative embed pre-warm (flag-gated) ──────────────────
            # The Stage-2 retrieve embed and the decompose/split LLM call both
            # read ONLY the raw message — independent work that runs
            # sequentially today. Fire the embed NOW so it overlaps the split;
            # retrieve() then hits the warm embedding cache. Pure scheduling
            # change — same stages, same retrieval, same routing decisions.
            import asyncio as _asyncio
            prewarm_task = None
            if (parallel_embed_enabled()
                    and hasattr(self._retriever, "prewarm_embed")):
                prewarm_task = _asyncio.create_task(
                    self._retriever.prewarm_embed(
                        query_text, tenant_id=principal.tenant_id))

            # ── Stage 0a + 0b — speculative parallel execution ───────────
            # Decompose and rewrite are both LLM calls (~1.2-1.5s each).
            # Single-sub-query messages (the overwhelming common case) get
            # rewriter input identical to decompose's only output — so we
            # speculatively kick off rewrite on the WHOLE message in
            # parallel with decompose. If decompose returns 1 sub-query
            # whose text equals the original message, we use the speculative
            # rewrite (saves ~1.3s). If it returns multi-sub-query or a
            # mutated single sub-query, we fall back to per-sub-query
            # rewrites and discard the speculative result.
            if self._unified_splitter is not None:
                # ── Merged path (one LLM call) ───────────────────────────
                # Reference-resolution + splitting in ONE round-trip. The
                # returned sub-queries are already self-contained, so the
                # per-sub-query rewrite below becomes a passthrough (no second
                # LLM call, no wasted speculative rewrite). RCA 2026-06-09.
                subqueries = await self._unified_splitter.split(
                    query_text, history=history, request_ctx=request_ctx)
                span.set_attribute("router.subquery_count", len(subqueries))
                span.set_attribute("router.unified_split", True)
                diag.append(
                    f"stage0: unified-split into {len(subqueries)} "
                    f"sub-query(ies) (refs resolved in one call)")
                spec_rewrite_task = None
                spec_usable = False
            else:
                # ── Two-call path (default) — speculative parallel ───────
                # Decompose and rewrite are both LLM calls (~1.2-1.5s each).
                # Single-sub-query messages (the common case) get rewriter
                # input identical to decompose's only output — so we
                # speculatively kick off rewrite on the WHOLE message in
                # parallel with decompose, using it when decompose returns 1
                # unmutated sub-query, else discarding it for per-sub rewrites.
                decompose_task = _asyncio.create_task(
                    self._decomposer.decompose(
                        query_text, request_ctx=request_ctx))
                spec_rewrite_task = _asyncio.create_task(
                    self._rewriter.rewrite(
                        query_text, history=history, request_ctx=request_ctx))
                subqueries = await decompose_task
                span.set_attribute("router.subquery_count", len(subqueries))
                diag.append(
                    f"stage0a: decomposed into {len(subqueries)} sub-query(ies)")
                spec_usable = (
                    len(subqueries) == 1
                    and subqueries[0].text.strip() == (query_text or "").strip()
                )
                await self._cancel_speculative(
                    spec_rewrite_task, spec_usable, span)

            # Join the speculative embed pre-warm (ran concurrently with the
            # split above). The embedding cache is now warm, so the per-
            # sub-query Stage-2 retrieve embed is a cache hit instead of a
            # fresh round-trip. Awaiting here costs ~max(split, embed) total
            # instead of split+embed sequential — it never adds latency vs the
            # flag-off path (the task started before the split). Best-effort.
            if prewarm_task is not None:
                with suppress(Exception):
                    await prewarm_task
                span.set_attribute("router.parallel_embed", True)

            routes: list[SubQueryRoute] = []
            unrouted: list[str] = []
            fail_reasons: list[str] = []
            any_policy_denied = False

            for sq in subqueries:
                outcome = await self._route_subquery(
                    sq, spec_rewrite_task=spec_rewrite_task,
                    spec_usable=spec_usable, signals=signals,
                    request_ctx=request_ctx, history=history,
                    diag=diag, principal=principal)

                if outcome.agent_ids:
                    routes.append(SubQueryRoute(
                        sub_query_id=sq.id,
                        agent_ids=outcome.agent_ids,
                        parameters_by_agent=outcome.parameters_by_agent,
                        depends_on_subqueries=list(sq.depends_on),
                        bindings=list(sq.bindings),
                    ))
                else:
                    unrouted.append(sq.text)
                    fail_reasons.append(outcome.reason)
                    any_policy_denied = any_policy_denied or outcome.policy_denied
                    diag.append(f"[{sq.id}] unrouted: {outcome.reason}")

            return await self._finalize_route(
                routes=routes, unrouted=unrouted, fail_reasons=fail_reasons,
                any_policy_denied=any_policy_denied, subqueries=subqueries,
                cache_key=cache_key, principal=principal, span=span,
                diag=diag, query_text=query_text)

    # ── route-decision cache helpers ─────────────────────────────────────

    async def _cancel_speculative(
        self, spec_rewrite_task: Any, spec_usable: bool, span: Any,
    ) -> None:
        """Resolve the speculative whole-message rewrite. Usable (single
        sub-query == whole message) → keep it. Otherwise cancel it and swallow
        the teardown CancelledError — but re-raise if THIS coroutine is itself
        being cancelled, so the turn aborts cleanly (S7497)."""
        import asyncio
        if spec_usable:
            span.set_attribute("router.spec_rewrite_used", True)
            return
        spec_rewrite_task.cancel()
        try:
            await spec_rewrite_task
        except asyncio.CancelledError:
            ct = asyncio.current_task()
            if ct is not None and ct.cancelling():
                raise
        except Exception:                                # noqa: BLE001
            pass
        span.set_attribute("router.spec_rewrite_used", False)

    async def _finalize_route(
        self, *, routes: list, unrouted: list, fail_reasons: list,
        any_policy_denied: bool, subqueries: list, cache_key: str | None,
        principal: Principal, span: Any, diag: list[str], query_text: str,
    ) -> RouteResult:
        """Merge the per-sub-query outcomes into the final RouteResult: cache +
        return the no_match/policy_denied verdict when nothing routed, else
        assemble the plan and cache the routed decision."""
        if not routes:
            # Single sub-query → surface its specific reason (precise boundary
            # response); several → summarise.
            if len(subqueries) == 1 and fail_reasons:
                reason = fail_reasons[0]
            else:
                reason = "no sub-query produced a confident route"
            outcome = "policy_denied" if any_policy_denied else "no_match"
            await self._route_cache_store(
                cache_key, principal, outcome=outcome,
                routes=[], unrouted=[], reason=reason)
            if any_policy_denied:
                return RouteResult.policy_denied(reason, diag)
            return RouteResult.no_match(reason, diag)

        await self._route_cache_store(
            cache_key, principal, outcome="routed",
            routes=routes, unrouted=unrouted, reason="")
        plan = assemble_plan(routes, self._registry)
        span.set_attribute("router.plan_steps", len(plan.steps))
        span.set_attribute("router.unrouted", len(unrouted))
        diag.append(f"plan: {len(plan.steps)} step(s) -> {list(plan.agent_ids)}")
        set_langfuse_io(
            span, input=query_text,
            output={"agents": list(plan.agent_ids),
                    "steps": len(plan.steps), "unrouted": len(unrouted)})
        return RouteResult.routed(plan, diag, unrouted)

    async def _route_subquery(
        self, sq: Any, *, spec_rewrite_task: Any, spec_usable: bool,
        signals: RequestSignals, request_ctx: dict, history: list,
        diag: list[str], principal: Principal,
    ) -> Any:
        """Process one sub-query: rewrite (speculative whole-message result when
        usable, else per-sub-query), scope signals to the sub-query's own
        entities/focus, then run the funnel. Returns the funnel outcome."""
        if self._unified_splitter is not None:
            # Merged path — the splitter already resolved references into
            # sq.text; a second rewrite call would be redundant (and an extra
            # LLM round-trip). Pass the text through unchanged.
            from oneops.router.rewrite import RewriteResult
            rewrite = RewriteResult.unchanged(sq.text)
        elif spec_usable:
            rewrite = await spec_rewrite_task
        else:
            rewrite = await self._rewriter.rewrite(
                sq.text, history=history, request_ctx=request_ctx)
        sq_signals = self._scope_subquery_signals(
            sq, rewrite, signals, request_ctx, history, diag)
        if rewrite.changed:
            diag.append(f"[{sq.id}] stage0b: rewritten -> {rewrite.text!r}")
        return await self._funnel(
            rewrite.text, principal, sq_signals, request_ctx,
            sq.id, diag, original_text=sq.text)

    def _scope_subquery_signals(
        self, sq: Any, rewrite: Any, signals: RequestSignals,
        request_ctx: dict, history: list, diag: list[str],
    ) -> RequestSignals:
        """Scope routing signals to the entities THIS sub-query names
        (post-rewrite), so "summarize INC1 and INC2" doesn't bind every step to
        the first entity. When the sub-query names no entity, inject the
        LangGraph state focus as the authoritative subject (the structural fix
        that replaced rewriter/disambiguator focus heuristics); as a rare
        fallback, a history-scanned focus for explicit linked-record refs."""
        from dataclasses import replace as _replace

        from oneops.router.entity_id import EntityIdNormalizer
        normalizer = EntityIdNormalizer.from_registry_file()
        sq_text_for_extract = rewrite.text if rewrite.changed else sq.text
        sq_extracted = normalizer.extract(sq_text_for_extract)
        if sq_extracted.entities:
            sq_entities = tuple(
                (e.entity_id, e.service_id) for e in sq_extracted.entities)
            diag.append(
                f"[{sq.id}] stage0b: scoped entities → "
                f"{[e.entity_id for e in sq_extracted.entities]}")
            return _replace(signals, present_entities=sq_entities)

        state_focus_id = (request_ctx.get("focus_entity_id") or "").strip()
        state_focus_service = (request_ctx.get("focus_service_id") or "").strip()
        if state_focus_id and state_focus_service:
            diag.append(
                f"[{sq.id}] stage0b: focus-bound follow-up → "
                f"{state_focus_id} (state authoritative)")
            return _replace(
                signals,
                present_entities=((state_focus_id, state_focus_service),))
        if _references_linked_record(sq_text_for_extract):
            # Belt-and-braces fallback when state focus is somehow missing —
            # scan history. Only fires when state focus is empty (rare).
            focus = _extract_focus_from_history(history, normalizer)
            if focus is not None:
                diag.append(
                    f"[{sq.id}] stage0b: history-scan focus → "
                    f"{focus[0]} (state focus was empty)")
                return _replace(signals, present_entities=(focus,))
        return _replace(signals, present_entities=())

    async def _route_cache_lookup(
        self, cache_key: str | None, principal: Principal, span: Any,
    ) -> RouteResult | None:
        """Route-decision cache get → a rebuilt RouteResult on a usable hit,
        else None (cache disabled / miss / malformed entry). The plan is
        reassembled fresh against the current registry downstream; a cache
        failure must never break routing."""
        if self._route_cache is None or cache_key is None:
            return None
        try:
            hit = await self._route_cache.get(
                tenant_id=principal.tenant_id, key=cache_key)
        except Exception as exc:                          # noqa: BLE001
            _log.warning("router.route_cache_get_failed", error=str(exc)[:160])
            hit = None
        if hit is not None:
            result = self._result_from_cache(hit)
            if result is not None:
                increment("oneops.router.route_cache.hit")
                span.set_attribute("router.route_cache", "hit")
                return result
        increment("oneops.router.route_cache.miss")
        span.set_attribute("router.route_cache", "miss")
        return None

    def _route_cache_key(
        self, query_text: str, principal: Principal,
        signals: RequestSignals, history: list, request_ctx: dict,
    ) -> str | None:
        """Build the cache key from every input the funnel reads. Returns None
        (cache skipped) if the cache is disabled or key construction fails —
        never let caching break routing."""
        if self._route_cache is None:
            return None
        try:
            from oneops.router.route_cache import (
                conversation_digest,
                route_cache_key,
                signals_digest,
            )
            return route_cache_key(
                query=query_text,
                role=principal.role,
                domain=(request_ctx.get("domain") or ""),
                focus_entity_id=(request_ctx.get("focus_entity_id") or ""),
                focus_service_id=(request_ctx.get("focus_service_id") or ""),
                sig_digest=signals_digest(signals),
                conv_digest=conversation_digest(history),
                registry_fingerprint=self._registry.routing_fingerprint(),
            )
        except Exception as exc:                                   # noqa: BLE001
            _log.warning("router.route_cache_key_failed", error=str(exc)[:160])
            return None

    def _result_from_cache(self, hit: dict) -> RouteResult | None:
        """Rebuild a RouteResult from a cached decision. The plan is reassembled
        against the CURRENT registry (deterministic, no LLM). Returns None on a
        malformed entry so the caller falls through to a fresh route."""
        try:
            outcome = hit.get("outcome")
            reason = hit.get("reason") or ""
            diag = ["route-cache: hit"]
            if outcome == "routed":
                from oneops.router.route_cache import deserialize_routes
                routes = deserialize_routes(hit.get("routes") or [])
                if not routes:
                    return None
                plan = assemble_plan(routes, self._registry)
                return RouteResult.routed(plan, diag, list(hit.get("unrouted") or []))
            if outcome == "policy_denied":
                return RouteResult.policy_denied(reason, diag)
            if outcome == "no_match":
                return RouteResult.no_match(reason, diag)
        except Exception as exc:                                   # noqa: BLE001
            _log.warning("router.route_cache_rebuild_failed", error=str(exc)[:160])
        return None

    async def _route_cache_store(
        self, cache_key: str | None, principal: Principal, *,
        outcome: str, routes: list, unrouted: list, reason: str,
    ) -> None:
        """Persist the funnel verdict. Best-effort — a cache write must never
        break a successful route."""
        if self._route_cache is None or cache_key is None:
            return
        try:
            from oneops.router.route_cache import serialize_decision
            value = serialize_decision(
                outcome=outcome, routes=routes, unrouted=unrouted, reason=reason)
            await self._route_cache.put(
                tenant_id=principal.tenant_id, key=cache_key, value=value)
            increment("oneops.router.route_cache.store")
        except Exception as exc:                                   # noqa: BLE001
            _log.warning("router.route_cache_put_failed", error=str(exc)[:160])

    # ── the per-sub-query funnel (stages 1-4) ────────────────────────────

    async def _funnel(
        self,
        text: str,
        principal: Principal,
        signals: RequestSignals,
        request_ctx: dict,
        sq_id: str,
        diag: list[str],
        *,
        original_text: str | None = None,
    ) -> _FunnelOutcome:
        _FunnelOutcome([], {}, "", False)

        # Stage 1 — glossary normalization.
        normalized = self._glossary.normalize(text)

        # Stage 2 — semantic retrieval.
        candidates = await self._retriever.retrieve(
            normalized, tenant_id=principal.tenant_id, top_k=self._top_k)
        if not candidates:
            return _FunnelOutcome([], {}, "no candidates retrieved", False)

        # Stage 3 — condition + ABAC filter.
        # Routing admission is decided by the agent's activation_condition,
        # using three-valued logic (PASS / INDETERMINATE / FAIL). The
        # verdict per survivor is carried forward so Stage 4 can use the
        # PASS-vs-INDETERMINATE distinction as a tiebreaker when the LLM
        # disambiguator returns no_confident_match (a definite PASS beats
        # speculative INDETERMINATEs).
        #
        # History (2026-05-28): a redundant `abac_tags.service` intersection
        # pre-filter was removed because it pre-empted survives_filter and
        # produced false-negative routing for cross-service queries (the
        # UC-3 `search_kb_by_ticket` case — an "incident" entity sub-query
        # being correctly admissible to UC-3 via its `intent_in[kb_search]`
        # activation arm). The post-stage-4 intent-resolved re-check below
        # catches the FAIL-once-intent-known case (e.g. "summarize
        # PBM0003001" → UC-3 carried as INDETERMINATE, LLM classifies intent
        # as 'summary', re-eval drops UC-3).
        survivors_with_verdict, policy_denied_any = await self._stage3_filter(
            candidates, principal, signals, sq_id, diag)
        survivors = [c for c, _v in survivors_with_verdict]

        if not survivors:
            reason = ("every candidate was denied by access policy"
                      if policy_denied_any
                      else "no candidate passed the activation-condition filter")
            return _FunnelOutcome([], {}, reason, policy_denied_any)

        # Stage 3.5 — deterministic single-survivor shortcut.
        # When stage 3 narrows to exactly one candidate AND that candidate's
        # activation evaluates to a definite PASS (not just survived as
        # INDETERMINATE), there is nothing to disambiguate — skip the LLM.
        # When the sole survivor evaluated as INDETERMINATE (e.g. UC-3 with
        # an `intent_in` clause that the LLM hasn't classified yet), the
        # LLM disambiguator MUST run: it has the option to return no_match,
        # which is what should happen for OOS queries like "tell me a joke"
        # that incidentally only have UC-3 surviving via INDETERMINATE.
        # Skipping in that case lets every off-topic query silently route
        # to UC-3 — the 2026-05-27 routing-leak bug.
        # Single-survivor shortcut: route directly to the sole candidate,
        # regardless of PASS vs INDETERMINATE. OOS / greetings / jokes
        # are caught upstream by the Stage-1 control gate; by the time
        # only one agent survives stages 1-3, that agent IS the right
        # handler. Letting the disambiguator decline on a single-option
        # query is the bug that produced "Are you looking to create a
        # ticket or KB?" for `salesforce sync lag` (2026-05-27).
        # Stage 3.4 — deterministic preroute (X6, 2026-05-28). High-confidence
        # patterns the LLM disambiguator was found to mishandle (bare entity
        # id → UC-1; content-noun / knowledge-verb + entity → UC-3) are
        # routed here directly, BEFORE the single-survivor shortcut. Runs on
        # the rewritten text. Target agent must be a registered active agent
        # AND must have survived stages 1-3 (post-authz / post-activation).
        preroute_outcome = self._try_preroute(
            survivors, signals, text, normalized, original_text, sq_id, diag)
        if preroute_outcome is not None:
            return preroute_outcome

        if len(survivors) == 1:
            sole = survivors[0]
            diag.append(
                f"[{sq_id}] stage3.5: single survivor {sole.agent_id} (PASS) — "
                "skipping disambiguation")
            # `_chat_bind` threads the rewritten sub-query text as
            # `user_message`/`query` so UC handlers can pick their own
            # full-summary vs field-read path (UC-1 field_read branch,
            # ISS-016); the step runner's data-driven tool-picker uses the
            # parameter shape to choose the tool.
            bound = self._chat_bind(sole.agent_id, signals, text)
            return _FunnelOutcome([sole.agent_id], {sole.agent_id: bound},
                                  "", False)

        # Stage 4 — LLM disambiguation over survivors only.
        return await self._stage4_disambiguate(
            normalized, survivors, survivors_with_verdict, signals,
            request_ctx, text, sq_id, diag)

    async def _stage3_filter(
        self,
        candidates: list,
        principal: Principal,
        signals: RequestSignals,
        sq_id: str,
        diag: list[str],
    ) -> tuple[list[tuple[Any, Ternary]], bool]:
        """Stage 3 — activation-condition (three-valued) + ABAC filter.

        Returns the surviving candidates each paired with their PASS /
        INDETERMINATE verdict (carried forward as the Stage-4 tiebreaker),
        plus a flag recording whether any candidate was denied by policy
        (used to distinguish "denied" from "no match" in the no-survivor
        diagnostic)."""
        survivors_with_verdict: list[tuple[Any, Ternary]] = []
        policy_denied_any = False
        with _tracer.start_as_current_span(
            "router.stage3.filter",
            attributes={
                "oneops.router.stage": "3",
                "oneops.router.input_candidate_count": len(candidates),
            },
        ) as s3:
            for cand in candidates:
                agent = self._registry.agents.get_optional(cand.agent_id)
                if agent is None:
                    diag.append(f"[{sq_id}] stage3: drop {cand.agent_id} — no active record")
                    continue
                verdict = evaluate(agent.activation_condition, signals)
                if verdict is Ternary.FAIL:
                    diag.append(f"[{sq_id}] stage3: drop {cand.agent_id} — condition FAIL")
                    continue
                resource = from_agent_record(agent, resource_tenant_id=principal.tenant_id)
                decision = await self._authz.check(principal, resource)
                if not decision.allowed:
                    policy_denied_any = True
                    diag.append(f"[{sq_id}] stage3: deny {cand.agent_id} — "
                                f"ABAC: {'; '.join(decision.reasons)}")
                    continue
                survivors_with_verdict.append((cand, verdict))
            survivors = [c for c, _v in survivors_with_verdict]
            s3.set_attribute("oneops.router.survivor_count", len(survivors))
            s3.set_attribute("oneops.router.policy_denied", policy_denied_any)
            s3.set_attribute("oneops.router.survivor_ids",
                             ",".join(c.agent_id for c in survivors))
            set_langfuse_io(
                s3,
                input=[c.agent_id for c in candidates],
                output={"survivors": [c.agent_id for c in survivors],
                        "policy_denied": policy_denied_any})
        return survivors_with_verdict, policy_denied_any

    def _try_preroute(
        self,
        survivors: list,
        signals: RequestSignals,
        text: str,
        normalized: str,
        original_text: str | None,
        sq_id: str,
        diag: list[str],
    ) -> _FunnelOutcome | None:
        """Stage 3.4 — deterministic preroute (X6, 2026-05-28).

        High-confidence patterns the LLM disambiguator was found to
        mishandle (bare entity id → UC-1; content-noun / knowledge-verb +
        entity → UC-3) route here directly, BEFORE the single-survivor
        shortcut. Target must be a registered active agent that survived
        stages 1-3. Returns the routed outcome, or None when no pattern
        fires (the funnel then continues to the single-survivor shortcut /
        Stage 4)."""
        from oneops.router.disambiguation import _deterministic_preroute
        survivor_ids = {c.agent_id for c in survivors}
        # Evaluate preroute on the ORIGINAL user text (before the rewriter
        # canonicalized references). The rewriter can rewrite "what do we
        # know about INC0001001" into "summarize INC0001001" — which
        # destroys the user goal for routing. Routing decisions belong to
        # the router; the rewriter only resolves references. We fall back
        # to the normalized text when no original was threaded through.
        preroute_text = original_text if original_text else normalized
        with _tracer.start_as_current_span(
            "router.stage3.4.preroute",
            attributes={
                "oneops.router.stage": "3.4",
                "oneops.router.survivor_count": len(survivor_ids),
                "oneops.router.survivor_ids": ",".join(sorted(survivor_ids)),
            },
        ) as pre_span:
            pre_target = _deterministic_preroute(preroute_text, survivor_ids)
            pre_span.set_attribute(
                "oneops.router.preroute.fired", pre_target is not None)
            set_langfuse_io(
                pre_span, input=sorted(survivor_ids),
                output={"fired": pre_target is not None,
                        "target": pre_target[0] if pre_target else None})
            _log.info("router.stage3.4.preroute_check",
                      preroute_text=preroute_text,
                      normalized=normalized,
                      survivor_ids=sorted(survivor_ids),
                      preroute_target=pre_target[0] if pre_target else None)
            if pre_target is None:
                return None
            agent_id, _intent, _rationale = pre_target
            pre_span.set_attribute("oneops.router.preroute.target", agent_id)
            pre_span.set_attribute("oneops.router.preroute.rationale",
                                   _rationale)
            diag.append(
                f"[{sq_id}] stage3.4: preroute → {agent_id} ({_rationale})")
            bound = self._chat_bind(agent_id, signals, text)
            return _FunnelOutcome([agent_id], {agent_id: bound}, "", False)

    async def _stage4_disambiguate(
        self,
        normalized: str,
        survivors: list,
        survivors_with_verdict: list[tuple[Any, Ternary]],
        signals: RequestSignals,
        request_ctx: dict,
        text: str,
        sq_id: str,
        diag: list[str],
    ) -> _FunnelOutcome:
        """Stage 4 — LLM disambiguation over survivors only, with the
        PASS-vs-INDETERMINATE tiebreaker and the post-stage-4 intent-
        resolved re-check.

        When the disambiguator declines but exactly one survivor was a
        definite PASS, route to it (PASS > INDETERMINATE). Otherwise bind
        the chosen agents' parameters and return them."""
        result = await self._disambiguator.disambiguate(
            normalized, survivors, request_ctx=request_ctx)
        if not result.is_confident_match:
            return self._declined_outcome(
                result, survivors_with_verdict, signals, text, sq_id, diag)

        chosen = self._resolve_chosen(result, signals, sq_id, diag)
        if not chosen:
            return _FunnelOutcome(
                [], {}, "selected agent(s) failed the intent-resolved check", False)
        params = self._bind_chosen_params(chosen, result, signals, text)
        return _FunnelOutcome(chosen, params, "", False)

    def _chat_bind(
        self, agent_id: str, signals: RequestSignals, text: str,
        base: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Bind a chosen agent's chat-path parameters: auto-bind present
        entities into the fast-path shape (LLM-set `base` params win), then
        thread the raw user text as `user_message`/`query` so handlers can
        pick their own field-read vs full path. Returns a fresh dict."""
        agent = self._registry.agents.get_optional(agent_id)
        bound = _bind_entities_to_fast_path(
            agent, dict(base or {}), signals.present_entities, self._registry)
        if text:
            bound = dict(bound)
            bound["user_message"] = text
            bound.setdefault("query", text)
        return bound

    def _declined_outcome(
        self, result: Any, survivors_with_verdict: list[tuple[Any, Ternary]],
        signals: RequestSignals, text: str, sq_id: str, diag: list[str],
    ) -> _FunnelOutcome:
        """Disambiguator declined. Tiebreaker (fix B, 2026-05-28): when Stage
        3 had exactly one definite PASS candidate (others INDETERMINATE),
        route to it — PASS > INDETERMINATE under the three-valued logic.
        This recovers multi-turn field-reads ("who is it assigned to?") that
        otherwise returned clarification after the abac_tags.service
        pre-filter removal (Issue 2). Else: no confident match."""
        pass_candidates = [c for c, v in survivors_with_verdict
                           if v is Ternary.PASS]
        if len(pass_candidates) == 1:
            sole = pass_candidates[0]
            diag.append(
                f"[{sq_id}] stage4-tiebreaker: disambiguator declined, "
                f"routing to sole PASS candidate {sole.agent_id} "
                f"(other survivors were INDETERMINATE)")
            bound = self._chat_bind(sole.agent_id, signals, text)
            return _FunnelOutcome([sole.agent_id], {sole.agent_id: bound},
                                  "", False)
        return _FunnelOutcome(
            [], {}, result.rationale or "no confident match", False)

    def _resolve_chosen(
        self, result: Any, signals: RequestSignals,
        sq_id: str, diag: list[str],
    ) -> list[str]:
        """Post-stage-4 guard: re-evaluate activation conditions with the
        now-classified intent, dropping any selected agent that no longer
        survives. Pass-through when the disambiguator set no intents."""
        if not result.intents:
            return list(result.selected_agent_ids)
        resolved = with_intents(signals, frozenset(result.intents))
        chosen: list[str] = []
        for agent_id in result.selected_agent_ids:
            agent = self._registry.agents.get_optional(agent_id)
            if agent is None or not survives_filter(agent.activation_condition, resolved):
                diag.append(f"[{sq_id}] stage4-guard: drop {agent_id} — "
                            "condition FAIL under classified intent")
                continue
            chosen.append(agent_id)
        return chosen

    def _bind_chosen_params(
        self, chosen: list[str], result: Any,
        signals: RequestSignals, text: str,
    ) -> dict[str, dict[str, str]]:
        """Bind each chosen agent's parameters. The disambiguator's
        `parameters_by_agent` win; required-but-empty fields auto-bind from
        present entities so chat ("summarize INC0001015") behaves
        identically to the button path ({ticket_id: INC0001015})."""
        params: dict[str, dict[str, str]] = {}
        for aid in chosen:
            params[aid] = self._chat_bind(
                aid, signals, text, base=result.params_for(aid))
        return params


# Entity-shaped parameter names — the closed set of registry-recognised
# fields that name a structured entity reference. Single source of truth
# used by both the binder (here) and the tool picker (step_runner). Adding
# a new entity-shape (e.g. `widget_id`) is a one-line entry, not a code
# search-and-replace.
_ENTITY_FIELD_NAMES = frozenset({
    "ticket_id", "article_id", "entity_id",
    "incident_id", "request_id", "problem_id",
    "change_id", "asset_id", "ci_id", "kb_id",
})

# Per-parameter-name accept lists: which entity service_ids a given
# parameter is willing to bind. Declarative, registry-shape rule —
# captures the documented intent of each parameter (e.g. `ticket_id`
# is by convention a cross-service work-record reference accepted by
# linked-lookup tools like `search_kb_by_ticket`).
_PARAM_ACCEPTS: dict[str, frozenset[str]] = {
    "ticket_id":   frozenset({"incident", "request", "problem", "change",
                               "asset", "cmdb_ci"}),
    "incident_id": frozenset({"incident"}),
    "request_id":  frozenset({"request"}),
    "problem_id":  frozenset({"problem"}),
    "change_id":   frozenset({"change"}),
    "asset_id":    frozenset({"asset"}),
    "ci_id":       frozenset({"cmdb_ci", "asset"}),
    "article_id":  frozenset({"knowledge"}),
    "kb_id":       frozenset({"knowledge"}),
    "entity_id":   frozenset({"incident", "request", "problem", "change",
                               "asset", "cmdb_ci", "knowledge"}),
}


_LINKED_REF_RE = __import__("re").compile(
    r"\b(?:the|its)\s+(?:linked|related|affected|parent|child)\s+"
    r"(?:problem|change|incident|request|ci|cmdb[\s_-]?ci|asset|kb|article|ticket|record)\b",
    __import__("re").IGNORECASE,
)


def _references_linked_record(text: str) -> bool:
    """True iff the text references a linked-record relationship that needs
    focus context to resolve (e.g. 'the linked problem', 'the affected CI',
    'its parent change'). Conservative regex; only fires on explicit
    relation + record-type pairings."""
    if not text:
        return False
    return bool(_LINKED_REF_RE.search(text))


def _extract_focus_from_history(history, normalizer) -> tuple[str, str] | None:
    """Most recent entity id mentioned in conversation history, as
    (entity_id, service_id). Scans the prior user turns latest-first;
    skips assistant text to avoid pulling an entity the assistant
    mentioned but the user has not adopted as focus."""
    for turn in reversed(list(history or [])):
        if getattr(turn, "role", "") != "user":
            continue
        content = getattr(turn, "content", "") or ""
        extracted = normalizer.extract(content)
        if extracted.entities:
            e = extracted.entities[0]
            return (e.entity_id, e.service_id)
    return None


def _bind_entities_to_fast_path(
    agent: Any, existing_params: dict[str, str],
    present_entities: tuple[tuple[str, str], ...],
    registry: Any = None,
) -> dict[str, str]:
    """Bind extracted entities to the chat-path parameter shape this agent
    will consume.

    Two passes, both data-driven:

    1. **Fast-path input fields** (existing button path). For each field
       declared on `agent.fast_path.input_fields`, bind a service-compatible
       entity. Compatibility is `entity.service_id in agent.abac_tags.service`
       — preserves the original guard that prevents a problem id binding
       into UC-3's button-shape `article_id`.

    2. **Tool parameters** (chat path; added 2026-05-28). For each parameter
       across `agent.tool_refs` whose name appears in `_ENTITY_FIELD_NAMES`,
       bind any `present_entity` whose `service_id` is in
       `_PARAM_ACCEPTS[param_name]`. Compatibility is per-parameter (the
       tool's documented input contract), NOT per-agent-service — that's
       what makes UC-3's `search_kb_by_ticket` (param `ticket_id`) able to
       receive an `incident` entity, restoring the documented cross-service
       linked-lookup capability without re-introducing the Issue 2 false-
       negative routing pre-filter.

    The LLM disambiguator's `parameters_by_agent` wins — anything it
    explicitly set is preserved. This is a SAFETY NET that prevents the
    handler from saying "ticket id required" when the entity is right
    there in the message."""
    if agent is None:
        return existing_params
    if not present_entities:
        return existing_params

    out = dict(existing_params)
    _bind_fast_path_fields(agent, out, present_entities)
    _bind_tool_params(agent, out, present_entities, registry)
    return out


def _bind_fast_path_fields(
    agent: Any, out: dict[str, str],
    present_entities: tuple[tuple[str, str], ...],
) -> None:
    """Pass 1 (button path; service-gated). Bind the first service-
    compatible present entity into the agent's `fast_path.input_fields`,
    deriving non-entity fields via `auto_derive_from`. Mutates `out`;
    never overrides a value the LLM disambiguator already set."""
    if agent.fast_path is None:
        return
    agent_services = set(agent.abac_tags.service or ())
    compatible = _first_compatible_entity(present_entities, agent_services)
    if compatible is None:
        return
    entity_id, _entity_service = compatible
    for field in agent.fast_path.input_fields:
        _bind_field(field, out, entity_id)


def _first_compatible_entity(
    present_entities: tuple[tuple[str, str], ...],
    agent_services: set[str],
) -> tuple[str, str] | None:
    """First present entity whose service the agent serves (or any entity
    when the agent declares no service scope)."""
    for eid, esvc in present_entities:
        if not agent_services or esvc in agent_services:
            return (eid, esvc)
    return None


def _bind_field(field: Any, out: dict[str, str], entity_id: str) -> None:
    """Bind one fast-path input field: entity-shaped fields take the entity
    id; others derive via `auto_derive_from`. Never overrides a value the
    LLM disambiguator already set."""
    if field.name in out and out[field.name]:
        return                                    # LLM set it
    if field.name in _ENTITY_FIELD_NAMES:
        out[field.name] = entity_id
        return
    if field.auto_derive_from and field.auto_derive_from in out:
        derived = _derive_for_chat(field.name, out[field.auto_derive_from])
        if derived:
            out[field.name] = derived


def _bind_tool_params(
    agent: Any, out: dict[str, str],
    present_entities: tuple[tuple[str, str], ...],
    registry: Any,
) -> None:
    """Pass 2 (chat path; per-param accept-list). For each entity-shaped
    parameter across the agent's tool_refs, bind a present entity the
    parameter accepts (`_PARAM_ACCEPTS`). Tool parameter contracts live on
    the tool record, resolved lazily via the registry. Mutates `out`;
    idempotent — never overrides an already-bound value."""
    if registry is None:
        return
    for tref in (getattr(agent, "tool_refs", None) or ()):
        tool = _resolve_tool(registry, tref)
        if tool is None:
            continue
        for p in (tool.parameters or ()):
            _bind_tool_param(p, out, present_entities)


def _resolve_tool(registry: Any, tref: Any) -> Any:
    """Resolve a tool record from a tool_ref via the registry; None when the
    record is absent or lookup raises (binding is best-effort)."""
    try:
        return registry.tools.get_optional(tref.tool_id)
    except Exception:                                  # noqa: BLE001
        return None


def _bind_tool_param(
    p: Any, out: dict[str, str],
    present_entities: tuple[tuple[str, str], ...],
) -> None:
    """Bind one entity-shaped tool parameter to the first present entity its
    `_PARAM_ACCEPTS` list admits. No-op for non-entity or already-bound
    params."""
    pname = p.name
    if pname not in _ENTITY_FIELD_NAMES or out.get(pname):
        return
    accepts = _PARAM_ACCEPTS.get(pname, frozenset())
    for eid, esvc in present_entities:
        if not accepts or esvc in accepts:
            out[pname] = eid
            return


def _derive_for_chat(target_field: str, source_value: str) -> str | None:
    """Mirror of the fast-path dispatcher's derivation for the chat
    routing path. Uses the same registry-driven entity normalizer."""
    if target_field == "service_id" and source_value:
        from oneops.router.entity_id import EntityIdNormalizer
        normalizer = EntityIdNormalizer.from_registry_file()
        r = normalizer.normalize(source_value)
        if r.entity is not None:
            return r.entity.service_id
    return None


__all__ = ["Router", "DEFAULT_TOP_K"]
