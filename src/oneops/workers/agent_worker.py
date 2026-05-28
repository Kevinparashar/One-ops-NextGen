"""AgentWorker — one NATS-addressable worker per agent_id.

Each worker subscribes on `oneops.agent.<agent_id>` (queue group
`oneops-agent-<agent_id>` for horizontal scale) and executes exactly
what `HandlerStepExecutor` would have executed in-process. The on-wire
contract:

    request  : {"step": {...}, "request": {...}}      (JSON utf-8)
    reply    : make_result(...)                         (JSON utf-8)

This makes agent-to-agent traffic flow over NATS so the demo can show
inter-agent activity in the NATS log alongside ingress↔graph traffic.
"""
from __future__ import annotations

import json
from typing import Any

from oneops.adapters.nats_client import get_nats_client
from oneops.executor.step_runner import HandlerStepExecutor, make_result
from oneops.observability import get_logger

_log = get_logger("oneops.workers.agent_worker")

SUBJECT_PREFIX = "oneops.agent"


def agent_subject(agent_id: str) -> str:
    return f"{SUBJECT_PREFIX}.{agent_id}"


def agent_queue(agent_id: str) -> str:
    return f"oneops-agent-{agent_id}"


class AgentWorker:
    """Owns one NATS subscription bound to one agent_id."""

    def __init__(self, agent_id: str, executor: HandlerStepExecutor) -> None:
        self._agent_id = agent_id
        self._executor = executor
        self._subscription = None

    async def start(self) -> None:
        if self._subscription is not None:
            return
        client = await get_nats_client()
        subject = agent_subject(self._agent_id)
        self._subscription = await client.subscribe(
            subject, handler=self._handle, queue=agent_queue(self._agent_id))
        _log.info("agent_worker.started",
                  agent_id=self._agent_id, subject=subject)

    async def stop(self) -> None:
        if self._subscription is None:
            return
        try:
            await self._subscription.drain()
        except Exception as exc:                          # noqa: BLE001
            _log.warning("agent_worker.drain_failed",
                         agent_id=self._agent_id, error=str(exc)[:200])
        self._subscription = None

    async def _handle(self, msg: Any) -> None:
        reply_to = getattr(msg, "reply", None)
        try:
            envelope = json.loads(msg.data.decode("utf-8"))
            step = envelope.get("step") or {}
            request = envelope.get("request") or {}
            result = await self._executor.run(step, request)
        except Exception as exc:                          # noqa: BLE001
            _log.warning("agent_worker.handler_raised",
                         agent_id=self._agent_id, error=str(exc)[:200])
            result = make_result(
                {"agent_id": self._agent_id},
                status="failed",
                error=f"agent_worker raised {type(exc).__name__}: {exc}")
        if not reply_to:
            return
        client = await get_nats_client()
        await client._nc.publish(                         # noqa: SLF001
            reply_to, json.dumps(result, default=str).encode("utf-8"))


__all__ = ["AgentWorker", "agent_subject", "agent_queue", "SUBJECT_PREFIX"]
