"""ToolStepExecutor — the real `StepExecutor` for the P6 graph.

Replaces `EchoStepExecutor`. For one plan step it resolves the agent from the
registry and runs the agent's declared tools through the `ToolRunner` (timeout,
idempotency, fault containment, output capping all enforced there).

P7 runs the agent's tools as a deterministic compound action — every declared
tool, in registry order, with the step's parameters. LLM-driven *selective*
tool calling (a ReAct loop deciding which tool, with what args) arrives with
the LLM gateway in P8 as a separate `StepExecutor`; the graph swaps executors
without changing.

The returned step-result `output` is JSON-safe (a `VariableRef` is reduced to
its preview dict) so it can live in the checkpointed graph state.
"""
from __future__ import annotations

from typing import Any

from oneops.executor.step_runner import make_result
from oneops.observability import get_logger, get_tracer, increment
from oneops.registry.service import RegistryService
from oneops.toolrunner.context import ToolContext
from oneops.toolrunner.runner import ToolRunner

_log = get_logger("oneops.toolrunner.step")
_tracer = get_tracer("oneops.toolrunner.step")


def _jsonable(value: Any) -> Any:
    """Reduce a `VariableRef` to a JSON-safe preview dict; pass others through."""
    if getattr(value, "is_variable_ref", False):
        return {
            "variable_ref": value.name,
            "preview": value.preview,
            "size_bytes": value.size_bytes,
        }
    return value


class ToolStepExecutor:
    """A `StepExecutor` that runs an agent's tools via the `ToolRunner`."""

    def __init__(self, registry: RegistryService, tool_runner: ToolRunner) -> None:
        self._registry = registry
        self._runner = tool_runner

    async def run(self, step: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        agent_id = step.get("agent_id", "")
        with _tracer.start_as_current_span(
            "toolrunner.step",
            attributes={"oneops.agent_id": agent_id,
                        "executor.step_id": step.get("step_id", "")},
        ) as span:
            agent = self._registry.agents.get_optional(agent_id)
            if agent is None:
                return make_result(
                    step, status="failed",
                    error=f"agent '{agent_id}' has no active registry record")

            if not agent.tool_refs:
                # A tool-less agent's work is pure LLM reasoning — that path
                # lands in P8. P7 has nothing to run; report it honestly.
                return make_result(
                    step, status="success",
                    output={"note": "agent declares no tools — LLM-only "
                                     "execution is wired in P8"})

            params = dict(step.get("parameters") or {})
            # A stable idempotency base: the envelope's idempotency_key when
            # present (survives NATS re-delivery), else the request id.
            base = request.get("idempotency_key") or request.get("request_id") or ""

            # Build the frozen ToolContext once per step — every tool in the
            # step shares the same ambient context (Moveworks: per-request
            # context, not per-tool).
            ctx = ToolContext.from_request(request)

            tool_outputs: dict[str, Any] = {}
            failures: list[str] = []
            for ref in agent.tool_refs:
                tool = self._registry.tools.get_optional(ref.tool_id, ref.version)
                if tool is None:
                    failures.append(f"{ref.tool_id}: no active registry record")
                    continue
                idem = (f"{base}:{step.get('step_id')}:{tool.id}"
                        if base else None)
                result = await self._runner.run(
                    tool, params, context=ctx, idempotency_key=idem)
                increment("ai.tool.calls.total", tool_id=tool.id,
                          status=result.status.value)
                tool_outputs[tool.id] = _jsonable(result.output) if result.ok else None
                if not result.ok:
                    failures.append(f"{tool.id}: {result.error}")

            status = "failed" if failures else "success"
            span.set_attribute("toolrunner.step_status", status)
            span.set_attribute("toolrunner.tools_run", len(tool_outputs))
            return make_result(
                step, status=status,
                output={"agent_id": agent_id, "tools": tool_outputs},
                error="; ".join(failures) or None)


__all__ = ["ToolStepExecutor"]
