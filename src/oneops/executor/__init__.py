"""Executor layer (P6) — the LangGraph orchestration runtime.

The compiled `StateGraph` runs the router's plan DAG: `Send` fan-out for
parallel steps, the wave loop for dependent steps, lifecycle hooks and an
`interrupt()` approval gate per step, a checkpointer for crash/interrupt
resume, and the boundary responder for non-routed turns.

Public surface:
    from oneops.executor import build_executor_graph, run_turn
    from oneops.executor import ExecutorState
    from oneops.executor import EchoStepExecutor, StepExecutor
    from oneops.executor import HookRegistry, default_hook_registry
    from oneops.executor import DeterministicBoundaryResponder
"""
from __future__ import annotations

from oneops.executor.boundary import (
    BoundaryResponder,
    DeterministicBoundaryResponder,
    LlmBoundaryResponder,
)
from oneops.executor.graph import (
    build_executor_graph,
    build_postgres_checkpointer,
    run_turn,
)
from oneops.executor.hooks import (
    HookContext,
    HookError,
    HookPhase,
    HookRegistry,
    default_hook_registry,
)
from oneops.executor.state import ExecutorState, merge_step_results, serialise_plan
from oneops.executor.step_runner import (
    EchoStepExecutor,
    HandlerStepExecutor,
    StepExecutor,
    make_result,
)

__all__ = [
    "build_executor_graph",
    "run_turn",
    "build_postgres_checkpointer",
    "ExecutorState",
    "merge_step_results",
    "serialise_plan",
    "StepExecutor",
    "EchoStepExecutor",
    "HandlerStepExecutor",
    "make_result",
    "HookRegistry",
    "HookContext",
    "HookPhase",
    "HookError",
    "default_hook_registry",
    "BoundaryResponder",
    "DeterministicBoundaryResponder",
    "LlmBoundaryResponder",
]
