"""Tool-runner layer (P7) — safe execution of registry-defined tools.

Every tool call goes through `ToolRunner`: handler resolution, a hard timeout,
total fault containment, idempotent re-delivery, and large-output capping.
`ToolStepExecutor` plugs the runner into the P6 graph as the real
`StepExecutor`, replacing the echo stub.

Public surface:
    from oneops.toolrunner import ToolRunner, ToolStepExecutor
    from oneops.toolrunner import HandlerResolver, InMemoryVariableStore
    from oneops.toolrunner import InMemoryIdempotencyStore
    from oneops.toolrunner import ToolResult, ToolStatus, VariableRef
"""
from __future__ import annotations

from oneops.toolrunner.context import CacheHint, CacheSource, ToolContext
from oneops.toolrunner.idempotency import (
    DEFAULT_IDEMPOTENCY_TTL_SECONDS,
    DragonflyIdempotencyStore,
    IdempotencyStore,
    InMemoryIdempotencyStore,
)
from oneops.toolrunner.models import ToolResult, ToolStatus, VariableRef
from oneops.toolrunner.resolver import HandlerResolver, ToolHandler
from oneops.toolrunner.runner import ToolRunner
from oneops.toolrunner.step_executor import ToolStepExecutor
from oneops.toolrunner.variables import (
    DEFAULT_PREVIEW_THRESHOLD_BYTES,
    InMemoryVariableStore,
)

__all__ = [
    "ToolRunner",
    "ToolStepExecutor",
    "ToolContext",
    "CacheHint",
    "CacheSource",
    "HandlerResolver",
    "ToolHandler",
    "InMemoryVariableStore",
    "DEFAULT_PREVIEW_THRESHOLD_BYTES",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "DragonflyIdempotencyStore",
    "DEFAULT_IDEMPOTENCY_TTL_SECONDS",
    "ToolResult",
    "ToolStatus",
    "VariableRef",
]
