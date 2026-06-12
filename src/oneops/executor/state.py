"""ExecutorState — the LangGraph graph state (P6).

The state is the single object every node reads and writes. It is plain
JSON-serialisable data (dict / list / str / int / bool) so the checkpointer
(ADR-0004) can persist any snapshot — that is what makes a run resumable after
a crash.

`step_results` is the one channel written concurrently: `Send` fans a wave of
steps out to parallel `run_step` invocations, each returning one result. Its
reducer (`merge_step_results`) merges those partial writes deterministically,
deduplicating by `step_id` — so a replayed checkpoint never double-counts.
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict


def merge_step_results(
    left: list[dict[str, Any]] | None,
    right: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Reducer for `step_results` — merge two partial lists, dedup by step_id.

    `Send` runs N `run_step` nodes in parallel; each returns `step_results:
    [one_result]`. LangGraph calls this reducer to fold them into the parent
    state. Idempotent — re-merging the same result never duplicates it.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for source in (left or [], right or []):
        for result in source:
            sid = result.get("step_id")
            key = sid if sid is not None else f"__anon_{len(by_id)}"
            by_id[key] = result
    return list(by_id.values())


def merge_plan(
    left: list[dict[str, Any]] | None,
    right: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Reducer for `plan` — union by step_id, first definition wins, order kept.

    The router writes the initial plan once. A `run_step` may then *append*
    runtime-generated steps (dynamic fan-out — e.g. a KG traversal that returns
    N affected CIs, each needing its own read): every parallel `Send` returns
    `plan: [new_step, ...]` and LangGraph folds them in here, exactly like
    `merge_step_results` does for results. Two properties make this safe:

      * **First-wins** — a step_id already in the plan keeps its original
        definition, so a re-appended id (or a replayed checkpoint) never
        rewrites a planned step. Generated steps carry fresh, namespaced ids.
      * **Order preserved** — original steps first, generated steps in append
        order. `aggregate` builds its render order from this list, so stable
        ordering matters.

    `dispatch_wave` re-reads `plan` every superstep, so appended steps run in a
    later wave with no graph recompile and no topology change.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for source in (left or [], right or []):
        for step in source:
            sid = step.get("step_id")
            key = sid if sid is not None else f"__anon_{len(by_id)}"
            if key not in by_id:          # first-wins: never rewrite a planned step
                by_id[key] = step
    return list(by_id.values())


class ExecutorState(TypedDict, total=False):
    """The graph's source of truth. All fields JSON-serialisable for checkpointing."""

    # ── Request envelope (immutable per turn) ────────────────────────────
    request_id: str
    tenant_id: str
    session_id: str
    user_id: str
    role: str
    message: str

    # ── Conversational memory (loaded by load_session, persisted by persist) ─
    # Recent prior turns, oldest-first: [{"role": "user"|"assistant", "content": ...}].
    # The router's rewriter resolves references ("close it", "same as last
    # time") against this. The full durable log lives in the P3 session store;
    # this is the hot window loaded for the turn.
    conversation_history: list[dict[str, str]]

    # ── Pre-routed dispatch (forced-agent path) ──────────────────────────
    # When set, the `route` node SKIPS the LLM router and builds the plan
    # directly from these agent ids — a first-class "pre-routed" execution path
    # for callers that already selected the agent(s): a team manager's
    # member-selector, the button/HTTP routes, the /propose fast-path. Ids with
    # no active registry record are dropped (never invent an agent); if none
    # survive, the turn is a no-confident-match. Empty/absent → normal routing.
    forced_agent_ids: list[str]

    # ── Active focus (Stage 2 LangGraph-native fix, 2026-05-28) ──────────
    # The single source of truth for "which record is the conversation
    # about." Computed deterministically by `update_focus` at the start of
    # every turn (current-message entity if present, else carried from
    # prior state via the checkpointer). Read by the rewriter (pronoun
    # resolution), the router (Stage-3 entity backstop), and any UC that
    # needs to know the active subject. Empty string when no focus is
    # active (fresh session, no entity yet named).
    focus_entity_id: str          # e.g. "CHG0004003"
    focus_service_id: str         # e.g. "change"

    # ── Stage-1 conversation-control gate (pre-router) ───────────────────
    # Set by `control_gate` to one of the labels in ControlType. When the
    # gate fires (non-`none`, non-`fallthrough`), `final_response` is also
    # populated and the graph short-circuits to `persist`. When the gate
    # falls through, this is `"none"` (or empty if the gate didn't run).
    control_gate_outcome: str

    # ── Routing output (written by route_node) ───────────────────────────
    route_outcome: str            # "routed" | "no_confident_match" | "entity_clarification" | "policy_denied"
    boundary_reason: str          # set when route_outcome != "routed"
    # On-screen text asking the user to correct a malformed entity ID. Set by
    # `route` whenever the message contained a near-miss reference; the
    # boundary node emits it (all-malformed turn) or aggregate appends it as a
    # note (good IDs alongside a bad one). Never left silent (thumb rule #11).
    entity_clarification: str
    # Serialised plan steps: {step_id, agent_id, parameters: dict, depends_on: list}.
    # Append-reducer (`merge_plan`): the router seeds it once, and a `run_step`
    # may append runtime-generated steps — Send-safe, first-wins, order kept.
    plan: Annotated[list[dict[str, Any]], merge_plan]
    unrouted: list[str]           # sub-query texts that did not route (partial)
    route_diagnostics: list[str]

    # ── Execution accumulator (Send-safe via the reducer) ─────────────────
    # Each result: {step_id, agent_id, status, output, error, hooks_run}.
    step_results: Annotated[list[dict[str, Any]], merge_step_results]

    # ── Final assembled response ──────────────────────────────────────────
    final_status: str             # "executed" | "partial" | "clarification" | ...
    final_response: str

    # ── TimeFilter (set conditionally by `route` after the plan is built) ──
    # Serialised `TimeFilter` (dict via .model_dump(mode="json")) when the
    # plan contains at least one agent whose registry record sets
    # `consumes_time_filter: true`. Empty dict otherwise. Threaded through
    # the request envelope so every tool that opts in (UC-2 today, UC-3/UC-5
    # later) reads the same scope from `context["time_filter"]`.
    time_filter: dict[str, Any]

    # ── Entry mode (set by the ingress, read only at graph start) ────────
    # "" / unset → chat ingress: load_session → route → wave …
    # "fast_path" → /fast/{uc_id} ingress: pre-built plan already in state;
    #               skip the router and go load_session → wave …
    # Persisted slots from a prior turn never satisfy the fast-path check —
    # only an explicit "fast_path" stamp does.
    entry_mode: str


def serialise_plan(plan_steps: Any, turn_id: str = "") -> list[dict[str, Any]]:
    """Turn a router `RoutePlan`'s steps into the JSON-shaped list the state
    holds. Accepts the `RoutePlan` object or its `.steps`.

    `turn_id` stamps each step with the request_id of the turn that planned it.
    On a checkpointed / interrupt-held session the `plan` and `step_results`
    channels PERSIST across turns (accumulating reducers), so `aggregate` would
    otherwise re-render a prior turn's results. The turn stamp lets `aggregate`
    keep only the current turn's plan + results — the multi-turn consistency fix.

    Data-flow fields (`parameter_bindings`, `dependency_types`) are emitted
    only when present, so existing plans serialise byte-identically to before.
    """
    steps = getattr(plan_steps, "steps", plan_steps)
    out: list[dict[str, Any]] = []
    for s in steps:
        d: dict[str, Any] = {
            "step_id": s.step_id,
            "agent_id": s.agent_id,
            "parameters": dict(s.parameters),
            "depends_on": list(s.depends_on),
        }
        if turn_id:
            d["turn_id"] = turn_id
        bindings = getattr(s, "parameter_bindings", ()) or ()
        if bindings:
            d["parameter_bindings"] = [
                {"from_step": b.from_step, "from_field": b.from_field,
                 "to_param": b.to_param, "required": b.required}
                for b in bindings
            ]
        dep_types = getattr(s, "dependency_types", ()) or ()
        if dep_types:
            d["dependency_types"] = [list(t) for t in dep_types]
        out.append(d)
    return out


__all__ = ["ExecutorState", "merge_step_results", "merge_plan", "serialise_plan"]
