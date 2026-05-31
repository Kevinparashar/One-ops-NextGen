"""UC-8 Fulfillment Agent — NATS-subscribed worker that runs execute_plan.

Subscribes to `oneops.uc08.fulfill.execute` (queue group `uc08-fulfill-workers`
for horizontal scale). When a fulfillment-execute event arrives, the
agent reads the RITM id from the envelope and calls `executor.execute_plan`,
which orchestrates the task DAG via LangGraph.

This is the symmetric counterpart of `uc05_triage/agent.py`. The on-wire
contract:

    request  : {"tenant_id": "...", "ritm_id": "...", "trace_id": "..."}
    reply    : (none — fire-and-forget; status is polled via /api/uc08/status)

Lifecycle: started at API boot when NATS is available, drained at
shutdown. Failures are logged and counted — they do NOT propagate back
to the publisher (the publish already succeeded).
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from oneops.adapters.nats_client import NATSClient
from oneops.observability import get_logger, span
from oneops.observability.metrics import increment

_log = get_logger("oneops.uc08.agent")

SUBJECT_FULFILL_EXECUTE = "oneops.uc08.fulfill.execute"
QUEUE_GROUP = "uc08-fulfill-workers"


# Type alias for the executor entry-point — same signature as
# `executor.execute_plan` so callers can pass it directly.
ExecutePlanFn = Callable[..., Awaitable[Any]]


class UC8FulfillmentAgent:
    """NATS subscriber that runs execute_plan for fulfillment requests."""

    def __init__(
        self,
        *,
        nats: NATSClient,
        execute_plan: ExecutePlanFn,
        connection_provider: Callable[[], Awaitable[Any]],
        adapter_factory: Callable[[], Any],
    ) -> None:
        self._nats = nats
        self._execute_plan = execute_plan
        self._cp = connection_provider
        self._adapter_factory = adapter_factory
        self._sub = None

    async def start(self) -> None:
        if self._sub is not None:
            return
        self._sub = await self._nats.subscribe(
            SUBJECT_FULFILL_EXECUTE,
            handler=self._on_execute,
            queue=QUEUE_GROUP,
        )
        _log.info(
            "uc08.agent.started",
            subject=SUBJECT_FULFILL_EXECUTE,
            queue=QUEUE_GROUP,
        )

    async def stop(self) -> None:
        if self._sub is None:
            return
        try:
            await self._sub.drain()
        except Exception as exc:                                # noqa: BLE001
            _log.warning("uc08.agent.drain_failed", error=str(exc)[:200])
        self._sub = None

    async def _on_execute(self, msg: Any) -> None:
        """Handle a fulfillment-execute event. Fire-and-forget — no reply."""
        try:
            envelope = json.loads(msg.data.decode("utf-8"))
            tenant_id = envelope["tenant_id"]
            ritm_id = envelope["ritm_id"]
            trace_id = envelope.get("trace_id")
        except Exception as exc:                                # noqa: BLE001
            _log.warning(
                "uc08.agent.envelope_invalid", error=str(exc)[:200],
            )
            increment(
                "ai.uc08.agent.events.total",
                outcome="envelope_invalid",
            )
            return

        with span(
            "uc08.agent.on_execute",
            **{
                "oneops.tenant_id": tenant_id,
                "uc08.ritm_id": ritm_id,
                "nats.subject": SUBJECT_FULFILL_EXECUTE,
            },
        ):
            _log.info(
                "uc08.agent.execute_received",
                tenant_id=tenant_id,
                ritm_id=ritm_id,
            )
            increment(
                "ai.uc08.agent.events.total",
                outcome="received",
                tenant_id=tenant_id,
            )
            try:
                await self._execute_plan(
                    tenant_id=tenant_id,
                    ritm_id=ritm_id,
                    adapter=self._adapter_factory(),
                    connection_provider=self._cp,
                    trace_id=trace_id,
                )
                increment(
                    "ai.uc08.agent.events.total",
                    outcome="completed",
                    tenant_id=tenant_id,
                )
            except Exception as exc:                            # noqa: BLE001
                _log.warning(
                    "uc08.agent.execute_failed",
                    tenant_id=tenant_id,
                    ritm_id=ritm_id,
                    error=str(exc)[:200],
                )
                increment(
                    "ai.uc08.agent.events.total",
                    outcome="failed",
                    tenant_id=tenant_id,
                )


__all__ = [
    "UC8FulfillmentAgent",
    "SUBJECT_FULFILL_EXECUTE",
    "QUEUE_GROUP",
]
