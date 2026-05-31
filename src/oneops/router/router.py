"""Router — the routing funnel (ARCHITECTURE.md §3).

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

from dataclasses import dataclass
from typing import Any

from oneops.authz.descriptors import from_agent_record
from oneops.authz.models import Principal
from oneops.authz.service import AuthzService
from oneops.observability import get_logger, get_tracer
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
        top_k: int = DEFAULT_TOP_K,
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
        self._top_k = top_k

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
            import asyncio as _asyncio
            decompose_task = _asyncio.create_task(
                self._decomposer.decompose(query_text, request_ctx=request_ctx))
            spec_rewrite_task = _asyncio.create_task(
                self._rewriter.rewrite(
                    query_text, history=history, request_ctx=request_ctx))
            subqueries = await decompose_task
            span.set_attribute("router.subquery_count", len(subqueries))
            diag.append(f"stage0a: decomposed into {len(subqueries)} sub-query(ies)")
            spec_usable = (
                len(subqueries) == 1
                and subqueries[0].text.strip() == (query_text or "").strip()
            )
            if not spec_usable:
                spec_rewrite_task.cancel()
                try:
                    await spec_rewrite_task
                except _asyncio.CancelledError:
                    pass
                except Exception:                                # noqa: BLE001
                    pass
                span.set_attribute("router.spec_rewrite_used", False)
            else:
                span.set_attribute("router.spec_rewrite_used", True)

            routes: list[SubQueryRoute] = []
            unrouted: list[str] = []
            fail_reasons: list[str] = []
            any_policy_denied = False

            for sq in subqueries:
                # ── Stage 0b — rewrite (resolve references) ──────────────
                if spec_usable:
                    rewrite = await spec_rewrite_task
                else:
                    rewrite = await self._rewriter.rewrite(
                        sq.text, history=history, request_ctx=request_ctx)
                # Extract entities from THIS sub-query's text (post-rewrite
                # if applicable) and use that scoped list for routing +
                # binding. Without scoping, a message like "summarize
                # INC0001001 and INC0001002" produces N sub-queries that
                # all bind to the FIRST whole-message entity. Each
                # sub-query must see only the entities it itself names —
                # otherwise N plan steps run with N copies of the same
                # ticket_id.
                from dataclasses import replace as _replace

                from oneops.router.entity_id import EntityIdNormalizer
                normalizer = EntityIdNormalizer.from_registry_file()
                sq_text_for_extract = rewrite.text if rewrite.changed else sq.text
                sq_extracted = normalizer.extract(sq_text_for_extract)
                if sq_extracted.entities:
                    sq_entities = tuple(
                        (e.entity_id, e.service_id)
                        for e in sq_extracted.entities)
                    sq_signals = _replace(
                        signals, present_entities=sq_entities)
                    diag.append(
                        f"[{sq.id}] stage0b: scoped entities → "
                        f"{[e.entity_id for e in sq_extracted.entities]}")
                else:
                    # No entity in THIS sub-query text. STRUCTURAL FIX
                    # (Stage 2.5, 2026-05-28): the LangGraph state's
                    # focus is the authoritative active subject. When
                    # focus is set AND the sub-query is a focus-bound
                    # follow-up (no new entity in text, no explicit
                    # KB doc-noun), use the state focus as
                    # present_entities. This bypasses the rewriter and
                    # disambiguator's probabilistic choices for the
                    # common follow-up case — Stage 3 admits the focus's
                    # owner agent on a hard structural signal.
                    #
                    # Replaces three earlier heuristic layers:
                    #   • rewriter focus injection (LLM, drift-prone)
                    #   • disambiguator focus block (prompt addition)
                    #   • linked-record-only backstop (too narrow)
                    #
                    # When focus is empty (fresh session / no prior
                    # entity), fall through to the existing path — the
                    # disambiguator and boundary responder handle
                    # off-domain / no-context cases per existing rules.
                    sq_signals = _replace(signals, present_entities=())
                    state_focus_id = (
                        request_ctx.get("focus_entity_id") or ""
                    ).strip()
                    state_focus_service = (
                        request_ctx.get("focus_service_id") or ""
                    ).strip()
                    # Inject the state focus as present_entities for THIS
                    # sub-query when state focus is set and the sub-query
                    # itself names no entity. Stage 3 then admits both
                    # UC-1 and UC-3 as candidates with a concrete subject;
                    # the LLM disambiguator (with focus context in its
                    # prompt) decides which agent owns this turn. This
                    # matches V1's architecture: the LLM is the authority
                    # on intent; the state focus is the authority on the
                    # active subject. No keyword regex on the routing
                    # path — see [[poc5mw1-focus-state-channel-2026-05-28]].
                    if state_focus_id and state_focus_service:
                        sq_signals = _replace(
                            signals,
                            present_entities=((state_focus_id, state_focus_service),),
                        )
                        diag.append(
                            f"[{sq.id}] stage0b: focus-bound follow-up → "
                            f"{state_focus_id} (state authoritative)")
                    elif _references_linked_record(sq_text_for_extract):
                        # Belt-and-braces fallback for the linked-record
                        # case when state focus is somehow missing —
                        # scan history. Only fires when state focus is
                        # empty (rare).
                        focus = _extract_focus_from_history(
                            history, normalizer)
                        if focus is not None:
                            sq_signals = _replace(
                                signals, present_entities=(focus,))
                            diag.append(
                                f"[{sq.id}] stage0b: history-scan focus → "
                                f"{focus[0]} (state focus was empty)")
                if rewrite.changed:
                    diag.append(f"[{sq.id}] stage0b: rewritten -> {rewrite.text!r}")

                outcome = await self._funnel(
                    rewrite.text, principal, sq_signals, request_ctx,
                    sq.id, diag, original_text=sq.text)

                if outcome.agent_ids:
                    routes.append(SubQueryRoute(
                        sub_query_id=sq.id,
                        agent_ids=outcome.agent_ids,
                        parameters_by_agent=outcome.parameters_by_agent,
                        depends_on_subqueries=list(sq.depends_on),
                    ))
                else:
                    unrouted.append(sq.text)
                    fail_reasons.append(outcome.reason)
                    any_policy_denied = any_policy_denied or outcome.policy_denied
                    diag.append(f"[{sq.id}] unrouted: {outcome.reason}")

            # ── Merge ────────────────────────────────────────────────────
            if not routes:
                # With a single sub-query, surface its specific reason so the
                # boundary responder can be precise; with several, summarise.
                if len(subqueries) == 1 and fail_reasons:
                    reason = fail_reasons[0]
                else:
                    reason = "no sub-query produced a confident route"
                if any_policy_denied:
                    return RouteResult.policy_denied(reason, diag)
                return RouteResult.no_match(reason, diag)

            plan = assemble_plan(routes, self._registry)
            span.set_attribute("router.plan_steps", len(plan.steps))
            span.set_attribute("router.unrouted", len(unrouted))
            diag.append(f"plan: {len(plan.steps)} step(s) -> {list(plan.agent_ids)}")
            return RouteResult.routed(plan, diag, unrouted)

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
            _log.info("router.stage3.4.preroute_check",
                      preroute_text=preroute_text,
                      normalized=normalized,
                      survivor_ids=sorted(survivor_ids),
                      preroute_target=pre_target[0] if pre_target else None)
            if pre_target is not None:
                agent_id, _intent, _rationale = pre_target
                pre_span.set_attribute("oneops.router.preroute.target", agent_id)
                pre_span.set_attribute("oneops.router.preroute.rationale",
                                       _rationale)
                diag.append(
                    f"[{sq_id}] stage3.4: preroute → {agent_id} ({_rationale})")
                agent = self._registry.agents.get_optional(agent_id)
                bound = _bind_entities_to_fast_path(
                    agent, {}, signals.present_entities, self._registry)
                if text:
                    bound = dict(bound)
                    bound["user_message"] = text
                    bound.setdefault("query", text)
                return _FunnelOutcome([agent_id], {agent_id: bound}, "", False)

        if len(survivors) == 1:
            sole = survivors[0]
            diag.append(
                f"[{sq_id}] stage3.5: single survivor {sole.agent_id} (PASS) — "
                "skipping disambiguation")
            agent = self._registry.agents.get_optional(sole.agent_id)
            bound = _bind_entities_to_fast_path(
                agent, {}, signals.present_entities, self._registry)
            # Thread the rewritten sub-query text so UC handlers can
            # decide between full-summary and field-read paths on their
            # own (UC-1 internal field_read branch, ISS-016).
            if text:
                bound = dict(bound)
                bound["user_message"] = text
                # Generic chat → query binding. Tools that take a `query`
                # parameter (e.g. UC-3 search_kb) read this directly; tools
                # that don't need it ignore it. No UC-specific code here —
                # the step runner's data-driven tool-picker (step_runner._
                # pick_tool) uses the parameter shape to choose which tool
                # of the agent's tool_refs to invoke.
                bound.setdefault("query", text)
            return _FunnelOutcome([sole.agent_id], {sole.agent_id: bound},
                                  "", False)

        # Stage 4 — LLM disambiguation over survivors only.
        result = await self._disambiguator.disambiguate(
            normalized, survivors, request_ctx=request_ctx)
        if not result.is_confident_match:
            # Architectural tiebreaker (fix B, 2026-05-28): when the LLM
            # disambiguator declines but Stage 3 had exactly one definite
            # PASS candidate (with all other survivors as INDETERMINATE),
            # route to the PASS candidate. PASS > INDETERMINATE is the
            # natural fallback under the existing three-valued logic —
            # PASS means "activation_condition definitely admits this
            # query under current signals", INDETERMINATE means "might
            # admit pending intent classification". When the LLM cannot
            # decide between a definite admission and a speculative one,
            # honour the definite one. This fixes the regression that
            # surfaced when the abac_tags.service pre-filter was removed
            # (Issue 2): UC-3 now correctly survives stage 3 as
            # INDETERMINATE for ticket-entity queries, which made
            # multi-turn field-reads ("who is it assigned to?") return
            # clarification instead of routing to UC-1.
            pass_candidates = [c for c, v in survivors_with_verdict
                               if v is Ternary.PASS]
            if len(pass_candidates) == 1:
                sole = pass_candidates[0]
                diag.append(
                    f"[{sq_id}] stage4-tiebreaker: disambiguator declined, "
                    f"routing to sole PASS candidate {sole.agent_id} "
                    f"(other survivors were INDETERMINATE)")
                agent = self._registry.agents.get_optional(sole.agent_id)
                bound = _bind_entities_to_fast_path(
                    agent, {}, signals.present_entities, self._registry)
                if text:
                    bound = dict(bound)
                    bound["user_message"] = text
                    bound.setdefault("query", text)
                return _FunnelOutcome([sole.agent_id], {sole.agent_id: bound},
                                      "", False)
            return _FunnelOutcome(
                [], {}, result.rationale or "no confident match", False)

        # Post-stage-4 guard — re-evaluate conditions with the now-known intent.
        chosen: list[str] = []
        if result.intents:
            resolved = with_intents(signals, frozenset(result.intents))
            for agent_id in result.selected_agent_ids:
                agent = self._registry.agents.get_optional(agent_id)
                if agent is None or not survives_filter(agent.activation_condition, resolved):
                    diag.append(f"[{sq_id}] stage4-guard: drop {agent_id} — "
                                "condition FAIL under classified intent")
                    continue
                chosen.append(agent_id)
        else:
            chosen = list(result.selected_agent_ids)

        if not chosen:
            return _FunnelOutcome(
                [], {}, "selected agent(s) failed the intent-resolved check", False)

        # Bind extracted entities into each chosen agent's parameters. The
        # disambiguator may have set `parameters_by_agent` directly (LLM
        # path) — those win. For any required input field still empty, we
        # auto-bind from `signals.present_entities` using the same shape
        # the fast-path dispatcher uses (ticket_id ← first incident-ish
        # entity, etc.). This makes chat ("summarize INC0001015") behave
        # identically to button ({ticket_id: INC0001015}) — same handler,
        # same parameter shape.
        params: dict[str, dict[str, str]] = {}
        for aid in chosen:
            agent = self._registry.agents.get_optional(aid)
            from_llm = result.params_for(aid)
            bound = _bind_entities_to_fast_path(
                agent, dict(from_llm), signals.present_entities, self._registry)
            if text:
                bound["user_message"] = text
                bound.setdefault("query", text)
            params[aid] = bound
        return _FunnelOutcome(chosen, params, "", False)


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

    # Pass 1: fast-path input fields (button path; service-gated).
    if agent.fast_path is not None:
        agent_services = set(agent.abac_tags.service or ())
        compatible_entity = None
        for eid, esvc in present_entities:
            if not agent_services or esvc in agent_services:
                compatible_entity = (eid, esvc)
                break
        if compatible_entity is not None:
            entity_id, _entity_service = compatible_entity
            for field in agent.fast_path.input_fields:
                if field.name in out and out[field.name]:
                    continue                              # LLM set it
                if field.name in _ENTITY_FIELD_NAMES:
                    out[field.name] = entity_id
                    continue
                if field.auto_derive_from and field.auto_derive_from in out:
                    derived = _derive_for_chat(
                        field.name, out[field.auto_derive_from])
                    if derived:
                        out[field.name] = derived

    # Pass 2: tool parameters (chat path; per-param accept-list).
    # Iterate over each tool's parameters; for each entity-shaped param
    # name, bind a present entity that the param accepts. Idempotent —
    # never overrides an already-set value.
    for tref in (getattr(agent, "tool_refs", None) or ()):
        # Tool lookup via the registry. The agent's tool_refs carry tool_id
        # only; the parameter contract is on the tool record itself.
        # We resolve lazily because the agent record alone doesn't carry
        # parameter shapes — they live on tool registrations (registry/v2/tools).
        if registry is None:
            continue
        try:
            tool = registry.tools.get_optional(tref.tool_id)
        except Exception:                                  # noqa: BLE001
            tool = None
        if tool is None:
            continue
        for p in (tool.parameters or ()):
            pname = p.name
            if pname not in _ENTITY_FIELD_NAMES:
                continue
            if out.get(pname):
                continue                                  # already bound
            accepts = _PARAM_ACCEPTS.get(pname, frozenset())
            for eid, esvc in present_entities:
                if not accepts or esvc in accepts:
                    out[pname] = eid
                    break

    return out


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
