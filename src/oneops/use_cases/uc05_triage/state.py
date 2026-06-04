"""LangGraph state for UC-5 Triage (Phase 3).

The state flows: fetch → Tool 1 → fan-out [Tool 2 ∥ Tool 3] → assemble → Proposal.
The `interrupt()` for Yes/No is *not* in the graph (it's handled at the API
boundary: /propose returns the Proposal, /decide resumes with the choice).
This keeps the graph synchronous and the checkpoint optional for dev.

Annotations on the fan-in fields make LangGraph merge parallel branches:
  assignment and prioritization land on the state in any order; the
  assemble node only fires when both are non-None.
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from oneops.use_cases.uc05_triage.contracts import (
    AssignmentRecommendation,
    DuplicateCheckResult,
    PrioritizationResult,
    Proposal,
)


def _take_last(_a, b):
    """Reducer used on parallel fan-in slots — last-write-wins is fine
    because each branch writes exactly one of them."""
    return b


class UC05State(TypedDict, total=False):
    """The shared state passed between LangGraph nodes."""

    # Inputs (set by API entry)
    tenant_id: str
    service_id: str          # "incident" | "request"
    ticket_id: str
    ticket_row: dict[str, Any]

    # Tool outputs
    duplicate: Annotated[DuplicateCheckResult | None, _take_last]
    assignment: Annotated[AssignmentRecommendation | None, _take_last]
    prioritization: Annotated[PrioritizationResult | None, _take_last]

    # Assembled proposal
    proposal: Annotated[Proposal | None, _take_last]
