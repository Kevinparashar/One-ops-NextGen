"""NATS invoker — ingress-side adapter that ships an envelope to the graph
worker over the message bus and awaits the worker's reply.

When `UC_INVOKER_MODE=nats`, the FastAPI ingress calls `nats_invoke(envelope)`
instead of running the LangGraph executor in-process. The ingress becomes
the I/O edge of the system; the worker is a separately-scalable process
that consumes the queue group `oneops-graph`. Same code path serves the
single-process demo (worker embedded in the same uvicorn) AND production
(worker as its own deployment).

Architecture invariants:

  * **Subject vocabulary:** ingress publishes to `oneops.request.chat`
    (a queue group: any worker replica may handle it). Replies land on a
    per-request inbox (`oneops.response.<request_id>`), so concurrent
    ingress turns never cross-talk.
  * **Tenant isolation in the subject** — tenant_id is embedded in the
    envelope, never in the subject; subject-routing therefore cannot
    leak across tenants. The worker enforces tenant binding on every
    handler (existing G5 path).
  * **OTel propagation** — the underlying `NATSClient.request` injects
    W3C traceparent into NATS headers. The worker's `nats.process` span
    nests under the ingress's `oneops.api.turn` span. End-to-end traces
    work even with the process split.
  * **No silent failure** — a NATS unavailability raises a typed
    `NATSUnavailableError`; the ingress maps it to a 503. A timeout
    raises the same; never a silent fallback to in-process (that would
    hide a production incident).
"""
from __future__ import annotations

import json
from typing import Any

from oneops.adapters.nats_client import get_nats_client
from oneops.errors import NATSUnavailableError
from oneops.observability import get_logger, get_tracer

_log = get_logger("oneops.api.nats_invoker")
_tracer = get_tracer("oneops.api.nats_invoker")

REQUEST_SUBJECT = "oneops.request.chat"


async def nats_invoke(
    envelope: dict[str, Any], *, timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Ship the request envelope to the graph worker via NATS, return the
    worker's reply.

    The envelope round-trips as JSON — the same shape `run_turn` accepts
    locally, so the worker doesn't transform anything. The reply is the
    same TurnResponse-shaped dict the in-process path produces.

    Raises:
      * `NATSUnavailableError` — no worker subscribed, NATS down, or the
        reply did not arrive within `timeout_s`. The ingress maps these
        to HTTP 503 so the client retries.
    """
    client = await get_nats_client()
    payload = json.dumps(envelope, default=str).encode("utf-8")
    with _tracer.start_as_current_span(
        "oneops.nats_invoke",
        attributes={
            "oneops.request_id": envelope.get("request_id", ""),
            "oneops.tenant_id": envelope.get("tenant_id", ""),
            "oneops.user_id": envelope.get("user_id", ""),
            "oneops.entry_mode": envelope.get("entry_mode", ""),
            "nats.subject.request": REQUEST_SUBJECT,
        },
    ) as span:
        from oneops.adapters.nats_resilience import resilient_call

        async def _one_request() -> bytes:
            return await client.request(
                REQUEST_SUBJECT, payload, timeout=timeout_s)
        try:
            reply_bytes = await resilient_call(
                _one_request,
                subject=REQUEST_SUBJECT,
                tenant_id=envelope.get("tenant_id", ""))
        except NATSUnavailableError:
            span.set_attribute("error", True)
            raise
        try:
            reply = json.loads(reply_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            span.set_attribute("error", True)
            _log.warning("nats_invoke.malformed_reply", error=str(exc)[:200])
            raise NATSUnavailableError(
                "graph worker reply was not valid JSON", cause=exc) from exc
        span.set_attribute("oneops.reply_status",
                           str(reply.get("final_status", "")))
        return reply


__all__ = ["nats_invoke", "REQUEST_SUBJECT"]
