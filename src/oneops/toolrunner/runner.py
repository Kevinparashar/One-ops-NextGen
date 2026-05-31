"""ToolRunner — safe execution of one tool invocation (P7).

Every tool call goes through `ToolRunner.run`. It enforces, in order:

  1. **Idempotency** — a repeated idempotency key returns the stored result;
     the handler does not run again (re-delivery safety, ADR-0005).
  2. **Handler resolution** — `handler_ref` → callable (resolver.py).
  3. **Timeout** — `asyncio.wait_for` at the tool record's `timeout_ms`; an
     overrun is cancelled and surfaces as a typed `TIMEOUT` result.
  4. **Fault containment** — any handler exception becomes a typed `FAILED`
     result. A handler fault never propagates out of the runner.
  5. **Output capping** — a large return value is moved to the variable store
     and replaced by a `VariableRef` preview (attention budget).

"Sandboxed" at P7 = timeout + total fault containment + the handler receiving
only its declared arguments. OS-level process isolation is a deployment
concern (FaaS), out of this module's scope.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from oneops.observability import get_logger, get_tracer
from oneops.registry.models import ToolRecord
from oneops.toolrunner.context import ToolContext
from oneops.toolrunner.idempotency import IdempotencyStore
from oneops.toolrunner.models import ToolResult
from oneops.toolrunner.resolver import HandlerResolver
from oneops.toolrunner.variables import InMemoryVariableStore

_log = get_logger("oneops.toolrunner")
_tracer = get_tracer("oneops.toolrunner")


class ToolRunner:
    """Executes tools safely. One instance is shared; the variable store may
    be per-request (large outputs are request-scoped)."""

    def __init__(
        self,
        resolver: HandlerResolver,
        variable_store: InMemoryVariableStore | None = None,
        idempotency_store: IdempotencyStore | None = None,
    ) -> None:
        self._resolver = resolver
        self._variables = variable_store or InMemoryVariableStore()
        self._idempotency = idempotency_store

    async def run(
        self,
        tool: ToolRecord,
        arguments: dict[str, Any],
        *,
        context: ToolContext,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        """Run one tool. Never raises for a handler fault — returns a typed
        `ToolResult`.

        `context` is a frozen `ToolContext` — production callers build it
        via `ToolContext.from_request(envelope)`. Tests that exercise the
        runner in isolation should also construct a `ToolContext` (a
        minimal one needs only `tenant_id` + `request_id`)."""
        if not isinstance(context, ToolContext):                    # pragma: no cover - guard
            raise TypeError(
                f"ToolRunner.run: context must be ToolContext, got {type(context).__name__}"
            )
        with _tracer.start_as_current_span(
            "toolrunner.run",
            attributes={"tool.id": tool.id,
                        "tool.execution_type": tool.execution_type.value,
                        "tenant.id": context.tenant.tenant_id,
                        "tenant.tier": context.tenant.tier.value},
        ) as span:
            if context.defaulted_fields:
                # Observability: surface partial-enrichment so an operator
                # sees when upstream is not yet feeding full context.
                span.set_attribute(
                    "toolrunner.defaulted_fields",
                    ",".join(context.defaulted_fields),
                )
            # 1. Idempotency — a repeated key short-circuits the handler.
            if idempotency_key and self._idempotency is not None:
                cached = await self._idempotency.get(idempotency_key)
                if cached is not None:
                    span.set_attribute("tool.idempotency_hit", True)
                    _log.info("toolrunner.idempotency_replay",
                              tool_id=tool.id, idempotency_key=idempotency_key)
                    return cached

            # 2. Resolve the handler (raises ToolHandlerError if unresolvable —
            #    a registry/config fault, surfaced loud, not a handler fault).
            handler = self._resolver.resolve(tool.handler_ref)

            # 3. + 4. Timeout-enforced, fault-contained call.
            timeout_s = tool.timeout_ms / 1000.0
            t0 = time.monotonic()
            try:
                raw = await asyncio.wait_for(
                    handler(arguments, context), timeout=timeout_s)
            except TimeoutError:
                latency = int((time.monotonic() - t0) * 1000)
                span.set_attribute("tool.status", "timeout")
                _log.warning("toolrunner.timeout",
                             tool_id=tool.id, timeout_ms=tool.timeout_ms)
                return ToolResult.timed_out(tool.id, tool.timeout_ms, latency_ms=latency)
            except Exception as exc:  # noqa: BLE001 — contained into a typed result
                latency = int((time.monotonic() - t0) * 1000)
                span.set_attribute("tool.status", "failed")
                _log.warning("toolrunner.handler_raised",
                             tool_id=tool.id, error=str(exc))
                return ToolResult.failed(
                    tool.id, f"handler raised {type(exc).__name__}: {exc}",
                    latency_ms=latency)

            latency = int((time.monotonic() - t0) * 1000)

            # 5. Cap a large output — moved to the variable store as a preview.
            output = self._variables.capture(raw, hint=tool.id)
            result = ToolResult.success(tool.id, output, latency_ms=latency)
            span.set_attribute("tool.status", "success")
            span.set_attribute("tool.latency_ms", latency)

            # Store the completed result for idempotent re-delivery.
            if idempotency_key and self._idempotency is not None:
                await self._idempotency.put(idempotency_key, result, ttl_seconds=86_400)
            return result


__all__ = ["ToolRunner"]
