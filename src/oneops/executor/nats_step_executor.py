"""NatsStepExecutor — StepExecutor implementation that dispatches each
step to an AgentWorker over NATS request/reply.

Conforms to the `StepExecutor` Protocol (oneops.executor.step_runner.StepExecutor).
Drop-in replacement for `HandlerStepExecutor` when agent-to-agent
traffic must flow over the message bus instead of in-process calls.
"""
from __future__ import annotations

import json
from typing import Any

from oneops.adapters.nats_client import get_nats_client
from oneops.errors import NATSUnavailableError
from oneops.executor.step_runner import make_result
from oneops.observability import get_logger
from oneops.workers.agent_worker import agent_subject

_log = get_logger("oneops.executor.nats_step_executor")


class NatsStepExecutor:
    """Publishes `{step, request}` to `oneops.agent.<agent_id>` and
    awaits the worker's reply."""

    def __init__(self, *, timeout_s: float = 60.0) -> None:
        self._timeout_s = timeout_s

    async def run(
        self, step: dict[str, Any], request: dict[str, Any],
    ) -> dict[str, Any]:
        agent_id = str(step.get("agent_id") or "").strip()
        if not agent_id:
            return make_result(step, status="failed",
                               error="step has no agent_id")
        subject = agent_subject(agent_id)
        payload = json.dumps(
            {"step": step, "request": request}, default=str,
        ).encode("utf-8")
        try:
            client = await get_nats_client()
            from oneops.adapters.nats_resilience import resilient_call

            async def _one_request() -> bytes:
                return await client.request(
                    subject, payload, timeout=self._timeout_s)
            data = await resilient_call(
                _one_request, subject=subject,
                tenant_id=str(request.get("tenant_id") or ""))
        except NATSUnavailableError as exc:
            _log.warning("nats_step_executor.unavailable",
                         agent_id=agent_id, error=str(exc)[:200])
            return make_result(
                step, status="failed",
                error=f"agent {agent_id!r} unreachable over NATS: {exc}")
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _log.warning("nats_step_executor.bad_reply",
                         agent_id=agent_id, error=str(exc)[:200])
            return make_result(
                step, status="failed",
                error=f"agent {agent_id!r} returned malformed reply: {exc}")


__all__ = ["NatsStepExecutor"]
