"""UC-5 triage plan — the orchestration expressed as DATA (B-refactor Phase 2b).

UC-5 used to own a bespoke LangGraph (`graph.py`: check → [assign ∥ prio] →
assemble). This module expresses that exact DAG as a serialised executor plan so
the MAIN executor runs it — like every other UC (agents-as-data). The plan is fed
to the executor via the fast-path entry (`entry_mode="fast_path"` + this plan in
state), so routing/disambiguation are skipped (UC-5 is API-only) but every safety
stage (load_session, policy, authz_recheck, hooks, per-tool action gate) runs.

Shape (identical to the old graph):

    check_duplicate_candidates
        ├─(candidates)──────────────→ recommend_assignment ─┐
        └─(category/subcategory)────→ prioritize_entity ─────┤
                                                             ▼
                                              assemble_triage_proposal → Proposal

Data-flow binding carries values between steps (no per-handler glue):
  * recommend_assignment.candidates  ← check.candidates       (optional/soft:
    absent ⇒ empty candidate set ⇒ the "no neighbours" recommendation, exactly
    the old graph's fallback)
  * prioritize.suggested_(sub)category ← check.suggested_*    (optional/soft)
  * assemble reads all three upstream outputs from `previous_results`.

The step ids are shared constants so the assemble handler can locate each
upstream result deterministically (it reconstructs the typed contracts from the
serialised outputs). Tool ids are stamped explicitly because check and prioritize
share a required-param shape — the executor's param-shape heuristic cannot tell
them apart, so the plan names the tool for each step (Phase 2b-i).
"""
from __future__ import annotations

from typing import Any

AGENT_ID = "uc05_triage"

STEP_CHECK = "uc05_check"
STEP_ASSIGN = "uc05_assign"
STEP_PRIO = "uc05_prio"
STEP_ASSEMBLE = "uc05_assemble"

TOOL_CHECK = "check_duplicate_candidates"
TOOL_ASSIGN = "recommend_assignment"
TOOL_PRIO = "prioritize_entity"
TOOL_ASSEMBLE = "assemble_triage_proposal"


def build_triage_plan(*, service_id: str, ticket_id: str) -> list[dict[str, Any]]:
    """Return the serialised executor plan for triaging one ticket.

    Pure: no I/O, deterministic for a given (service_id, ticket_id). The result
    is the value placed on the executor's `plan` channel for a fast-path turn.
    """
    return [
        {
            "step_id": STEP_CHECK,
            "agent_id": AGENT_ID,
            "tool_id": TOOL_CHECK,
            "parameters": {"service_id": service_id, "ticket_id": ticket_id},
            "depends_on": [],
        },
        {
            "step_id": STEP_ASSIGN,
            "agent_id": AGENT_ID,
            "tool_id": TOOL_ASSIGN,
            "parameters": {},
            "depends_on": [STEP_CHECK],
            # candidates is optional: if the check produced none (or failed
            # soft), recommend_assignment defaults to an empty set and returns
            # the "no neighbours" recommendation — the old graph's fallback.
            "parameter_bindings": [
                {"from_step": STEP_CHECK, "from_field": "candidates",
                 "to_param": "candidates", "required": False},
            ],
            "dependency_types": [[STEP_CHECK, "soft"]],
        },
        {
            "step_id": STEP_PRIO,
            "agent_id": AGENT_ID,
            "tool_id": TOOL_PRIO,
            "parameters": {"service_id": service_id, "ticket_id": ticket_id},
            "depends_on": [STEP_CHECK],
            # category/subcategory are hints — optional, best-effort.
            "parameter_bindings": [
                {"from_step": STEP_CHECK, "from_field": "suggested_category",
                 "to_param": "suggested_category", "required": False},
                {"from_step": STEP_CHECK, "from_field": "suggested_subcategory",
                 "to_param": "suggested_subcategory", "required": False},
            ],
            "dependency_types": [[STEP_CHECK, "soft"]],
        },
        {
            "step_id": STEP_ASSEMBLE,
            "agent_id": AGENT_ID,
            "tool_id": TOOL_ASSEMBLE,
            "parameters": {"service_id": service_id, "ticket_id": ticket_id},
            "depends_on": [STEP_CHECK, STEP_ASSIGN, STEP_PRIO],
        },
    ]


__all__ = [
    "build_triage_plan",
    "AGENT_ID",
    "STEP_CHECK", "STEP_ASSIGN", "STEP_PRIO", "STEP_ASSEMBLE",
    "TOOL_CHECK", "TOOL_ASSIGN", "TOOL_PRIO", "TOOL_ASSEMBLE",
]
