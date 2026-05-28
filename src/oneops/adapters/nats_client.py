"""NATS client — request/reply + subscription.

Single connection per process. The nats-py async client is designed for shared
use across asyncio tasks. We hold one and expose request/reply + subscribe.

Concurrency:
- One TCP connection multiplexes thousands of inflight requests.
- nats-py handles concurrent request correlation internally.
- No per-call mutable state in our wrapper.

OTEL propagation:
- Outgoing requests inject W3C traceparent into NATS headers.
- Incoming messages extract trace context and continue the parent trace.

Reconnection:
- nats-py has built-in reconnect (default: forever, with exponential backoff).
- Disconnect/reconnect callbacks log at WARN/INFO.
- Pending requests during a disconnect raise NATSUnavailableError; caller decides retry.

Lifecycle:
    client = await get_nats_client()
    reply = await client.request("oneops.uc.uc01.summary", payload_bytes, timeout=30)
    sub = await client.subscribe("oneops.uc.uc01.>", handler=handle_msg, queue="uc01-workers")
    await shutdown_nats_client()
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import nats
from nats.aio.client import Client as NATSConnection
from nats.aio.msg import Msg
from nats.errors import NoRespondersError, TimeoutError as NATSTimeoutError
from opentelemetry import context as otel_context
from opentelemetry import propagate

from oneops.config import get_settings
from oneops.errors import NATSUnavailableError
from oneops.observability import get_logger, get_tracer

_log = get_logger("oneops.nats")
_tracer = get_tracer("oneops.nats")

MsgHandler = Callable[[Msg], Awaitable[None]]


class NATSClient:
    """Thin async wrapper. One instance per process."""

    def __init__(self, conn: NATSConnection) -> None:
        self._nc = conn
        self._subs: list[Any] = []

    @property
    def is_connected(self) -> bool:
        return self._nc.is_connected

    async def request(
        self,
        subject: str,
        payload: bytes,
        *,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """Send a request, wait for the first reply. Injects OTEL trace context.

        Raises:
            NATSUnavailableError: no responders or timeout
        """
        merged_headers: dict[str, str] = dict(headers or {})
        # Inject W3C traceparent so the responder can continue the trace
        propagate.inject(merged_headers)

        with _tracer.start_as_current_span(
            "nats.request",
            attributes={
                "messaging.system": "nats",
                "messaging.destination.name": subject,
                "messaging.message.payload_size_bytes": len(payload),
            },
        ) as span:
            try:
                msg = await self._nc.request(
                    subject, payload, timeout=timeout, headers=merged_headers
                )
                span.set_attribute("messaging.response.payload_size_bytes", len(msg.data))
                return msg.data
            except NoRespondersError as e:
                raise NATSUnavailableError(f"no responders for subject {subject!r}", cause=e) from e
            except NATSTimeoutError as e:
                raise NATSUnavailableError(
                    f"NATS request timeout on {subject!r} after {timeout}s", cause=e
                ) from e

    async def publish(
        self,
        subject: str,
        payload: bytes,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Fire-and-forget publish. Injects OTEL trace context into headers."""
        merged_headers: dict[str, str] = dict(headers or {})
        propagate.inject(merged_headers)
        with _tracer.start_as_current_span(
            "nats.publish",
            attributes={
                "messaging.system": "nats",
                "messaging.destination.name": subject,
                "messaging.message.payload_size_bytes": len(payload),
            },
        ):
            await self._nc.publish(subject, payload, headers=merged_headers)

    async def subscribe(
        self,
        subject: str,
        *,
        handler: MsgHandler,
        queue: str | None = None,
    ) -> Any:
        """Subscribe to a subject. Handler is invoked per message under a continued trace.

        When `queue` is set, multiple replicas share the subject in queue-group fashion
        (load-balanced delivery). This is how UC microservices scale horizontally.
        """
        async def wrapped(msg: Msg) -> None:
            # Continue trace from incoming headers if present
            carrier: dict[str, str] = dict(msg.header or {}) if msg.header else {}
            ctx = propagate.extract(carrier)
            token = otel_context.attach(ctx)
            try:
                with _tracer.start_as_current_span(
                    "nats.process",
                    attributes={
                        "messaging.system": "nats",
                        "messaging.destination.name": msg.subject,
                        "messaging.message.payload_size_bytes": len(msg.data),
                        "messaging.operation": "process",
                    },
                ):
                    await handler(msg)
            except Exception as exc:  # noqa: BLE001 — log and let caller decide on reply
                _log.exception("nats.handler_error", subject=msg.subject, error=str(exc))
                raise
            finally:
                otel_context.detach(token)

        sub = await self._nc.subscribe(subject, queue=queue or "", cb=wrapped)
        self._subs.append(sub)
        _log.info("nats.subscribed", subject=subject, queue=queue or None)
        return sub

    async def drain(self) -> None:
        """Drain subscriptions + flush pending messages, then close."""
        if self._nc.is_connected:
            await self._nc.drain()


# ── Process-wide singleton ──────────────────────────────────────
_client: NATSClient | None = None
_lock = threading.Lock()


async def get_nats_client() -> NATSClient:
    """Get-or-create the shared NATS client. Concurrency-safe."""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is not None:
            return _client
        settings = get_settings()

        async def _on_disconnect() -> None:
            _log.warning("nats.disconnected", url=settings.nats_url)

        async def _on_reconnect() -> None:
            _log.info("nats.reconnected", url=settings.nats_url)

        async def _on_error(exc: Exception) -> None:
            _log.error("nats.async_error", error=str(exc))

        try:
            conn = await nats.connect(
                servers=[settings.nats_url],
                name=settings.service_name,
                connect_timeout=10,
                max_reconnect_attempts=-1,
                reconnect_time_wait=2,
                disconnected_cb=_on_disconnect,
                reconnected_cb=_on_reconnect,
                error_cb=_on_error,
                # No drain timeout — let drain() decide at shutdown
            )
            _client = NATSClient(conn)
            _log.info("nats.connected", url=settings.nats_url)
            return _client
        except Exception as e:
            raise NATSUnavailableError(f"cannot connect to NATS: {e}", cause=e) from e


async def shutdown_nats_client() -> None:
    """Drain + close the shared client. Called from graceful shutdown."""
    global _client
    if _client is not None:
        try:
            await _client.drain()
        except Exception as e:  # noqa: BLE001 — shutdown must not raise
            _log.warning("nats.drain_failed", error=str(e))
        _client = None


__all__ = ["NATSClient", "get_nats_client", "shutdown_nats_client", "MsgHandler"]
