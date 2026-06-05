"""UC-5 executor-backed propose runner (B-refactor Phase 2b-iii).

Runs the triage propose flow on the MAIN executor — like every other UC — instead
of UC-5's bespoke runner+graph. It:

  1. builds the triage plan as data (`build_triage_plan`: check → [assign ∥ prio]
     → assemble),
  2. seeds it into a fast-path envelope (`entry_mode="fast_path"` + the plan), so
     the executor skips routing/disambiguation (UC-5 is API-only) but runs every
     safety stage — policy, the `authz_recheck` before-hook, the per-tool action
     gate, hooks, persist,
  3. runs one turn through the compiled main graph (`run_turn`),
  4. extracts the assembled `Proposal` from the terminal step's output.

The main graph already wires the registry `HandlerStepExecutor` + `AuthzService`,
so this runner needs only the graph. It is wired behind a flag
(`ONEOPS_UC05_EXECUTOR_PROPOSE`) at app boot and validated for Proposal-parity
against the legacy runner before the flip (Phase 3 retires the legacy path).

No silent failure (rule §2.7): a plan that doesn't reach assemble, an assemble
step that failed, or a propagated upstream error each raise a typed
`TriageExecutorError` the route maps to a 5xx — never a malformed/empty Proposal.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from oneops.errors import OneOpsError
from oneops.executor.graph import run_turn
from oneops.observability import get_logger, span
from oneops.use_cases.uc05_triage.contracts import Proposal
from oneops.use_cases.uc05_triage.plan import STEP_ASSEMBLE, build_triage_plan

_log = get_logger("oneops.use_cases.uc05_triage.executor_runner")

ExecutorProposeRunner = Callable[..., Awaitable[Proposal]]


class TriageExecutorError(OneOpsError):
    """The main executor could not produce a triage Proposal (surfaced, not silent)."""


def make_executor_propose_runner(graph: Any) -> ExecutorProposeRunner:
    """Build the executor-backed propose runner over a compiled main graph.

    Returned signature:
        async fn(*, service_id, ticket_id, tenant_id, user_id, role) -> Proposal
    """

    async def _runner(*, service_id: str, ticket_id: str, tenant_id: str,
                      user_id: str, role: str) -> Proposal:
        with span("uc05.executor_runner.invoke",
                  **{"oneops.tenant_id": tenant_id,
                     "uc05.service_id": service_id,
                     "uc05.ticket_id": ticket_id}):
            envelope = {
                "request_id": "req_" + uuid.uuid4().hex[:18],
                "tenant_id": tenant_id,
                "user_id": user_id,
                "role": role,
                # Throwaway session — the propose turn carries no chat memory.
                "session_id": "uc05_" + uuid.uuid4().hex[:12],
                "message": "",
                "entry_mode": "fast_path",
                "plan": build_triage_plan(
                    service_id=service_id, ticket_id=ticket_id),
            }
            out = await run_turn(graph, envelope)

            results = {r.get("step_id"): r
                       for r in (out.get("step_results") or [])}
            assemble = results.get(STEP_ASSEMBLE)
            if assemble is None:
                raise TriageExecutorError(
                    f"triage plan did not reach assemble for "
                    f"{service_id}/{ticket_id} (final_status="
                    f"{out.get('final_status')!r})")
            if assemble.get("status") != "success":
                raise TriageExecutorError(
                    f"assemble step {assemble.get('status')!r}: "
                    f"{assemble.get('error')}")
            output = assemble.get("output")
            if isinstance(output, dict) and "outcome" in output:
                # assemble propagated a typed upstream error (not_found, etc.)
                raise TriageExecutorError(
                    f"triage failed: {output.get('outcome')} — "
                    f"{output.get('message')}")
            return Proposal.model_validate(output)

    return _runner


__all__ = ["make_executor_propose_runner", "ExecutorProposeRunner",
           "TriageExecutorError"]
