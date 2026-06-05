"""Executor graph nodes (P6).

The graph runs the router's plan DAG:

    route → [routed?] → wave ⇄ run_step (Send fan-out)  → aggregate → END
                      └ no  → boundary                  → END

`ExecutorNodes` holds the injected dependencies; its async methods are the
graph nodes. The conditional-edge functions are pure module functions (they
only read state — no I/O, no mutation).

Wave execution: `wave` is a no-op; `dispatch_wave` computes the next set of
runnable steps (every `depends_on` already has a result) and emits one `Send`
per step. Independent steps fan out together (parallel); dependent steps wait
for their wave. The loop ends when every step has a result.
"""
from __future__ import annotations

import os
import time
from typing import Any

from langgraph.types import Send, interrupt

from oneops.authz.models import Principal
from oneops.executor.hooks import HookContext, HookError, HookPhase, HookRegistry
from oneops.executor.memory import (
    ConversationTrimError,
    ConversationTrimmer,
    NoopTrimmer,
)
from oneops.executor.state import ExecutorState, serialise_plan
from oneops.executor.step_runner import StepExecutor, make_result
from oneops.observability import (
    get_logger,
    get_tracer,
    histogram,
    increment,
    set_langfuse_io,
)
from oneops.registry.models import ExecutionTier
from oneops.registry.service import RegistryService
from oneops.router.entity_id import EntityIdNormalizer
from oneops.router.plan import RouteOutcome
from oneops.router.rewrite import ConversationTurn
from oneops.router.router import Router
from oneops.router.signals import RequestSignals
from oneops.session.backend import ConversationEvent
from oneops.session.store import SessionEventStore

_log = get_logger("oneops.executor.nodes")
_tracer = get_tracer("oneops.executor.nodes")


# ── runtime step generation (dynamic fan-out) safety budget ───────────────
# A handler may discover at runtime that it must spawn follow-up work it could
# not know at plan time (e.g. a KG traversal returns N affected CIs, each
# needing its own read). It returns `generated_steps` from its result; the
# executor appends them to the plan channel and `dispatch_wave` picks them up
# in a later wave. The depth + width budgets keep generation from running away
# — a self-spawning handler can never loop forever or fan out without bound.
#
# These are runaway *safety backstops*, not decision logic — and they are NOT
# hardcoded. The platform-wide defaults come from env (operator-tunable per
# deployment), and any single step can override them by carrying `_gen_max_depth`
# / `_gen_max_width` (agents-as-data: a UC that legitimately needs a wider
# fan-out sets its own budget without a code change). Overrides propagate down
# the generated subtree so the whole chain honours the configured budget.
DEFAULT_MAX_GENERATION_DEPTH = int(os.getenv("ONEOPS_MAX_GENERATION_DEPTH", "3"))
DEFAULT_MAX_GENERATED_PER_STEP = int(os.getenv("ONEOPS_MAX_GENERATED_PER_STEP", "8"))


def _normalise_generated_steps(
    parent_step: dict[str, Any], raw_steps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Validate + namespace runtime-generated steps from a handler result.

    Returns `(steps, note)`: `steps` is the (possibly empty) list to append to
    the plan channel; `note` is a human-readable reason whenever anything was
    refused (depth/width budget, missing agent_id) — surfaced on the parent
    result and in the trace, never a silent drop (thumb rule #11).

    The effective budget is resolved dynamically: a per-step override
    (`_gen_max_depth` / `_gen_max_width`, set by the agent/handler) takes
    precedence over the env-configured platform default — nothing is hardcoded.
    Each generated step gets a globally-unique id namespaced under its parent
    (`<parent>.g<i>`), defaults `depends_on` to the parent (so generated work
    runs *after* the step that asked for it), carries `_gen_depth` for the next
    round's depth check, and inherits the budget overrides so the whole subtree
    stays within the same configured limits.
    """
    parent_id = parent_step.get("step_id", "step")
    depth = int(parent_step.get("_gen_depth", 0) or 0)
    max_depth = int(parent_step.get("_gen_max_depth")
                    or DEFAULT_MAX_GENERATION_DEPTH)
    max_width = int(parent_step.get("_gen_max_width")
                    or DEFAULT_MAX_GENERATED_PER_STEP)

    if depth >= max_depth:
        return [], (f"generation depth limit ({max_depth}) reached at "
                    f"'{parent_id}' — {len(raw_steps)} follow-up step(s) not spawned")

    note = ""
    capped = raw_steps[:max_width]
    if len(raw_steps) > max_width:
        note = (f"generation width limit ({max_width}) at '{parent_id}' "
                f"— {len(raw_steps) - max_width} step(s) dropped")

    # Carry budget overrides down the subtree (only when explicitly set, so the
    # env default still applies when no override was given).
    inherited: dict[str, Any] = {}
    if parent_step.get("_gen_max_depth") is not None:
        inherited["_gen_max_depth"] = parent_step["_gen_max_depth"]
    if parent_step.get("_gen_max_width") is not None:
        inherited["_gen_max_width"] = parent_step["_gen_max_width"]

    out: list[dict[str, Any]] = []
    for i, raw in enumerate(capped):
        agent_id = (raw or {}).get("agent_id")
        if not agent_id:
            note = (note + "; " if note else "") + "a generated step had no agent_id"
            continue
        out.append({
            "step_id": f"{parent_id}.g{i}",
            "agent_id": agent_id,
            "parameters": dict((raw or {}).get("parameters") or {}),
            "depends_on": list((raw or {}).get("depends_on") or [parent_id]),
            "_gen_depth": depth + 1,
            **inherited,
        })
    return out, note


# ── data-flow binding (previous_results) ──────────────────────────────────
# A step can consume an upstream step's RUNTIME output via declared bindings:
# `to_param ← from_step.output.<from_field>`. Resolution is a pure function of
# (step, the completed dependency results) — so it is deterministic and
# checkpoint-replay-safe (the dependency results live in the durable
# `step_results` channel; nothing derived is persisted). Policy-aligned with
# updated_policy_v2.md TEAM_COORDINATION / ORDERING (`previous_results`,
# `dependency_type: hard|soft`).


def dotted_get(obj: Any, path: str) -> Any:
    """Walk a dotted path into nested dicts / objects. Returns None if any
    segment is missing (never raises) — `output.summary.root_cause`,
    `affected_ci_ids`. Pure."""
    cur = obj
    for seg in str(path).split("."):
        if seg == "":
            return None
        if isinstance(cur, dict):
            if seg not in cur:
                return None
            cur = cur[seg]
        else:
            cur = getattr(cur, seg, None)
        if cur is None:
            return None
    return cur


def _resolve_bindings(
    step: dict[str, Any], previous_results: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], str, str]:
    """Resolve a step's declared `parameter_bindings` against its completed
    dependency results.

    Returns `(bound_inputs, status, reason)`:
      * `status == "ok"`  → `bound_inputs` is the `{to_param: value}` map to
        deliver to the handler (empty when the step declares no bindings).
      * `status == "blocked"` → a *hard* dependency did not succeed, or a
        *required* field was missing; `reason` is the user-safe explanation
        (surfaced, never silent — thumb rule #11).

    `dependency_type` (hard|soft) gates a failed upstream: hard → block; soft
    → proceed best-effort (the binding from that dep is simply omitted).
    """
    bindings = step.get("parameter_bindings") or []
    if not bindings:
        return {}, "ok", ""

    def dep_kind(dep_step_id: str) -> str:
        for pair in step.get("dependency_types") or []:
            if len(pair) == 2 and pair[0] == dep_step_id:
                return pair[1]
        return "hard"

    bound: dict[str, Any] = {}
    for b in bindings:
        from_step = b.get("from_step", "")
        from_field = b.get("from_field", "")
        to_param = b.get("to_param", "")
        required = b.get("required", True)
        prev = previous_results.get(from_step)
        kind = dep_kind(from_step)

        if prev is None or prev.get("status") != "success":
            if kind == "soft":
                continue                      # best-effort: omit this dep's value
            if required:
                why = ("did not succeed" if prev is not None
                       else "produced no result")
                return {}, "blocked", f"upstream step '{from_step}' {why}"
            continue                          # hard but optional binding → omit

        # Resolve the field against the producer's output: try the top level
        # first, then the `bindable` namespace (where producers expose their
        # record's dynamic fields). Flat from_field names work either way.
        output = prev.get("output")
        val = dotted_get(output, from_field)
        if val is None and isinstance(output, dict):
            val = dotted_get(output.get("bindable"), from_field)
        if val is None:
            if required:
                return ({}, "blocked",
                        f"required input '{to_param}' is missing — "
                        f"'{from_step}.{from_field}' was not in its output")
            continue                          # optional missing → omit + proceed
        bound[to_param] = val

    return bound, "ok", ""


def _envelope(state: dict[str, Any]) -> dict[str, Any]:
    env = {k: state.get(k, "") for k in
           ("request_id", "tenant_id", "session_id", "user_id", "role", "message")}
    # Carry the LangGraph focus channel into request_ctx so the router,
    # rewriter and downstream consumers can READ the authoritative focus
    # instead of re-deriving it from history strings (Stage 2 fix).
    env["focus_entity_id"] = state.get("focus_entity_id", "") or ""
    env["focus_service_id"] = state.get("focus_service_id", "") or ""
    # TimeFilter (serialised dict) — empty dict means "no filter requested".
    env["time_filter"] = state.get("time_filter", {}) or {}
    return env


# ── friendly response builder ───────────────────────────────────────────


# A short, neutral fallback for any failure we can't categorise. Kept under
# 12 words so it never reads alarming; details live in the trace, not the
# response.
_GENERIC_FAILURE = "I wasn't able to complete that request."


def _extract_entity_id(step: dict[str, Any], result: dict[str, Any]) -> str:
    """Best-effort: pick the canonical id the step was acting on so denial
    / not-found messages can name it. Looks at step parameters first, then
    falls back to the handler's structured output."""
    params = step.get("parameters") or {}
    for key in ("ticket_id", "entity_id", "article_id", "kb_id",
                "incident_id", "request_id", "problem_id",
                "change_id", "asset_id", "ci_id"):
        if params.get(key):
            return str(params[key])
    output = result.get("output") or {}
    if isinstance(output, dict):
        for key in ("ticket_id", "entity_id", "article_id",
                    "incident_id", "request_id", "problem_id",
                    "change_id", "asset_id", "ci_id"):
            if output.get(key):
                return str(output[key])
    return ""


def friendly_step_response(
    step: dict[str, Any], result: dict[str, Any],
) -> str:
    """Render one step's result as user-facing text. Decisions in order:

      1. SUCCESS + the handler returned a `display_text` field (canonical
         "chat-ready text" contract — any UC tool may emit it; the executor
         surfaces it verbatim). This is the preferred output for UCs whose
         response shape is opinionated (e.g. UC-2 spec-formatted similar-
         tickets list with per-result flags + "Common: …" prose).
      2. SUCCESS + the handler returned a `summary` block (LLM summariser
         output `{"summary": "...paragraph...", "key_details": {...}, ...}`)
         → return the paragraph.
      3. SUCCESS + the handler returned a structured `outcome` + `message`
         (e.g. `outcome="not_found"` from `summarize_entity`) → return that
         already-friendly message.
      4. FAILED + the error string identifies an authz_recheck deny
         → "Your role doesn't allow you to read {entity_id}."
      5. FAILED + LLM gateway exhaustion → "summarisation service
         temporarily unavailable" message.
      6. FAILED + asyncio timeout → ask the user to retry.
      7. Otherwise → a short generic failure line, no internals leaked.

    The returned string is the substance the caller renders. The aggregate
    node joins multi-step turns with blank lines.

    Tool-output contract for `display_text`:
      • Must be a non-empty `str`.
      • Caller owns formatting — Markdown is rendered by the chat UI.
      • When present, takes precedence over `summary` and `message` so a UC
        whose result has all three fields renders the spec-defined output.
      • Empty / whitespace-only `display_text` is ignored (falls through to
        the older paths) so a buggy renderer can't blank a chat reply.
    """
    status = (result.get("status") or "").lower()
    output = result.get("output") or {}
    error = (result.get("error") or "")
    entity_id = _extract_entity_id(step, result)

    # ── success path ────────────────────────────────────────────────────
    if status == "success":
        if isinstance(output, dict):
            # 1. Canonical chat-ready text — used by UCs whose spec dictates
            #    an opinionated output shape (UC-2 ranked list with flags).
            display_text = output.get("display_text")
            if isinstance(display_text, str) and display_text.strip():
                return display_text.strip()
            # 2. UC-1 summariser path.
            outer_summary = output.get("summary")
            if isinstance(outer_summary, dict):
                paragraph = str(outer_summary.get("summary") or "").strip()
                if paragraph:
                    return paragraph
            elif isinstance(outer_summary, str) and outer_summary.strip():
                return outer_summary.strip()
            # 3. Handlers that emit `outcome` + already-friendly `message`.
            message = output.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        # A success step with no surfaceable text — short ack, never empty.
        return "Done."

    # ── blocked path (data-flow dependency could not be satisfied) ──────
    # A step is `blocked` when a required upstream output was missing or a
    # hard dependency did not succeed. Surface the reason plainly — the user
    # learns what could not run and why (no fabrication, no silent skip).
    if status == "blocked":
        reason = error.strip()
        return reason or ("This step was skipped because a prerequisite "
                          "did not complete.")

    # ── failure / denial paths ─────────────────────────────────────────
    lowered = error.lower()
    if "hookerror" in lowered or "authz_recheck" in lowered or \
       "before-hook aborted" in lowered:
        if entity_id:
            return f"Your role doesn't allow you to read {entity_id}."
        return "Your role doesn't allow that action."
    if "llmgatewayerror" in lowered or "llm call failed" in lowered or \
       "llm_unavailable" in lowered:
        return ("The summarisation service is temporarily unavailable. "
                "Please try again in a moment.")
    if "timed out" in lowered or "timeout" in lowered:
        return "That request took too long to complete. Please try again."
    if "not_found" in lowered and entity_id:
        return f"I couldn't find {entity_id} in this tenant's records."
    return _GENERIC_FAILURE


class ExecutorNodes:
    """The graph's node implementations, with dependencies injected once."""

    def __init__(
        self,
        router: Router,
        registry: RegistryService,
        step_executor: StepExecutor,
        hooks: HookRegistry,
        boundary,                       # BoundaryResponder
        session_store: SessionEventStore | None = None,
        policy_engine=None,             # PolicyEngine | None
        authz_service=None,             # AuthzService | None — injected into
                                        # HookContext.services for the builtin
                                        # authz_recheck hook (substrate gap G5)
        conversation_trimmer: ConversationTrimmer | None = None,
                                        # Bounds conversation_history before
                                        # router/state propagate it (substrate
                                        # gap G2). Default = NoopTrimmer
                                        # (current behavior preserved).
        focus_intent_classifier=None,   # FocusIntentClassifier | None — when
                                        # provided, runs in update_focus to
                                        # drop focus on explicit topic-search
                                        # turns. When None, focus carries as
                                        # before (legacy behaviour preserved).
        time_filter_extractor=None,     # TimeFilterExtractor | None — when
                                        # wired, the route node runs it
                                        # conditionally (only when the plan
                                        # contains an agent whose registry
                                        # record sets consumes_time_filter:
                                        # true). When None, no extraction
                                        # occurs and tools see an empty filter.
    ) -> None:
        self._router = router
        self._registry = registry
        self._step_executor = step_executor
        self._hooks = hooks
        self._boundary = boundary
        # Conversational memory. When None, the turn is stateless — load gives
        # an empty history and persist is a no-op.
        self._session_store = session_store
        # Policy engine (P10). When None, no policy gate runs.
        self._policy_engine = policy_engine
        # AuthZ service for `builtin:authz_recheck`. When None, the hook is
        # unwired and any agent that declares it fails loud (never silent).
        self._authz_service = authz_service
        # Conversation memory trimmer (substrate gap G2). Defaults to a
        # passthrough so non-FaaS deployments preserve today's behavior.
        self._trimmer: ConversationTrimmer = conversation_trimmer or NoopTrimmer()
        # System-wide entity-ID normalizer (registry-driven). Built once.
        self._entity_normalizer = EntityIdNormalizer.from_registry_file()
        # Focus-intent classifier (optional). When wired, the update_focus
        # node calls it on each turn with a carried focus + no current entity
        # to decide if the focus should be dropped (explicit topic-search).
        self._focus_intent_classifier = focus_intent_classifier
        self._time_filter_extractor = time_filter_extractor

    # ── load_session ─────────────────────────────────────────────────────

    async def load_session(self, state: ExecutorState) -> dict[str, Any]:
        """Load the recent conversation history for this session so the router
        can resolve references ("close it", "same as last time") against it."""
        if self._session_store is None:
            return {"conversation_history": []}
        tenant_id = state.get("tenant_id", "")
        session_id = state.get("session_id", "")
        if not tenant_id or not session_id:
            return {"conversation_history": []}
        with _tracer.start_as_current_span(
            "executor.load_session",
            attributes={"oneops.tenant_id": tenant_id,
                        "oneops.user_id": state.get("user_id", ""),
                        "session.id": session_id},
        ) as span:
            events = await self._session_store.recent(tenant_id, session_id)
            history = [{"role": e.turn_role, "content": e.content} for e in events]
            span.set_attribute("session.history_len_raw", len(history))
            # Bound the history before it propagates into router state (G2).
            # A NoopTrimmer (default) is a one-call passthrough — non-FaaS
            # deployments keep today's behavior. FaaS deployments wire a
            # TokenBudgetTrimmer; over-budget without a summariser is a loud
            # typed failure, never a silent drop.
            try:
                trim = await self._trimmer.trim(history, tenant_id=tenant_id)
            except ConversationTrimError as exc:
                span.set_attribute("session.trim_failed", True)
                _log.warning(
                    "executor.memory.trim_failed",
                    tenant_id=tenant_id, session_id=session_id,
                    error=str(exc),
                )
                # The router can still operate on the raw history; we do NOT
                # block the turn on a memory-management policy issue. The
                # failure is recorded in the trace + log; an operator alerts
                # off this attribute. (Compare: an authz failure DOES block.)
                return {"conversation_history": history}
            span.set_attribute("session.history_len", len(trim.history))
            span.set_attribute("session.trim_summary_emitted", trim.summary_emitted)
            span.set_attribute("session.tokens_before", trim.estimated_tokens_before)
            span.set_attribute("session.tokens_after", trim.estimated_tokens_after)
            return {"conversation_history": trim.history}

    # ── update_focus (Stage 2 — LangGraph-native focus channel) ──────────
    async def update_focus(self, state: ExecutorState) -> dict[str, Any]:
        """Compute the active focus entity for THIS turn and write it
        into the LangGraph state as a structured channel.

        Rule (deterministic, no LLM):
          1. If the CURRENT user message contains a canonical entity id
             (INC, REQ, PBM, CHG, AST, CI, KB) → that is the new focus.
          2. Otherwise, walk the conversation history latest-first;
             return the most recent USER-named entity id.
          3. If neither yields an id → focus stays empty (fresh session
             or off-domain chat).

        Within a turn, downstream consumers (rewriter, router, UC handlers)
        READ focus from state instead of inferring it from history strings.
        ACROSS turns: production uses a per-request thread_id, so the
        checkpointer does NOT carry focus to the next turn — `load_session`
        re-derives it from the persisted session transcript at the start of
        each turn. (The "already in state via the checkpointer" path only
        applies when a caller reuses one thread_id, e.g. tests.)

        Why this matters: the stale-focus / linked-record-drift /
        assistant-mentioned-id bug class was caused by 3 layers
        (rewriter LLM, router regex helper, field-read LLM) each
        re-deriving focus independently and disagreeing. A single
        state channel collapses the class structurally — there is
        nothing to drift TO.
        """
        from oneops.router.entity_id import EntityIdNormalizer
        with _tracer.start_as_current_span(
            "executor.update_focus",
            attributes={"oneops.tenant_id": state.get("tenant_id", ""),
                        "session.id": state.get("session_id", "")},
        ) as span:
            normalizer = EntityIdNormalizer.from_registry_file()
            message = state.get("message", "") or ""
            # Carry forward the previous focus by default (the checkpointer
            # restored it). Only overwrite when this turn brings a new id.
            new_focus_id = state.get("focus_entity_id", "") or ""
            new_focus_service = state.get("focus_service_id", "") or ""
            source = "carried"
            # Step 1 — current message.
            extracted = normalizer.extract(message)
            if extracted.entities:
                e = extracted.entities[0]
                new_focus_id = e.entity_id
                new_focus_service = e.service_id
                source = "current_message"
            elif not new_focus_id:
                # Step 2 — most-recent user-named entity in history. Only
                # consulted when we have no carried-forward focus AND no
                # current-message entity (first turn after a session
                # resume from cold).
                for turn in reversed(state.get("conversation_history", []) or []):
                    if (turn.get("role") or "").lower() != "user":
                        continue
                    h_extracted = normalizer.extract(turn.get("content", "") or "")
                    if h_extracted.entities:
                        he = h_extracted.entities[0]
                        new_focus_id = he.entity_id
                        new_focus_service = he.service_id
                        source = "history_recovery"
                        break

            # Focus-intent classifier — runs on any turn that ended up with a
            # focus that did NOT come from the current message itself. The
            # user did not name an entity in this turn, but a focus is being
            # carried (from prior state or recovered from history). Classify
            # whether the user's intent is a property-of-focus question or a
            # topic-search; if topic-search, drop the focus so downstream
            # disambiguation runs against a clean state.
            if (new_focus_id
                    and source in ("carried", "history_recovery")
                    and self._focus_intent_classifier is not None):
                try:
                    label = await self._focus_intent_classifier.classify(
                        message=message,
                        focus_entity_id=new_focus_id,
                        focus_service=new_focus_service,
                        tenant_id=state.get("tenant_id", "") or "",
                        user_id=state.get("user_id", "") or "",
                    )
                except Exception:                                   # noqa: BLE001
                    label = "unknown"
                if label == "topic":
                    new_focus_id = ""
                    new_focus_service = ""
                    source = "topic_search_drop"
            span.set_attribute("focus.entity_id", new_focus_id or "")
            span.set_attribute("focus.service_id", new_focus_service or "")
            span.set_attribute("focus.source", source)
            _log.info("executor.update_focus",
                      focus_entity_id=new_focus_id,
                      focus_service_id=new_focus_service,
                      source=source)
            return {
                "focus_entity_id": new_focus_id,
                "focus_service_id": new_focus_service,
            }

    # ── route ────────────────────────────────────────────────────────────

    async def route(self, state: ExecutorState) -> dict[str, Any]:
        """Run the P5 router; write the plan (or a non-routed outcome)."""
        with _tracer.start_as_current_span(
            "executor.route",
            attributes={"oneops.request_id": state.get("request_id", ""),
                        "oneops.tenant_id": state.get("tenant_id", "")},
        ) as span:
            principal = Principal(
                tenant_id=state.get("tenant_id", ""),
                user_id=state.get("user_id", "") or "unknown",
                role=state.get("role", "") or "unknown")
            # Extract + canonicalise any entity references in the message, so
            # the router's condition filter sees real entity signals.
            extraction = self._entity_normalizer.extract(state.get("message", "") or "")
            span.set_attribute("router.entities_found", len(extraction.entities))
            span.set_attribute("router.entities_malformed", len(extraction.malformed))
            for bad in extraction.malformed:
                _log.info("router.malformed_entity_ref", raw=bad.raw, reason=bad.reason)

            # A near-miss (a real prefix, a botched number) always gets a
            # user-facing reply — never just a log line (thumb rule #11).
            entity_clarification = ""
            if extraction.malformed:
                entity_clarification = self._entity_normalizer.clarification_message(
                    extraction.malformed)

            # Every entity the user named is malformed and there is no valid
            # one to act on — the turn's answer IS the correction request.
            # Short-circuit before the router: there is nothing to route.
            if extraction.malformed and not extraction.entities:
                span.set_attribute("executor.route_outcome", "entity_clarification")
                increment("ai.router.outcome.total", outcome="entity_clarification",
                          tenant_id=principal.tenant_id)
                return {
                    "route_outcome": "entity_clarification",
                    "route_diagnostics": ["all entity references were malformed"],
                    "unrouted": [],
                    "plan": [],
                    "boundary_reason": "malformed_entity_reference",
                    "entity_clarification": entity_clarification,
                }

            signals = RequestSignals(
                role=principal.role, tenant_id=principal.tenant_id,
                present_entities=tuple(
                    (e.entity_id, e.service_id) for e in extraction.entities))
            history = [
                ConversationTurn(role=t.get("role", ""), content=t.get("content", ""))
                for t in (state.get("conversation_history") or [])
            ]
            result = await self._router.route(
                state.get("message", ""), principal=principal, signals=signals,
                conversation_history=history, request_ctx=_envelope(state))
            span.set_attribute("executor.route_outcome", result.outcome.value)
            set_langfuse_io(
                span, input=state.get("message", ""),
                output={"outcome": result.outcome.value,
                        "agents": (list(result.plan.agent_ids)
                                   if result.plan else [])})
            increment("ai.router.outcome.total", outcome=result.outcome.value,
                      tenant_id=principal.tenant_id)

            update: dict[str, Any] = {
                "route_outcome": result.outcome.value,
                "route_diagnostics": list(result.diagnostics),
                "unrouted": list(result.unrouted),
                "entity_clarification": entity_clarification,
            }
            if result.outcome is RouteOutcome.ROUTED and result.plan is not None:
                update["plan"] = serialise_plan(result.plan)
            else:
                update["plan"] = []
                update["boundary_reason"] = result.boundary_reason

            # ── Conditional TimeFilter extraction ─────────────────────────
            # Run the extractor ONLY when the plan contains an agent whose
            # registry record opts in via `consumes_time_filter: true`. This
            # keeps the LLM cost off summarisation / KB / triage turns that
            # don't need a temporal scope.
            update["time_filter"] = {}
            if (self._time_filter_extractor is not None
                    and update["plan"]):
                wants_filter = False
                for step in update["plan"]:
                    aid = step.get("agent_id")
                    if not aid:
                        continue
                    try:
                        rec = self._registry.agents.get_optional(aid)
                    except Exception:                                  # noqa: BLE001
                        rec = None
                    if rec is not None and getattr(
                            rec, "consumes_time_filter", False):
                        wants_filter = True
                        break
                if wants_filter:
                    try:
                        tf = await self._time_filter_extractor.extract(
                            message=state.get("message", "") or "",
                            tenant_id=principal.tenant_id,
                            user_id=principal.user_id,
                        )
                        if tf is not None:
                            update["time_filter"] = tf.model_dump(mode="json")
                            span.set_attribute(
                                "executor.time_filter.present", True)
                    except Exception as exc:                          # noqa: BLE001
                        # Never break routing on extractor failure.
                        _log.warning("executor.time_filter.extract_failed",
                                     error=str(exc)[:160])
            return update

    # ── wave (no-op; dispatch_wave does the routing) ─────────────────────

    async def wave(self, state: ExecutorState) -> dict[str, Any]:
        return {}

    # ── action-gate granularity (per-tool when the step names one) ───────

    def _step_is_action(self, step: dict[str, Any], agent: Any) -> bool:
        """Decide whether this step's work needs the action-approval interrupt.

        Granularity rule (data-driven, backward-compatible):
          * When the step explicitly names a `tool_id` (multi-tool plans —
            e.g. UC-5 triage), gate on THAT TOOL's `execution_type`. This is
            the correct granularity: an action-tier AGENT may own read tools
            (analysis / propose) and action tools (apply); only the action
            TOOLS should require approval. A read-only propose step under an
            action agent must not interrupt.
          * When the step names no tool (the chat path — the router does not
            stamp a tool_id), fall back to the AGENT tier. This preserves the
            existing behaviour exactly (golden tests unchanged).
          * Unknown tool_id ⇒ conservative fall back to the agent tier (the
            step will fail loudly in the executor regardless).
        """
        tool_id = str(step.get("tool_id") or "").strip()
        if tool_id:
            tool = self._registry.tools.get_optional(tool_id)
            if tool is not None:
                return tool.execution_type is ExecutionTier.ACTION
        return agent.abac_tags.tier is ExecutionTier.ACTION

    # ── run_step (one Send instance per step) ────────────────────────────

    async def run_step(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute one plan step: before-hooks → (interrupt if action) →
        step executor → after-hooks. Returns `{step_results: [result]}`."""
        step: dict[str, Any] = payload["_step"]
        request: dict[str, Any] = payload["_request"]
        agent_id = step.get("agent_id", "")
        t0 = time.monotonic()

        with _tracer.start_as_current_span(
            "executor.run_step",
            attributes={"oneops.agent_id": agent_id,
                        "executor.step_id": step.get("step_id", "")},
        ) as span:
            # Langfuse: the agent step's INPUT (which agent, with what params).
            set_langfuse_io(
                span,
                input={"agent_id": agent_id,
                       "parameters": step.get("parameters") or {}})
            agent = self._registry.agents.get_optional(agent_id)
            if agent is None:
                # The plan named an agent with no active registry record.
                return {"step_results": [make_result(
                    step, status="failed",
                    error=f"agent '{agent_id}' has no active registry record")]}

            # ── Policy gate (P10) ────────────────────────────────────────
            # A DENY refuses the step; a CANNED verdict replaces the step's
            # work with a pre-approved response (compliance touchpoint — zero
            # hallucination). Both skip the handler entirely.
            if self._policy_engine is not None:
                from oneops.policy_engine import PolicyEffect, PolicyQuery
                decision = self._policy_engine.evaluate(PolicyQuery(
                    tenant_id=request.get("tenant_id", ""),
                    role=request.get("role") or None,
                    data_classification=agent.abac_tags.data_classification.value,
                    intent=step.get("intent") or None))
                span.set_attribute("executor.policy_effect", decision.effect.value)
                if decision.effect is PolicyEffect.DENY:
                    return {"step_results": [make_result(
                        step, status="denied", error=f"policy: {decision.reason}")]}
                if decision.effect is PolicyEffect.CANNED:
                    return {"step_results": [make_result(
                        step, status="success",
                        output={"canned_response": decision.canned_response,
                                "policy_rule": decision.matched_rule_id})]}

            # ── data-flow binding (previous_results) ─────────────────────
            # Resolve declared bindings from upstream outputs BEFORE hooks /
            # approval: a step blocked on a missing required dependency must
            # not run its before-hooks or ask the user to approve work that
            # cannot proceed. `bound_inputs` are delivered to the handler via
            # context; `previous_results` is exposed raw too (policy term, so a
            # handler can read deps directly even without a declared binding).
            previous_results = payload.get("_previous_results") or {}
            bound_inputs, bind_status, bind_reason = _resolve_bindings(
                step, previous_results)
            if bind_status == "blocked":
                span.set_attribute("executor.blocked_reason", bind_reason)
                _log.info("executor.step_blocked",
                          step_id=step.get("step_id"), reason=bind_reason)
                return {"step_results": [make_result(
                    step, status="blocked", error=bind_reason)]}
            if bound_inputs:
                span.set_attribute("executor.bindings_resolved", len(bound_inputs))

            before = list(agent.hooks.before_invocation)
            after = list(agent.hooks.after_invocation)
            is_action = self._step_is_action(step, agent)
            span.set_attribute("executor.tier", agent.abac_tags.tier.value)
            span.set_attribute("executor.determinism", agent.determinism_level.value)

            # ── before-hooks ─────────────────────────────────────────────
            # NOTE on resume: an interrupt() below makes the node restart from
            # here, so before-hooks re-run. The built-in hooks are idempotent
            # (pure validation) — that is a requirement for any before-hook.
            try:
                hook_services = {"agent": agent}
                if self._authz_service is not None:
                    hook_services["authz"] = self._authz_service
                # Per-tool tier granularity (matches the action-approval gate):
                # an action-tier AGENT may own read tools (analysis / propose)
                # and action tools (apply). The authz re-check must evaluate the
                # resource at the STEP's effective tier — `is_action` from
                # `_step_is_action` — not blanket the agent tier, so a read-only
                # propose step under an action agent is checked as READ.
                hook_services["step_is_action"] = is_action
                ctx = HookContext(agent_id=agent_id, phase=HookPhase.BEFORE,
                                  step=step, request=request,
                                  services=hook_services)
                await self._hooks.run(before, ctx)
            except HookError as exc:
                return {"step_results": [make_result(
                    step, status="failed", error=f"before-hook aborted: {exc}")]}

            # ── action approval gate (interrupt) ─────────────────────────
            if is_action:
                decision = interrupt({
                    "kind": "action_approval",
                    "agent_id": agent_id,
                    "step_id": step.get("step_id"),
                    "parameters": dict(step.get("parameters") or {}),
                    "message": (f"Approve action '{agent_id}'? This step "
                                "changes ITSM state and is not auto-run."),
                })
                approved = bool(decision.get("approved")
                                if isinstance(decision, dict) else decision)
                if not approved:
                    return {"step_results": [make_result(
                        step, status="denied",
                        error="action not approved by the user")]}

            # ── the step's work ──────────────────────────────────────────
            try:
                # Deliver dependency outputs to the handler. Only build a
                # copy when there is something to add — no-binding steps pass
                # the original envelope unchanged (zero behaviour change).
                run_request = (
                    {**request, "previous_results": previous_results,
                     "bound_inputs": bound_inputs}
                    if (previous_results or bound_inputs) else request)
                result = await self._step_executor.run(step, run_request)
            except Exception as exc:  # noqa: BLE001 — typed into a failed result
                _log.warning("executor.step_executor_raised",
                             agent_id=agent_id, error=str(exc))
                result = make_result(step, status="failed",
                                     error=f"step executor raised: {exc}")

            # ── after-hooks ──────────────────────────────────────────────
            try:
                hook_services = {"agent": agent}
                if self._authz_service is not None:
                    hook_services["authz"] = self._authz_service
                ctx = HookContext(agent_id=agent_id, phase=HookPhase.AFTER,
                                  step=step, request=request, result=result,
                                  services=hook_services)
                await self._hooks.run(after, ctx)
            except HookError as exc:
                result = make_result(step, status="failed",
                                     error=f"after-hook aborted: {exc}")

            latency_ms = int((time.monotonic() - t0) * 1000)
            span.set_attribute("executor.step_status", result.get("status", ""))
            span.set_attribute("executor.latency_ms", latency_ms)
            histogram("ai.agent.latency_ms", value=latency_ms, agent_id=agent_id)

            # ── runtime-generated steps (dynamic fan-out) ────────────────
            # A handler signals follow-up work it discovered at runtime by
            # returning `generated_steps` on its result. We validate +
            # namespace them and append to the `plan` channel; `dispatch_wave`
            # re-reads the plan each superstep, so they run in a later wave —
            # no graph recompile, no topology change. Budget-guarded so a
            # self-spawning handler can never run away (depth + width caps).
            raw_generated = (result.pop("generated_steps", None)
                             if isinstance(result, dict) else None)
            update: dict[str, Any] = {"step_results": [result]}
            if raw_generated:
                new_steps, note = _normalise_generated_steps(step, raw_generated)
                # ── anti-hallucination guard ─────────────────────────────
                # Generated steps may be produced by a handler's LLM decision,
                # so they cannot be trusted blindly. A step may ONLY name an
                # agent that has an active registry record (closed-vocabulary
                # check); a hallucinated / made-up agent_id is refused here and
                # surfaced — never executed, never silent (thumb rule #11).
                # Each surviving step still re-enters run_step, so it is
                # re-gated by policy + RBAC + hooks like any planned step —
                # generation can never escalate privilege or skip a control.
                valid: list[dict[str, Any]] = []
                for s in new_steps:
                    if self._registry.agents.get_optional(s["agent_id"]) is None:
                        note = ((note + "; " if note else "")
                                + f"refused generated step naming unknown "
                                  f"agent '{s['agent_id']}'")
                        continue
                    valid.append(s)
                if valid:
                    span.set_attribute("executor.generated_steps", len(valid))
                    update["plan"] = valid
                if note:
                    span.add_event("executor.generation_capped", {"reason": note})
                    _log.warning("executor.generation_capped",
                                 parent=step.get("step_id"), reason=note)
                    result["generation_note"] = note
            return update

    # ── aggregate ────────────────────────────────────────────────────────

    async def aggregate(self, state: ExecutorState) -> dict[str, Any]:
        """Stitch step results into the final response + status.

        Every step result is rendered through `friendly_step_response(step,
        result)` — the user sees a clean, contextual message regardless of
        whether the handler returned an outcome string, the hook denied, or
        the LLM gateway exhausted retries. The terse `"- agent_id: status"`
        debug shape is gone (Phase A+B of the UC-1 contract).
        """
        results = state.get("step_results") or []
        plan = state.get("plan") or []
        unrouted = state.get("unrouted") or []

        # No-silent-skip (thumb rule #11): any plan step that never produced a
        # result — transitively blocked by an upstream failure, or a
        # malformed-plan deadlock — is surfaced as `blocked` rather than
        # vanishing. Well-formed plans run every step, so this is a no-op for
        # them (existing behaviour unchanged).
        result_ids = {r.get("step_id") for r in results}
        synthesized = [
            make_result(s, status="blocked",
                        error="not executed — an upstream dependency did "
                              "not complete")
            for s in plan if s.get("step_id") not in result_ids
        ]
        all_results = list(results) + synthesized

        order = {s["step_id"]: i for i, s in enumerate(plan)}
        plan_by_step = {s.get("step_id"): s for s in plan}
        ordered = sorted(all_results,
                         key=lambda r: order.get(r.get("step_id"), 10**6))

        # Per-agent run metric — every step, every status (drives the
        # per-agent error-rate dashboard, ARCHITECTURE.md §7).
        for r in ordered:
            increment("ai.agent.runs.total", agent_id=r.get("agent_id", ""),
                      status=r.get("status", "unknown"))

        succeeded = [r for r in ordered if r.get("status") == "success"]
        failed = [r for r in ordered if r.get("status") in ("failed", "denied")]
        blocked = [r for r in ordered if r.get("status") == "blocked"]

        if succeeded and not failed and not blocked and not unrouted:
            status = "executed"
        elif succeeded:
            status = "partial"            # some worked, some blocked/failed
        elif blocked and not failed:
            status = "blocked"            # nothing ran; a dependency stopped it
        else:
            status = "failed"

        # A canned (policy) response is the user-facing answer — compliance
        # wins over the structural summary.
        canned = next(
            (r["output"]["canned_response"] for r in ordered
             if isinstance(r.get("output"), dict)
             and r["output"].get("canned_response")),
            None)
        if canned:
            response = canned
        else:
            # NEW: per-step friendly messages. Multi-step turns (compound
            # actions, multi-sub-query) join with blank lines so each step
            # reads independently.
            parts: list[str] = []
            seen: set[str] = set()
            for r in ordered:
                step = plan_by_step.get(r.get("step_id"), {})
                rendered = friendly_step_response(step, r)
                if not rendered:
                    continue
                # Dedup identical step messages — "summarize X and find KB
                # for X" against a missing X produces the same not-found
                # text twice; surfacing it once is enough. Keep the first
                # occurrence in plan order.
                key = " ".join(rendered.split())
                if key in seen:
                    continue
                seen.add(key)
                parts.append(rendered)
            response = "\n\n".join(parts) or "Nothing to do."
            if unrouted:
                response += (
                    "\n\nI couldn't act on: "
                    + "; ".join(f'"{u}"' for u in unrouted))

        # The turn acted on the valid IDs, but the message also held a
        # malformed one — append the correction request so the bad reference
        # is never silently dropped (thumb rule #11).
        clarification = state.get("entity_clarification", "")
        if clarification:
            response = f"{response}\n\n{clarification}"

        return {"final_status": status, "final_response": response}

    # ── conversation control gate (Stage 1) ──────────────────────────────
    #
    # Pre-router gate that handles greetings / thanks / acks / farewells /
    # help inquiries / structural noise with canned replies. A turn
    # classified as conversational short-circuits the rest of the graph:
    # no router, no disambiguator, no agent invocation, no LLM tokens on
    # cache hit. Falls through to the normal pipeline on `none` /
    # abstention / canonical-ID present.

    async def control_gate(self, state: ExecutorState) -> dict[str, Any]:
        from oneops.conversation.control_gate import (
            detect_conversation_control,
        )
        # Always run the LLM control gate. The earlier embedding-axis
        # bypass (deleted 2026-05-29) tried to skip this when focus was
        # set, but it could not distinguish domain-adjacent off-domain
        # queries ("how to fix bluetooth", "schedule a meeting") from
        # legitimate on-domain follow-ups ("any data on this") — both
        # scored axis-B on the embedding classifier. Trusting the LLM
        # control gate is V1's pattern and Moveworks' pattern; the
        # latency cost (~300ms) is acceptable until we have proper
        # hierarchical intent classification at 50+ UCs.
        result = await detect_conversation_control(
            message=state.get("message", "") or "",
            tenant_id=state.get("tenant_id", "") or "",
            user_id=state.get("user_id", "") or "",
            request_id=state.get("request_id", "") or "",
            focus_entity_id=state.get("focus_entity_id", "") or "",
            focus_service_id=state.get("focus_service_id", "") or "",
        )
        # Always set the marker so the graph's conditional edge sees it.
        update: dict[str, Any] = {"control_gate_outcome": result.control_type}
        if result.is_control and result.response_text:
            # ── Data-driven domain backstop (2026-06-02 RCA) ─────────────
            # The gate's `out_of_scope` verdict is occasionally wrong — and
            # not perfectly deterministic even at temperature 0 — for
            # borderline IT how-to phrasings ("how do I configure a VPN
            # client?" refused while "...client" answered). Before refusing,
            # probe the KB CORPUS itself, the authoritative + deterministic
            # domain signal: if it has a real answer the query IS in-domain,
            # so return that answer instead of "out of scope". A genuine
            # off-topic query ("recommend a pizza place") matches nothing
            # (search_kb's relevance gate) and refuses exactly as before —
            # this only rescues real IT questions the scope LLM mishandled.
            if result.control_type == "out_of_scope":
                from oneops.use_cases.uc03_kb_lookup.handlers import (
                    kb_backstop_answer,
                )
                ctx = {
                    "tenant_id": state.get("tenant_id", "") or "",
                    "user_id": state.get("user_id", "") or "",
                    "role": state.get("role", "") or "",
                    "request_id": state.get("request_id", "") or "",
                }
                kb = await kb_backstop_answer(
                    state.get("message", "") or "", ctx)
                if kb:
                    return {"control_gate_outcome": "kb_backstop",
                            "final_status": "executed",
                            "final_response": kb}
            # Short-circuit. final_status mirrors the boundary's
            # `clarification` for non-task replies so the UI renders the
            # same way it does today for greetings/OOS.
            update.update({
                "final_status": "clarification",
                "final_response": result.response_text,
            })
        return update

    # ── boundary ─────────────────────────────────────────────────────────

    async def boundary(self, state: ExecutorState) -> dict[str, Any]:
        """No use-case agent ran — the boundary responder answers."""
        outcome = state.get("route_outcome", "no_confident_match")
        reason = state.get("boundary_reason", "")
        # Every entity the user named was malformed: the correction request,
        # built by `route`, is the whole answer for this turn.
        if outcome == "entity_clarification":
            clarification = state.get("entity_clarification", "") or (
                "I could not read the record ID in your message. "
                "Please send it again, e.g. \"INC0001234\".")
            with _tracer.start_as_current_span(
                "executor.boundary",
                attributes={"executor.route_outcome": outcome},
            ):
                return {"final_status": "clarification",
                        "final_response": clarification}
        with _tracer.start_as_current_span(
            "executor.boundary",
            attributes={"executor.route_outcome": outcome},
        ):
            text = await self._boundary.respond(
                outcome=outcome, reason=reason, request=_envelope(state))
        status = "clarification" if outcome == "no_confident_match" else "denied"
        return {"final_status": status, "final_response": text}

    # ── persist ──────────────────────────────────────────────────────────

    async def persist(self, state: ExecutorState) -> dict[str, Any]:
        """Append this turn — the user message and the assistant response — to
        the durable conversation log, so the next turn can resolve references
        against it. A no-op when no session store is wired."""
        if self._session_store is None:
            return {}
        tenant_id = state.get("tenant_id", "")
        session_id = state.get("session_id", "")
        if not tenant_id or not session_id:
            return {}

        message = state.get("message", "") or ""
        response = state.get("final_response", "") or ""
        base = len(state.get("conversation_history") or [])
        now = int(time.time() * 1000)

        with _tracer.start_as_current_span(
            "executor.persist",
            attributes={"oneops.tenant_id": tenant_id,
                        "oneops.user_id": state.get("user_id", ""),
                        "session.id": session_id},
        ):
            if message:
                await self._session_store.append(tenant_id, session_id, ConversationEvent(
                    session_id=session_id, turn_role="user", content=message,
                    turn_index=base, occurred_at_unix_ms=now))
            if response:
                await self._session_store.append(tenant_id, session_id, ConversationEvent(
                    session_id=session_id, turn_role="assistant", content=response,
                    turn_index=base + 1, occurred_at_unix_ms=now))
        return {}


# ── conditional-edge functions (pure — read state only) ──────────────────


def route_branch(state: ExecutorState) -> str:
    """After `route`: into execution if a plan exists, else to the boundary."""
    return "execute" if state.get("route_outcome") == "routed" else "boundary"


def dispatch_wave(state: ExecutorState) -> list[Send] | str:
    """After `wave`: emit a `Send` per runnable step, or go to `aggregate`.

    A step is runnable when every `depends_on` step already has a result.
    Independent steps fan out together; dependent steps wait their wave.
    """
    plan = state.get("plan") or []
    results = state.get("step_results") or []
    completed = {r.get("step_id") for r in results}
    results_by_id = {r.get("step_id"): r for r in results}

    remaining = [s for s in plan if s["step_id"] not in completed]
    if not remaining:
        return "aggregate"

    request = _envelope(state)
    runnable = [s for s in remaining
                if all(dep in completed for dep in s.get("depends_on", []))]
    if not runnable:
        # No step can advance though some remain — a malformed plan (a valid
        # DAG never reaches here). Stop the loop; aggregate reports the rest
        # (transitively-blocked steps are surfaced there, never silent).
        _log.warning("executor.dispatch_deadlock",
                     remaining=[s["step_id"] for s in remaining])
        return "aggregate"

    # Attach each step's dependency outputs so `run_step` can resolve declared
    # data-flow bindings (previous_results). Pure derivation from the durable
    # `step_results` channel ⇒ deterministic on checkpoint replay.
    sends: list[Send] = []
    for s in runnable:
        dep_results = {d: results_by_id[d]
                       for d in s.get("depends_on", []) if d in results_by_id}
        sends.append(Send("run_step", {"_step": s, "_request": request,
                                       "_previous_results": dep_results}))
    return sends


__all__ = ["ExecutorNodes", "route_branch", "dispatch_wave"]
