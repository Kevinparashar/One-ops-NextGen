"""GraphWorker — consumes `oneops.request.chat` and runs the LangGraph
turn loop, replying to the per-request inbox.

The worker is the executor side of the ingress↔executor split. It owns
the compiled `StateGraph` + every substrate dependency (registry, router,
session store, etc.). One worker handles every message from the queue
group; replicas can be added behind NATS naturally — each message goes
to exactly one replica.

Lifecycle:

  * Construction binds the compiled graph (built by the API factory's
    lifespan, shared with the in-process invoker — same graph, same
    state, exactly one source of truth).
  * `start()` subscribes to `oneops.request.chat` with queue group
    `oneops-graph`. The NATSClient adapter handles trace-context
    propagation per message.
  * On message: deserialize envelope, run `run_turn`, serialize the
    reply, publish on `msg.reply`. Errors are caught at the boundary
    and reported as a typed failure envelope, never as a swallowed
    exception (no silent failure).
  * `stop()` drains the subscription so an in-flight turn finishes
    before the worker exits.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from oneops.adapters.nats_client import get_nats_client
from oneops.config import get_settings
from oneops.executor.graph import run_turn
from oneops.observability import get_logger, get_tracer, set_langfuse_io

_log = get_logger("oneops.workers.graph_worker")
_tracer = get_tracer("oneops.workers.graph_worker")

REQUEST_SUBJECT = "oneops.request.chat"
QUEUE_GROUP = "oneops-graph"


class GraphWorker:
    """Owns one compiled StateGraph + a NATS subscription."""

    def __init__(self, graph: Any, *, default_timeout_s: float | None = None) -> None:
        self._graph = graph
        # Env-tunable via GRAPH_WORKER_TIMEOUT_SECONDS (default 90.0); an explicit
        # caller-supplied value still wins. Behaviour unchanged at the default.
        self._timeout_s = (
            default_timeout_s if default_timeout_s is not None
            else get_settings().graph_worker_timeout_seconds)
        self._subscription = None

    async def start(self) -> None:
        """Subscribe to `oneops.request.chat`. Idempotent — calling
        twice on the same worker is a no-op (the NATS client tracks
        active subs)."""
        if self._subscription is not None:
            return
        client = await get_nats_client()
        self._subscription = await client.subscribe(
            REQUEST_SUBJECT, handler=self._handle, queue=QUEUE_GROUP)
        _log.info("graph_worker.started",
                  subject=REQUEST_SUBJECT, queue=QUEUE_GROUP)

    async def stop(self) -> None:
        """Drain + unsubscribe. Idempotent."""
        if self._subscription is None:
            return
        try:
            await self._subscription.drain()
        except Exception as exc:                          # noqa: BLE001 — best effort
            _log.warning("graph_worker.drain_failed", error=str(exc)[:200])
        self._subscription = None
        _log.info("graph_worker.stopped")

    async def _handle(self, msg: Any) -> None:
        """Per-message handler. The NATSClient.subscribe wrapper has
        already opened a `nats.process` span and propagated trace
        context from incoming headers."""
        t0 = time.monotonic()
        getattr(msg, "reply", None)
        try:
            envelope = json.loads(msg.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _log.warning("graph_worker.bad_payload", error=str(exc)[:200])
            await _publish_reply(msg, {
                "final_status": "failed",
                "final_response": "(graph worker: malformed envelope)",
                "step_results": [],
                "session_id": "", "request_id": "", "trace_id": None,
                "latency_ms": int((time.monotonic() - t0) * 1000),
            })
            return

        thread_id = (envelope.get("request_id")
                     or envelope.get("session_id") or "default")
        with _tracer.start_as_current_span(
            "graph_worker.run_turn",
            attributes={
                "oneops.request_id": envelope.get("request_id", ""),
                "oneops.tenant_id": envelope.get("tenant_id", ""),
                "oneops.user_id": envelope.get("user_id", ""),
                "oneops.entry_mode": envelope.get("entry_mode", ""),
            },
        ) as span:
            try:
                out = await asyncio.wait_for(
                    run_turn(self._graph, envelope,
                             config={"configurable": {"thread_id": thread_id}}),
                    timeout=self._timeout_s,
                )
                trace_id_int = span.get_span_context().trace_id
                trace_id = format(trace_id_int, "032x") if trace_id_int else None
                set_langfuse_io(span, input=envelope.get("message", ""),
                                output=out.get("final_response"))
                reply = {
                    "final_status": out.get("final_status") or "",
                    "final_response": out.get("final_response") or "",
                    "step_results": list(out.get("step_results") or []),
                    "session_id": envelope.get("session_id", ""),
                    "request_id": envelope.get("request_id", ""),
                    "trace_id": trace_id,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                }
            except TimeoutError:
                _log.warning("graph_worker.timeout",
                             request_id=envelope.get("request_id", ""))
                reply = {
                    "final_status": "failed",
                    "final_response": "(graph worker: turn timed out)",
                    "step_results": [],
                    "session_id": envelope.get("session_id", ""),
                    "request_id": envelope.get("request_id", ""),
                    "trace_id": None,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                }
            except Exception as exc:                     # noqa: BLE001 — boundary
                _log.warning("graph_worker.handler_raised",
                             error=str(exc)[:200])
                reply = {
                    "final_status": "failed",
                    "final_response": f"(graph worker: {type(exc).__name__})",
                    "step_results": [],
                    "session_id": envelope.get("session_id", ""),
                    "request_id": envelope.get("request_id", ""),
                    "trace_id": None,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                }
            await _publish_reply(msg, reply)


async def _publish_reply(msg: Any, reply: dict[str, Any]) -> None:
    """Publish the reply on `msg.reply` if the requester opted in to
    request/reply. NATS publishes are non-blocking."""
    if not getattr(msg, "reply", None):
        return                                            # fire-and-forget request
    client = await get_nats_client()
    payload = json.dumps(reply, default=str).encode("utf-8")
    # The NATSClient wrapper doesn't expose `publish` directly today;
    # use the underlying connection.
    await client._nc.publish(msg.reply, payload)         # noqa: SLF001 — adapter seam


def build_graph_worker(graph: Any) -> GraphWorker:
    """Factory — keeps construction in one place for tests + production."""
    return GraphWorker(graph)


__all__ = ["GraphWorker", "build_graph_worker", "REQUEST_SUBJECT", "QUEUE_GROUP"]
