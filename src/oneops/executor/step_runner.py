"""StepExecutor — runs the actual work of one plan step.

This is the seam between orchestration (P6 — the LangGraph graph) and the
tool layer (P7). The graph drives *when* and *in what order* steps run, with
hooks, interrupts, and checkpointing; the `StepExecutor` does the *work* of
one step — invoking the agent's tools, calling the LLM.

`StepExecutor` is a Protocol. Two concrete implementations ship today:

  * `HandlerStepExecutor` — production: resolves the agent's primary tool
    (today `fast_path.primary_tool_id`), looks up its `handler_ref` via the
    `HandlerResolver`, and calls the handler with the step parameters and a
    context dict built from the request envelope. Returns a typed result.
  * `EchoStepExecutor` — deterministic stub used by graph tests that exercise
    orchestration without the handler layer. Always succeeds, records the
    invocation, calls no real handler.

A step result is a plain dict: `{step_id, agent_id, status, output, error}`.
`status` ∈ {success, failed}.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

from langgraph.errors import GraphInterrupt

from oneops.errors import ToolHandlerError
from oneops.observability import get_logger, get_tracer, set_langfuse_io
from oneops.observability.event_sink import publish as _publish_event
from oneops.observability.metrics import increment as _metric_inc

# Telemetry literals → constants (sonar S1192).
_STEP_STATUS = "step.status"


def _tool_action(tool: Any) -> str:
    """A one-line, human 'what this tool does' phrase for the live UI —
    derived from the tool's REGISTRY description (first sentence). No static
    per-tool phrase catalogue: it tracks whatever the registry declares."""
    desc = (getattr(tool, "description", "") or "").strip()
    if not desc:
        return ""
    first = desc.replace("\n", " ").split(". ")[0].strip().rstrip(".")
    return first[:160]

_log = get_logger("oneops.executor.step_runner")
_tracer = get_tracer("oneops.executor.step_runner")


class StepExecutor(Protocol):
    async def run(self, step: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        """Execute one plan step. Returns a step-result dict. Must not raise
        for an ordinary handler failure — return `status="failed"` with a
        populated `error` instead, so the aggregator can report it."""
        ...


def make_result(
    step: dict[str, Any], *, status: str, output: Any = None,
    error: str | None = None, tool_id: str = "", latency_ms: int | None = None,
) -> dict[str, Any]:
    """Build a well-formed step-result dict.

    `tool_id` + `latency_ms` surface which tool the executor invoked for this
    step and how long it took. They feed the UI's execution-trace panel
    ("which agents + tools ran this query") and are additive, optional fields
    on the response contract — older consumers ignore them.
    """
    return {
        "step_id": step.get("step_id"),
        "agent_id": step.get("agent_id"),
        "status": status,
        "output": output,
        "error": error,
        "tool_id": tool_id,
        "latency_ms": latency_ms,
    }


class EchoStepExecutor:
    """Deterministic `StepExecutor` — records the invocation, does no real work.

    A genuine implementation of the Protocol (not a mock): it is the correct
    executor for any environment without the P7 tool layer, and it is what
    the P6 graph tests run against. It always succeeds; failure-path tests
    inject their own executor stub.
    """

    async def run(self, step: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        return make_result(
            step,
            status="success",
            output={
                "agent_id": step.get("agent_id"),
                "parameters": dict(step.get("parameters") or {}),
                "note": "echo executor — P7 supplies the real tool-running executor",
            },
        )


# ── HandlerStepExecutor — the production executor ────────────────────────


_DEFAULT_TIMEOUT_S = 30.0


class HandlerStepExecutor:
    """Resolves the agent's primary tool, calls its registered handler.

    Today the "primary tool" is `agent.fast_path.primary_tool_id`. Every UC
    in POC-5-MW declares it (both the fast-path door and the chat door
    invoke the same primary tool — `Button == Chat`). When ReAct-style
    multi-tool agent loops land, this class is the seam that grows into
    the loop; the surface above (`StepExecutor.run`) does not change.

    Invocation contract:

      * Looks up the `AgentRecord` from the registry — unknown agent ⇒
        `status="failed"`, error names the missing id.
      * Picks the primary tool: today `agent.fast_path.primary_tool_id`. If
        the agent has no fast_path block, the step fails loud (no
        UC-without-a-tool-binding silently does nothing).
      * Resolves `tool.handler_ref` through `HandlerResolver` — unresolvable
        ⇒ `ToolHandlerError`, wrapped into `status="failed"`.
      * Builds a context dict from the request envelope — the same shape
        the in-memory handler tests use (`tenant_id`, `user_id`, `role`,
        plus the canonical envelope ids). The handler reads via `.get()`.
      * Calls the handler under an `asyncio.wait_for` with the tool's
        `timeout_ms` (or `_DEFAULT_TIMEOUT_S` if not set). Timeout ⇒
        `status="failed"` with the latency stamped.
      * Any handler exception is contained — never raised through the
        graph (the aggregator must always have a typed step result).

    Observability: one `executor.step.handler_call` span per call, attributes
    cover tenant, agent_id, tool_id, status, and latency. Sensitive
    arguments (`ticket_id`, etc.) are attributed as identifiers, never as
    free-text. The handler's own spans nest inside this one (OTel context
    propagation).
    """

    # ── tool selection ───────────────────────────────────────────────────

    def _select_tool(
        self, agent: Any, step: dict[str, Any], step_params: dict[str, Any],
    ) -> tuple[str, Any]:
        """Pick the tool for a step, honouring an explicit `step["tool_id"]`.

        Multi-tool plans (e.g. UC-5 triage: check → assign ∥ prio → assemble)
        name the exact tool each step runs — necessary because several tools on
        one agent can share a required-parameter shape (check and prioritize
        both need service_id+ticket_id), which `_pick_tool`'s shape heuristic
        cannot disambiguate. An explicit tool_id is the planner's commitment.

        Rules (data-driven, no UC-specific code):
          * `step["tool_id"]` present AND bound to the agent (`tool_refs`) →
            use it (the planner chose it).
          * present but NOT bound to the agent → return it with tool=None so
            the caller fails LOUD (never silently run a different tool).
          * absent → defer to `_pick_tool` (the chat path; zero behaviour
            change — the router never stamps a tool_id).
        """
        explicit = str(step.get("tool_id") or "").strip()
        if explicit:
            allowed = {t.tool_id for t in (getattr(agent, "tool_refs", []) or [])}
            if explicit in allowed:
                return (explicit, self._registry.tools.get_optional(explicit))
            return (explicit, None)        # surfaced loud by the caller
        return self._pick_tool(agent, step_params)

    def _candidate_tool_ids(self, agent: Any) -> tuple[list[str], str]:
        """The agent's candidate tool ids (its tool_refs, else its fast-path
        primary tool) plus the declared primary_id."""
        tool_refs = list(getattr(agent, "tool_refs", []) or [])
        primary_id = (agent.fast_path.primary_tool_id
                      if agent.fast_path else "")
        candidate_ids = [t.tool_id for t in tool_refs] or (
            [primary_id] if primary_id else [])
        return candidate_ids, primary_id

    def _primary_tool_if_fits(
        self, primary_id: str, candidate_ids: list[str], present: set[str],
    ) -> tuple[str, Any] | None:
        """The declared primary tool, IF it's a candidate and its required
        params are all present in `present`; else None."""
        if not primary_id or primary_id not in candidate_ids:
            return None
        primary_tool = self._registry.tools.get_optional(primary_id)
        if primary_tool is None:
            return None
        need = _required_params(primary_tool)
        if not need or need.issubset(present):
            return (primary_id, primary_tool)
        return None

    def _pick_tool(
        self, agent: Any, step_params: dict[str, Any],
    ) -> tuple[str, Any]:
        """Pick the right tool from the agent's tool_refs for this step.

        Selection rule (data-driven, no UC-specific code):
          * One tool → use it.
          * Multiple tools → pick the FIRST tool whose REQUIRED parameter
            names are all present (non-empty) in `step_params`. The
            registry's `tool.parameters[*].required` flag is the source
            of truth; we never hard-code names. This is what makes
            agent.tool_refs of size N route correctly without registry
            patches per UC.
          * No tool satisfies → fall back to `agent.fast_path.primary_tool_id`
            (the fast-path button's explicit choice). This preserves
            backward-compat for single-tool agents and the button door.

        Returns `(tool_id, ToolRecord)`. Either is empty/None when no
        invokable tool can be resolved — the caller surfaces that loud.
        """
        candidate_ids, primary_id = self._candidate_tool_ids(agent)
        if not candidate_ids:
            return ("", None)
        if len(candidate_ids) == 1:
            only = candidate_ids[0]
            return (only, self._registry.tools.get_optional(only))

        present = {k for k, v in step_params.items()
                   if v not in (None, "", [], {})}

        # First pass — the agent's declared primary tool wins if it's a
        # candidate and its required params are all present (the author named
        # it as the chat default for this UC).
        hit = self._primary_tool_if_fits(primary_id, candidate_ids, present)
        if hit is not None:
            return hit

        # Second pass — highest specificity among tools whose required params
        # are all present (tie-break entity-shaped, then registry order).
        best_id, best_tool = self._best_specificity_tool(candidate_ids, present)
        if best_tool is not None:
            return (best_id, best_tool)
        # Fall back to the agent's declared primary tool (fast-path's
        # choice). Used for button door and for agents where no tool's
        # required-field set fits this step's parameter shape.
        if primary_id:
            return (primary_id, self._registry.tools.get_optional(primary_id))
        # As a last resort, use the first listed tool — better than
        # silent dead-end.
        first = candidate_ids[0]
        return (first, self._registry.tools.get_optional(first))

    def _best_specificity_tool(
        self, candidate_ids: list[str], present: set[str],
    ) -> tuple[str, Any]:
        """Among candidates whose REQUIRED params are all present, return the
        `(tool_id, ToolRecord)` with the highest specificity score. Tie-break
        by entity-shaped required-param count (a tool that asks for `ticket_id`
        beats one that asks for `query` when both fit); final tie-break is
        registry order via the `>` comparison (first writer wins). Returns
        `("", None)` when no candidate's required set fits `present`."""
        best_id: str = ""
        best_tool: Any = None
        best_score: tuple[int, int] = (-1, -1)
        for tid in candidate_ids:
            t = self._registry.tools.get_optional(tid)
            if t is None:
                continue
            need = _required_params(t)
            if need and need.issubset(present):
                score = _tool_specificity(need)
                if score > best_score:
                    best_score = score
                    best_id, best_tool = tid, t
        return (best_id, best_tool)

    def __init__(
        self,
        *,
        registry,                                          # RegistryService
        resolver=None,                                     # HandlerResolver
        default_timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        # Lazy import — `oneops.toolrunner` re-exports `make_result` from
        # this module, so importing it eagerly creates a cycle. Resolved at
        # construction time, after the package is fully initialised.
        if resolver is None:
            from oneops.toolrunner.resolver import HandlerResolver
            resolver = HandlerResolver()
        self._registry = registry
        self._resolver = resolver
        self._default_timeout_s = default_timeout_s

    async def run(
        self, step: dict[str, Any], request: dict[str, Any],
    ) -> dict[str, Any]:
        agent_id = str(step.get("agent_id") or "").strip()
        if not agent_id:
            return make_result(step, status="failed",
                               error="step has no agent_id")
        agent = self._registry.agents.get_optional(agent_id)
        if agent is None:
            return make_result(step, status="failed",
                               error=f"unknown agent {agent_id!r}")
        # Tool selection — data-driven, multi-tool-aware.
        #
        # An agent's `tool_refs` is its full toolset. The chat-path step
        # arrives with `parameters` shaped by the router (entity binds +
        # generic `user_message` / `query`). The right tool for THIS step
        # is the one whose REQUIRED parameter names are all satisfied by
        # `step.parameters`. With one tool the choice is trivial; with
        # several (UC-3 has `search_kb` / `get_kb_article` /
        # `search_kb_by_ticket`) we match by parameter shape — no
        # UC-specific code, no keyword catalogs.
        #
        # `fast_path.primary_tool_id` remains the fallback (and the
        # explicit choice the fast-path BUTTON makes). The chat path
        # only falls back to it when no tool's required params are
        # satisfied.
        step_params: dict[str, Any] = dict(step.get("parameters") or {})
        tool_id, tool = self._select_tool(agent, step, step_params)
        if tool is None:
            return make_result(
                step, status="failed",
                error=(f"agent {agent_id} has no invokable tool — "
                       f"tool_refs missing or none satisfies the step "
                       f"parameter shape"))

        handler, handler_err = self._resolve_handler(tool, step)
        if handler_err is not None:
            return handler_err

        timeout_s = (tool.timeout_ms / 1000.0) if tool.timeout_ms else self._default_timeout_s
        arguments = dict(step.get("parameters") or {})
        # Data-flow binding (generic — every UC, no per-handler code). The
        # executor resolved declared bindings into `bound_inputs`
        # ({to_param: upstream_value}); merge them into the handler arguments so
        # ANY handler receives them as ordinary parameters and never needs to
        # know bindings exist. A bound value wins for its declared param — the
        # planner explicitly routed that input from an upstream result. Empty
        # unless this step declared bindings ⇒ zero change for every other path.
        bound_inputs = request.get("bound_inputs") or {}
        if bound_inputs:
            arguments.update(bound_inputs)
        context = _build_handler_context(request)

        # ── Slot-filling gate (flag-gated, additive) ──────────────────────
        # If a REQUIRED entity-shaped parameter (e.g. ticket_id) is still
        # unbound AND there's no conversational focus to fill it, ASK the user
        # which record they mean instead of dispatching the tool to a certain
        # failure. On the first pass `maybe_elicit_entity` raises GraphInterrupt
        # (the turn pauses with the question); on resume it returns the resolved
        # bindings, which we merge before dispatch. Flag OFF ⇒ skipped wholesale
        # (today's path, zero regression). Focus-bound follow-ups are left to
        # the existing focus path — we never interrupt mid-conversation.
        if _elicitation_enabled() and not context.get("focus_entity_id"):
            slot = _missing_entity_slot(tool, arguments)
            if slot is not None:
                from oneops.executor import entity_elicitation
                param_name, service_param = slot
                bindings = await entity_elicitation.maybe_elicit_entity(
                    param_name=param_name, service_param=service_param,
                    context=context)
                if bindings:
                    arguments.update(bindings)

        # Live UI: announce the tool is now executing (no-op unless a
        # streaming sink is open for this request).
        rid = str(request.get("request_id") or "")
        _publish_event(rid, {
            "type": "tool_start",
            "step_id": step.get("step_id") or "",
            "agent_id": agent_id,
            "tool_id": tool_id,
            "action": _tool_action(tool),
        })

        return await self._invoke_handler(
            handler=handler, arguments=arguments, context=context, step=step,
            rid=rid, agent_id=agent_id, tool_id=tool_id, timeout_s=timeout_s)

    def _resolve_handler(
        self, tool: Any, step: dict[str, Any],
    ) -> tuple[Any, dict[str, Any] | None]:
        """Resolve a tool's handler callable. Returns `(handler, None)` on
        success, or `(None, failed_result)` when the handler ref is
        unresolvable or the resolver raises."""
        try:
            return self._resolver.resolve(tool.handler_ref), None
        except ToolHandlerError as exc:
            return None, make_result(step, status="failed",
                                     error=f"handler unresolvable: {exc}")
        except Exception as exc:                          # noqa: BLE001 — boundary
            return None, make_result(
                step, status="failed",
                error=f"handler resolver raised {type(exc).__name__}: {exc}")

    async def _invoke_handler(
        self, *, handler: Any, arguments: dict[str, Any],
        context: dict[str, Any], step: dict[str, Any], rid: str,
        agent_id: str, tool_id: str, timeout_s: float,
    ) -> dict[str, Any]:
        """Call the handler inside a `handler_call` span with a hard timeout,
        emitting the started marker + Langfuse I/O + the terminal `tool_done`
        event. Timeouts and raises are typed into a failed result via
        `_handler_error_result`."""
        with _tracer.start_as_current_span(
            "executor.step.handler_call",
            attributes={
                "oneops.tenant_id": context.get("tenant_id", ""),
                "oneops.user_id": context.get("user_id", ""),
                "oneops.agent_id": agent_id,
                "oneops.tool_id": tool_id,
                "oneops.step_id": step.get("step_id") or "",
                "oneops.timeout_s": timeout_s,
            },
        ) as span:
            # Langfuse: tool INPUT (redacted, content-gated) on the handler span
            # so the trace tree shows what each tool received.
            set_langfuse_io(span, input=arguments, observation_type="span")
            # "started" — the terminal status (success/failed) is emitted
            # at the aggregation point in `executor.nodes.aggregate`. The
            # dashboard's success-rate query filters for {status=~"success|
            # failed"} so this "started" marker is observable for operator
            # debug but never drags the ratio.
            _metric_inc("ai.agent.runs.total", 1,
                        agent_id=agent_id,
                        tenant_id=str(context.get("tenant_id") or ""),
                        tool_id=tool_id,
                        status="started")
            t0 = time.monotonic()
            try:
                output = await asyncio.wait_for(
                    handler(arguments, context), timeout=timeout_s,
                )
            except GraphInterrupt:
                # The handler PAUSED the turn (Conversational Interrupt
                # Protocol — e.g. UC-8 catalog asking the user to pick an
                # item). This is control flow, not a failure: it must
                # propagate to LangGraph so the graph checkpoints and the API
                # returns the interrupt. Never type it into a failed result.
                raise
            except Exception as exc:                      # noqa: BLE001 — boundary
                # TimeoutError (a subclass of Exception) and any other handler
                # exception both land here; `_handler_error_result` branches on
                # the type for the span status + log + error text.
                return self._handler_error_result(
                    exc, step=step, rid=rid, agent_id=agent_id,
                    tool_id=tool_id, timeout_s=timeout_s, t0=t0, span=span)
            latency_ms = int((time.monotonic() - t0) * 1000)
            span.set_attribute(_STEP_STATUS, "success")
            span.set_attribute("step.latency_ms", latency_ms)
            # Langfuse: tool OUTPUT (redacted, content-gated).
            set_langfuse_io(span, output=output, observation_type="span")
            _publish_event(rid, {
                "type": "tool_done", "step_id": step.get("step_id") or "",
                "agent_id": agent_id, "tool_id": tool_id,
                "status": "success", "latency_ms": latency_ms})
            return make_result(step, status="success", output=output,
                               tool_id=tool_id, latency_ms=latency_ms)

    def _handler_error_result(
        self, exc: BaseException, *, step: dict[str, Any], rid: str,
        agent_id: str, tool_id: str, timeout_s: float, t0: float, span,
    ) -> dict[str, Any]:
        """Build the failed step result for a handler that timed out or raised.
        A `TimeoutError` is reported as a timeout (distinct span status + log +
        user-safe text); any other exception is reported as a handler raise.
        Both publish a `tool_done(failed)` event and return a `failed` result."""
        latency_ms = int((time.monotonic() - t0) * 1000)
        span.set_attribute("error", True)
        if isinstance(exc, TimeoutError):
            span.set_attribute(_STEP_STATUS, "timeout")
            _log.warning("executor.step.timeout",
                         agent_id=agent_id, tool_id=tool_id,
                         timeout_s=timeout_s)
            error = (f"handler timed out after {timeout_s:.1f}s "
                     f"(tool={tool_id})")
        else:
            span.set_attribute(_STEP_STATUS, "failed")
            _log.warning("executor.step.handler_raised",
                         agent_id=agent_id, tool_id=tool_id,
                         error=str(exc)[:200])
            error = f"handler raised {type(exc).__name__}: {exc}"
        _publish_event(rid, {
            "type": "tool_done", "step_id": step.get("step_id") or "",
            "agent_id": agent_id, "tool_id": tool_id,
            "status": "failed", "latency_ms": latency_ms})
        return make_result(step, status="failed", error=error,
                           tool_id=tool_id, latency_ms=latency_ms)


# Parameter names the step runner injects from the request envelope
# (`tenant_id`, `user_id`, …) — present to the handler but never in
# `step.parameters`. Excluded from the required-set check so e.g.
# `summarize_entity` (required: ticket_id+service_id+tenant_id) is not wrongly
# eliminated when the step carries only ticket_id and service_id.
_CONTEXT_BOUND_PARAMS = {"tenant_id", "user_id", "role", "request_id",
                         "session_id", "trace_id"}
# Entity-shaped parameter names — must match the single source of truth in
# `oneops.router.router._ENTITY_FIELD_NAMES`. A tool requiring an entity-shaped
# parameter is a stronger semantic match than one requiring a free-text
# `query`, so it wins the specificity tie-break.
_ENTITY_SHAPED_PARAMS = {
    "ticket_id", "article_id", "entity_id",
    "incident_id", "request_id", "problem_id",
    "change_id", "asset_id", "ci_id", "kb_id",
}


def _required_params(tool: Any) -> set[str]:
    """Required parameter names for a tool, excluding the context-bound names
    the step runner injects from the request envelope."""
    return {p.name for p in (tool.parameters or [])
            if p.required and p.name not in _CONTEXT_BOUND_PARAMS}


def _tool_specificity(need: set[str]) -> tuple[int, int]:
    """Two-component ranking score: (count of required params, count of
    entity-shaped names among them). More required params = more specific;
    structured-entity match outranks free-text match on equal counts."""
    return (len(need), len(need & _ENTITY_SHAPED_PARAMS))


def _elicitation_enabled() -> bool:
    """Slot-filling feature flag. Graduated to ON by default (2026-06-11) after
    live validation: a missing required entity now ASKS the user which record
    they mean instead of dispatch-and-fail. Set
    `ONEOPS_ENTITY_ELICITATION_ENABLED=false` to disable. Read per-call so an
    operator can flip it without a restart (matches `approval_enabled`)."""
    from oneops.config import _parse_flag
    return _parse_flag("ONEOPS_ENTITY_ELICITATION_ENABLED", default=True)


def _missing_entity_slot(
    tool: Any, arguments: dict[str, Any],
) -> tuple[str, str] | None:
    """The first REQUIRED, entity-shaped parameter this step left unbound (e.g.
    `ticket_id` for 'summarize my ticket'), paired with the tool's service
    parameter name so resolution can bind both. `None` when every required
    entity slot is filled — the common case, near-zero overhead.

    Data-driven: `required` + entity-shaped come from the tool's own registry
    parameters; no UC-specific code, no hardcoded tool/param names beyond the
    shared `_ENTITY_SHAPED_PARAMS` / `service_id` envelope."""
    params = tool.parameters or []
    present = {k for k, v in arguments.items() if v not in (None, "", [], {})}
    service_param = "service_id" if any(
        getattr(p, "name", "") == "service_id" for p in params) else ""
    for p in params:
        if (p.required and p.name in _ENTITY_SHAPED_PARAMS
                and p.name not in present):
            return (p.name, service_param)
    return None


def _build_handler_context(request: dict[str, Any]) -> dict[str, Any]:
    """Translate the executor's request envelope into the context dict the
    in-process handlers consume. UC-1 handlers read via `.get()`
    (`context.get("tenant_id")`, `context.get("role")`).

    The keys threaded here are the **whole envelope context** a handler
    might legitimately need — kept future-proof so adding a new substrate
    signal (locale, region, …) doesn't require touching every handler.
    Handlers read what they care about and ignore the rest.
    """
    return {
        "tenant_id":   request.get("tenant_id", "") or "",
        "user_id":     request.get("user_id", "") or "",
        "role":        request.get("role", "") or "",
        "session_id":  request.get("session_id", "") or "",
        "request_id":  request.get("request_id", "") or "",
        # Focus channel — set by `update_focus`; consumed by UCs that
        # support multi-turn follow-ups without re-naming the entity.
        "focus_entity_id":  request.get("focus_entity_id", "") or "",
        "focus_service_id": request.get("focus_service_id", "") or "",
        # TimeFilter (serialised dict) — set conditionally by the route
        # node when the plan contains an agent with consumes_time_filter:
        # true. Empty dict ⇒ no temporal scope was requested.
        "time_filter": request.get("time_filter", {}) or {},
        # Locale: detected (G4) or tenant-default; handlers + the LLM
        # gateway use it for same-language reply (BEHAVIOR_CORPUS §C13).
        "locale":      request.get("locale", "") or "",
        # Free-form ABAC attribute pass-through.
        "attributes":  request.get("attributes", {}) or {},
        # Hot conversation window (G2 already trims). Optional — handlers
        # that need prior context read it; most ignore it.
        "conversation_history": request.get("conversation_history", []) or [],
        # Data-flow binding (previous_results). `bound_inputs` is the resolved
        # {to_param: value} map from declared upstream bindings; `previous_results`
        # is the raw upstream results so a handler can read deps directly. Both
        # empty unless this step declared bindings — handlers ignore them
        # otherwise (zero behaviour change for existing UCs).
        "bound_inputs": request.get("bound_inputs", {}) or {},
        "previous_results": request.get("previous_results", {}) or {},
    }


__all__ = [
    "StepExecutor",
    "EchoStepExecutor",
    "HandlerStepExecutor",
    "make_result",
]
