"""Tool-runner value objects (P7).

A tool invocation produces a `ToolResult`. When the output is large it is not
carried inline — it is stored in the variable store and the result carries a
`VariableRef` (a name + a short preview + the byte size). Downstream context
(an LLM prompt) sees the preview, never the full blob — Moveworks
attention-budget discipline.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ToolStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class VariableRef:
    """A pointer to a large value held in the variable store. Replaces the
    value in a tool result / prompt so a big payload never bloats context."""

    name: str
    preview: str
    size_bytes: int
    is_variable_ref: bool = True        # discriminator for downstream consumers


@dataclass(frozen=True)
class ToolResult:
    """The outcome of one tool invocation.

    `output` is the tool's return value, OR a `VariableRef` when the value was
    too large to carry inline. `from_idempotency_cache` is True when the result
    was replayed for a repeated idempotency key (the tool did not run again).
    """

    tool_id: str
    status: ToolStatus
    output: Any = None
    error: str | None = None
    latency_ms: int = 0
    from_idempotency_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.SUCCESS

    @staticmethod
    def success(tool_id: str, output: Any, *, latency_ms: int = 0,
                from_cache: bool = False) -> ToolResult:
        return ToolResult(tool_id, ToolStatus.SUCCESS, output, None,
                          latency_ms, from_cache)

    @staticmethod
    def failed(tool_id: str, error: str, *, latency_ms: int = 0) -> ToolResult:
        return ToolResult(tool_id, ToolStatus.FAILED, None, error, latency_ms)

    @staticmethod
    def timed_out(tool_id: str, timeout_ms: int, *, latency_ms: int = 0) -> ToolResult:
        return ToolResult(tool_id, ToolStatus.TIMEOUT, None,
                          f"tool exceeded its {timeout_ms}ms timeout", latency_ms)


__all__ = ["ToolStatus", "VariableRef", "ToolResult"]
