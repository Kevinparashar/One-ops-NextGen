"""LangGraph wiring for UC-5 Triage (Phase 3).

Shape:

    START → check_duplicates → [recommend_assignment, prioritize] → assemble → END

The middle pair runs in parallel via LangGraph's native fan-out (two edges
from check_duplicates → both targets, both targets edge to assemble). The
reducers on UC05State.assignment and .prioritization slots merge the
parallel writes.

Checkpointer: InMemorySaver by default (matches executor pattern). Phase 4
swaps in a NATS-backed durable saver if requested.

The graph runs synchronously — there is NO `interrupt()` inside it. The
binary Yes/No approval gate lives at the API boundary (Section J):
/propose returns the Proposal, /decide resumes with the user's choice +
final_values. This keeps the graph idempotent and dev-friendly.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from oneops.observability import span
from oneops.use_cases.uc05_triage.assembly import assemble_proposal
from oneops.use_cases.uc05_triage.contracts import (
    AssignmentRecommendation,
    DuplicateCheckResult,
    PrioritizationResult,
)
from oneops.use_cases.uc05_triage.state import UC05State

# Tool-runner protocol types (injected by the builder, never imported globals).
CheckDuplicatesFn = Callable[..., Awaitable[DuplicateCheckResult]]
RecommendAssignmentFn = Callable[..., Awaitable[AssignmentRecommendation]]
PrioritizeFn = Callable[..., Awaitable[PrioritizationResult]]


def build_uc05_graph(
    *,
    check_duplicates: CheckDuplicatesFn,
    recommend_assignment: RecommendAssignmentFn,
    prioritize: PrioritizeFn,
    checkpointer: BaseCheckpointSaver | None = None,
) -> Any:
    """Build and compile the UC-5 LangGraph.

    Returns a compiled graph with `.ainvoke(state, config)` ready.
    Callers inject the three tool functions — they are responsible for
    binding tenant/user/gateway/store at call time.
    """

    # ── Nodes ────────────────────────────────────────────────────────────────

    async def node_check_duplicates(state: UC05State) -> UC05State:
        with span("uc05.graph.check_duplicates",
                  **{"oneops.tenant_id": state.get("tenant_id"),
                     "uc05.service_id": state.get("service_id"),
                     "uc05.ticket_id": state.get("ticket_id")}):
            dup = await check_duplicates(
                ticket_row=state["ticket_row"],
                service_id=state["service_id"],
                tenant_id=state["tenant_id"],
            )
        return {"duplicate": dup}

    async def node_recommend_assignment(state: UC05State) -> UC05State:
        with span("uc05.graph.recommend_assignment"):
            dup = state.get("duplicate")
            if dup is None:
                # No duplicate-check yet — return empty AssignmentRecommendation
                asn = AssignmentRecommendation(
                    assignment_group=None, confidence=0.0, coverage=0.0,
                    diversity=0, basis_ids=[], basis="empty_neighbours",
                    rationale="no neighbours available",
                )
            else:
                asn = await recommend_assignment(
                    candidates=dup.candidates,
                    probe_text=str(state["ticket_row"].get("title") or ""),
                    ticket_row=dict(state["ticket_row"]),
                )
        return {"assignment": asn}

    async def node_prioritize(state: UC05State) -> UC05State:
        with span("uc05.graph.prioritize"):
            dup = state.get("duplicate")
            pri = await prioritize(
                service_id=state["service_id"],
                ticket_row=dict(state["ticket_row"]),
                suggested_category=(dup.suggested_category if dup else None),
                suggested_subcategory=(dup.suggested_subcategory if dup else None),
            )
        return {"prioritization": pri}

    async def node_assemble(state: UC05State) -> UC05State:
        with span("uc05.graph.assemble"):
            proposal = assemble_proposal(
                ticket_id=state["ticket_id"],
                service_id=state["service_id"],
                tenant_id=state["tenant_id"],
                duplicate=state["duplicate"],
                assignment=state["assignment"],
                prioritization=state["prioritization"],
            )
        return {"proposal": proposal}

    # ── Wiring ───────────────────────────────────────────────────────────────

    g: StateGraph = StateGraph(UC05State)
    g.add_node("check_duplicates", node_check_duplicates)
    g.add_node("recommend_assignment", node_recommend_assignment)
    g.add_node("prioritize", node_prioritize)
    g.add_node("assemble", node_assemble)

    g.add_edge(START, "check_duplicates")
    # Fan out — both edges fire after check_duplicates returns
    g.add_edge("check_duplicates", "recommend_assignment")
    g.add_edge("check_duplicates", "prioritize")
    # Fan in — assemble runs after BOTH have written their slot
    g.add_edge("recommend_assignment", "assemble")
    g.add_edge("prioritize", "assemble")
    g.add_edge("assemble", END)

    return g.compile(checkpointer=checkpointer or InMemorySaver())


__all__ = ["build_uc05_graph", "UC05State"]
