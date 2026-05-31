"""API-side NATS dispatch for UC-8 fulfillment execution.

The button-mode /api/uc08/fulfill route persists the RITM + tasks
synchronously, then needs to kick off `executor.execute_plan` for the
actual workflow. When NATS is available, we publish that kick over NATS
to the UC-8 fulfillment agent (fire-and-forget). When NATS is down,
the API falls back to an in-process asyncio task — graceful degradation,
no silent loss.

This is the same pattern UC-5 uses for propose/decide
(`uc05_triage/nats_dispatcher.py`), adapted for the fire-and-forget
shape that fulfillment requires (the workflow takes ~30–300s; the API
returns as soon as the message is queued so the user sees immediate
status=running progress).
"""
from __future__ import annotations

import json

from oneops.adapters.nats_client import NATSClient
from oneops.observability import span

SUBJECT_FULFILL_EXECUTE = "oneops.uc08.fulfill.execute"
QUEUE_GROUP = "uc08-fulfill-workers"


async def dispatch_execute(
    *,
    nats: NATSClient,
    tenant_id: str,
    ritm_id: str,
    trace_id: str | None = None,
) -> None:
    """Publish a fulfillment-execute event over NATS (fire-and-forget).

    Returns as soon as the NATS broker has accepted the message. The
    actual workflow runs in the UC-8 agent (`agent.py`); progress is
    visible via /api/uc08/status/{ritm_id} polling against Postgres.
    """
    payload = json.dumps({
        "tenant_id": tenant_id,
        "ritm_id": ritm_id,
        "trace_id": trace_id,
    }).encode("utf-8")
    with span(
        "uc08.dispatch.execute",
        **{
            "oneops.tenant_id": tenant_id,
            "uc08.ritm_id": ritm_id,
            "nats.subject": SUBJECT_FULFILL_EXECUTE,
        },
    ):
        await nats.publish(SUBJECT_FULFILL_EXECUTE, payload)


__all__ = [
    "dispatch_execute",
    "SUBJECT_FULFILL_EXECUTE",
    "QUEUE_GROUP",
]
