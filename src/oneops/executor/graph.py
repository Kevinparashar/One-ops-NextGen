"""LangGraph executor graph (P6) — the orchestration runtime.

This is where LangGraph is the framework. The compiled `StateGraph` runs one
user turn end to end:

    START → route → ┬ routed ─→ wave ⇄ run_step (Send fan-out) → aggregate → END
                    └ else  ──→ boundary ─────────────────────────────────→ END

  * `Send` fans a wave of independent steps out to parallel `run_step`
    invocations; dependent steps wait for their wave (the `wave ⇄ run_step`
    loop, driven by `dispatch_wave`).
  * the checkpointer persists every state snapshot — a crash mid-run resumes
    from the last wave; an `interrupt()` (action approval) pauses durably.
  * `interrupt()` inside `run_step` gates action-tier steps on user approval.

Checkpointer: `InMemorySaver` by default (dev/tests). Production uses an
`AsyncPostgresSaver` against a **dedicated** database (ADR-0004) — never the
shared application DB. `build_postgres_checkpointer()` is the wiring; it is
env-gated and not exercised where there is no database.
"""
from __future__ import annotations

import os
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from oneops.errors import UpstreamError
from oneops.executor.boundary import BoundaryResponder, DeterministicBoundaryResponder
from oneops.executor.hooks import HookRegistry, default_hook_registry
from oneops.executor.nodes import ExecutorNodes, dispatch_wave, route_branch
from oneops.executor.state import ExecutorState
from oneops.executor.step_runner import EchoStepExecutor, StepExecutor
from oneops.observability import (
    get_logger,
    get_tracer,
    histogram,
    increment,
    safe_hash_text,
    safe_text_len,
)
from oneops.registry.service import RegistryService
from oneops.router.router import Router

_log = get_logger("oneops.executor.graph")
_tracer = get_tracer("oneops.executor.graph")


def build_executor_graph(
    router: Router,
    registry: RegistryService,
    *,
    step_executor: StepExecutor | None = None,
    hooks: HookRegistry | None = None,
    boundary: BoundaryResponder | None = None,
    session_store: Any | None = None,
    policy_engine: Any | None = None,
    authz_service: Any | None = None,
    conversation_trimmer: Any | None = None,
    checkpointer: Any | None = None,
    focus_intent_classifier: Any | None = None,
    time_filter_extractor: Any | None = None,
) -> Any:
    """Compile the executor `StateGraph`.

    Defaults are the no-infrastructure implementations — `EchoStepExecutor`,
    the built-in hook registry, the deterministic boundary responder,
    `InMemorySaver`. Production injects the P7 tool-running executor, the LLM
    boundary responder, the Postgres checkpointer, and a `SessionEventStore`.

    `session_store` carries conversational memory: `load_session` reads the
    recent history before routing; `persist` appends the turn after. When it
    is None the turn is stateless.
    """
    nodes = ExecutorNodes(
        router=router,
        registry=registry,
        step_executor=step_executor or EchoStepExecutor(),
        hooks=hooks or default_hook_registry(),
        boundary=boundary or DeterministicBoundaryResponder(),
        session_store=session_store,
        policy_engine=policy_engine,
        authz_service=authz_service,
        conversation_trimmer=conversation_trimmer,
        focus_intent_classifier=focus_intent_classifier,
        time_filter_extractor=time_filter_extractor,
    )

    # Per-node retry for the LLM-bearing DECISION nodes only. Both are
    # read-only/idempotent (classify + plan — no writes), so re-running on a
    # transient upstream blip is safe. Scoped to `UpstreamError` (LLM
    # upstream/timeout/rate-limit, cache, NATS) so logic errors are NOT retried,
    # and gateway-exhausted `LLMGatewayError` (which already had its own internal
    # retries) is not retried again. Defaults: backoff 0.5s ×2, jitter on.
    _llm_node_retry = RetryPolicy(max_attempts=3, retry_on=UpstreamError)

    g: StateGraph = StateGraph(ExecutorState)
    g.add_node("load_session", nodes.load_session)
    g.add_node("update_focus", nodes.update_focus)
    g.add_node("control_gate", nodes.control_gate, retry_policy=_llm_node_retry)
    g.add_node("route", nodes.route, retry_policy=_llm_node_retry)
    g.add_node("wave", nodes.wave)
    g.add_node("run_step", nodes.run_step)
    g.add_node("aggregate", nodes.aggregate)
    g.add_node("boundary", nodes.boundary)
    g.add_node("persist", nodes.persist)

    g.add_edge(START, "load_session")
    # `update_focus` runs deterministically after history loads and before
    # any LLM consumer (control_gate / route / rewriter / field_read). It
    # writes a structured `focus_entity_id` / `focus_service_id` channel
    # into state — the single source of truth for the active subject of
    # the conversation. Downstream layers READ from state instead of
    # re-deriving focus from history text (which is what caused the
    # stale-focus / linked-record-drift bug class).
    g.add_edge("load_session", "update_focus")

    # Stage-1 conversation-control gate runs immediately after focus
    # update but BEFORE routing. A greeting / thanks / farewell / etc.
    # short-circuits straight to persist; everything else falls through to
    # the entry branch (fast-path vs chat). Fast-path BUTTON entries
    # bypass the gate entirely — UI-declared intent never needs the
    # social classifier.
    def _post_focus_branch(state) -> str:
        if state.get("entry_mode") == "fast_path" and state.get("plan"):
            return "wave"                    # fast-path button
        return "control_gate"

    g.add_conditional_edges("update_focus", _post_focus_branch,
                            {"control_gate": "control_gate", "wave": "wave"})

    # After the gate: if `final_response` is set (gate fired), go straight
    # to persist; otherwise route normally.
    def _post_gate_branch(state) -> str:
        if (state.get("final_response") and
                state.get("control_gate_outcome") not in ("", "none")):
            return "persist"
        return "route"

    g.add_conditional_edges("control_gate", _post_gate_branch,
                            {"persist": "persist", "route": "route"})
    g.add_conditional_edges("route", route_branch,
                            {"execute": "wave", "boundary": "boundary"})
    # dispatch_wave returns either a list[Send] (→ run_step ×N) or "aggregate".
    g.add_conditional_edges("wave", dispatch_wave,
                            {"run_step": "run_step", "aggregate": "aggregate"})
    g.add_edge("run_step", "wave")          # loop: next wave
    g.add_edge("aggregate", "persist")      # record the turn in conversation memory
    g.add_edge("boundary", "persist")
    g.add_edge("persist", END)

    compiled = g.compile(checkpointer=checkpointer or InMemorySaver())
    _log.info("executor.graph_compiled",
              checkpointer=type(checkpointer or InMemorySaver()).__name__)
    return compiled


async def run_turn(
    graph: Any, envelope: dict[str, Any], *, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Run one user turn through the compiled graph.

    `thread_id` (LangGraph's checkpoint key) defaults to the session id, so
    multi-turn conversations and interrupt/resume share one durable thread.
    """
    cfg: dict[str, Any] = dict(config or {})
    configurable = dict(cfg.get("configurable") or {})
    configurable.setdefault(
        "thread_id",
        envelope.get("session_id") or envelope.get("request_id") or "default")
    cfg["configurable"] = configurable
    # The wave loop (wave ⇄ run_step) consumes several supersteps per wave;
    # lift the default recursion limit so multi-wave plans complete. This is the
    # framework-level backstop for deep runtime step generation — operator-tunable
    # via env (not hardcoded) so large multi-wave plans get headroom without code.
    cfg.setdefault("recursion_limit",
                   int(os.getenv("ONEOPS_EXECUTOR_RECURSION_LIMIT", "60")))

    # Root span for the whole turn — every node/tool/LLM span created during
    # `ainvoke` nests under it (context propagation), so one user request is
    # one connected trace with no orphan spans. PII-safe: the raw message is
    # hashed into an attribute, never carried verbatim.
    import time as _time
    message = str(envelope.get("message", ""))
    t0 = _time.monotonic()
    with _tracer.start_as_current_span(
        "oneops.request",
        attributes={
            "oneops.request_id": envelope.get("request_id", ""),
            "oneops.tenant_id": envelope.get("tenant_id", ""),
            "oneops.user_id": envelope.get("user_id", ""),
            "oneops.session_id": envelope.get("session_id", ""),
            "oneops.role": envelope.get("role", ""),
            "oneops.message_hash": safe_hash_text(message),
            "oneops.message_len": safe_text_len(message),
        },
    ) as span:
        result = await graph.ainvoke(envelope, config=cfg)
        final_status = str(result.get("final_status") or "unknown")
        span.set_attribute("oneops.final_status", final_status)
        latency_ms = int((_time.monotonic() - t0) * 1000)
        histogram("ai.request.latency_ms", value=latency_ms,
                  tenant_id=envelope.get("tenant_id", ""))
        increment("ai.requests.total", final_status=final_status,
                  tenant_id=envelope.get("tenant_id", ""))
        return result


async def build_postgres_checkpointer() -> Any:
    """Build an `AsyncPostgresSaver` against the DEDICATED checkpoint
    database (ADR-0004).

    Production discipline:
      * Reads `LANGGRAPH_POSTGRES_URL`. Refuses to boot if equal to
        `POSTGRES_URL` (the shared app DB) — the 2026-05-16 incident
        wiped that DB exactly once; never let it happen again.
      * Opens a bounded `AsyncConnectionPool` (min=1, max=10). The pool
        is attached to `_owned_pool` on the saver so the API factory's
        shutdown path can close it cleanly.
      * Runs `setup()` once at boot — idempotent (creates the
        checkpoint tables if absent, no-op when present).
      * Loud failure on any of the above — never silently fall back to
        an in-memory saver. The caller (`app.py`) handles the boot-gate
        decision; this function only succeeds or raises.
    """
    import os

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    dsn = (os.getenv("LANGGRAPH_POSTGRES_URL") or "").strip()
    if not dsn:
        raise RuntimeError(
            "LANGGRAPH_POSTGRES_URL is not set — the executor checkpointer "
            "requires a dedicated checkpoint database (ADR-0004)")
    app_dsn = (os.getenv("POSTGRES_URL") or "").strip()
    if app_dsn and dsn == app_dsn:
        raise RuntimeError(
            "LANGGRAPH_POSTGRES_URL must NOT equal POSTGRES_URL — the "
            "checkpointer runs schema DDL and would clobber the shared "
            "app database (ADR-0004; 2026-05-16 incident).")

    # psycopg expects "postgresql://" or "postgres://"; reject anything else
    # so a misconfigured DSN is a hard boot failure, not a runtime mystery.
    if not (dsn.startswith(("postgresql://", "postgres://"))):
        raise RuntimeError(
            f"LANGGRAPH_POSTGRES_URL must be a postgresql:// DSN; got "
            f"{dsn[:30]!r}…")

    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=int(os.getenv("LANGGRAPH_POSTGRES_POOL_MIN", "1")),
        max_size=int(os.getenv("LANGGRAPH_POSTGRES_POOL_MAX", "10")),
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=False,
    )
    await pool.open(wait=True, timeout=10.0)
    saver = AsyncPostgresSaver(conn=pool)
    await saver.setup()
    # Expose the pool so the API factory shutdown closes it cleanly.
    saver._owned_pool = pool                              # noqa: SLF001 — boundary
    return saver


__all__ = ["build_executor_graph", "run_turn", "build_postgres_checkpointer"]
