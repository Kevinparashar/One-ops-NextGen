"""HandlerStepExecutor — F1 of the UC-1 production contract.

Verifies the production step executor: resolves the agent's primary tool,
calls its registered handler, contains every failure mode behind a typed
`step_result` (status ∈ {success, failed}).
"""
from __future__ import annotations

import asyncio

import pytest

from oneops.executor.step_runner import HandlerStepExecutor
from oneops.registry.models import (
    AbacTags,
    ActivationCondition,
    AgentRecord,
    ConditionOperator,
    ConditionSignal,
    DeterminismLevel,
    ExecutionTier,
    FastPathInputField,
    FastPathSpec,
    RecordStatus,
    RoutingShape,
    ToolParameter,
    ToolRecord,
    ToolRef,
)
from oneops.registry.service import RegistryService
from oneops.registry.store import FileBackend
from oneops.toolrunner.resolver import HandlerResolver

# ── fixtures ────────────────────────────────────────────────────────────


def _make_tool(tool_id: str, handler_ref: str, *, timeout_ms: int = 30_000):
    return ToolRecord(
        id=tool_id, version=1, status=RecordStatus.ACTIVE, owner="team-test",
        description="Test tool.", handler_ref=handler_ref,
        execution_type="read",
        parameters=(ToolParameter(
            name="ticket_id", type="str", required=True,
            description="A ticket id."),),
        timeout_ms=timeout_ms,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF,
            signal=ConditionSignal.INTENT_IN, values=("summary",)),
    )


def _make_agent(*, tool_id: str = "test_summarize"):
    return AgentRecord(
        id="uc_test_summarize", version=1, status=RecordStatus.ACTIVE,
        owner="team-test",
        description="Test summarisation use case.",
        intent_family="summary",
        routing_shape=RoutingShape.SINGLE,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF,
            signal=ConditionSignal.INTENT_IN, values=("summary",)),
        tool_refs=(ToolRef(tool_id=tool_id),),
        abac_tags=AbacTags(tier=ExecutionTier.READ),
        determinism_level=DeterminismLevel.LOW,
        fast_path=FastPathSpec(
            enabled=True, primary_tool_id=tool_id,
            input_fields=(FastPathInputField(
                name="ticket_id", type="str", required=True,
                description="…"),),
        ),
    )


@pytest.fixture
def registry(tmp_path):
    svc = RegistryService(FileBackend(tmp_path))
    return svc


def _seed(svc: RegistryService, agent: AgentRecord, tool: ToolRecord) -> None:
    svc.tools.create(tool)
    svc.tools.activate(tool.id, 1)
    svc.agents.create(agent)
    svc.agents.activate(agent.id, 1)


def _envelope(**over):
    base = {
        "request_id": "req_x", "tenant_id": "T001", "session_id": "s1",
        "user_id": "oneops", "role": "service_desk_agent",
        "message": "summarize INC0001001",
    }
    base.update(over)
    return base


def _step(agent_id="uc_test_summarize", **params):
    return {"step_id": "step_1", "agent_id": agent_id,
            "parameters": params, "depends_on": []}


# ── happy path — handler invoked with right args + context ──────────────


async def test_handler_is_called_with_step_args_and_envelope_context(registry):
    calls = []

    async def fake_handler(arguments, context):
        calls.append((dict(arguments), dict(context)))
        return {"outcome": "summarized", "summary": "ok",
                "ticket_id": arguments.get("ticket_id")}

    resolver = HandlerResolver()
    resolver.register("test:fake_handler", fake_handler)
    tool = _make_tool("test_summarize", "test:fake_handler")
    agent = _make_agent(tool_id="test_summarize")
    _seed(registry, agent, tool)

    runner = HandlerStepExecutor(registry=registry, resolver=resolver)
    result = await runner.run(
        _step(ticket_id="INC0001001"), _envelope())
    assert result["status"] == "success"
    assert result["output"]["summary"] == "ok"
    assert result["output"]["ticket_id"] == "INC0001001"

    args, ctx = calls[0]
    assert args == {"ticket_id": "INC0001001"}
    assert ctx["tenant_id"] == "T001"
    assert ctx["user_id"] == "oneops"
    assert ctx["role"] == "service_desk_agent"
    assert ctx["request_id"] == "req_x"


# ── primary tool is taken from fast_path.primary_tool_id ────────────────


async def test_primary_tool_id_drives_handler_selection(registry):
    calls = []

    async def alt_handler(arguments, context):
        calls.append("alt")
        return {"outcome": "from_alt"}

    async def primary_handler(arguments, context):
        calls.append("primary")
        return {"outcome": "from_primary"}

    resolver = HandlerResolver()
    resolver.register("test:alt", alt_handler)
    resolver.register("test:primary", primary_handler)
    primary_tool = _make_tool("primary_tool", "test:primary")
    alt_tool = _make_tool("alt_tool", "test:alt")
    agent = _make_agent(tool_id="primary_tool")
    # Add a SECOND tool_ref the agent could pick — the executor must still
    # call the declared primary, not the first.
    agent_with_two = agent.model_copy(update={
        "tool_refs": (ToolRef(tool_id="alt_tool"),
                       ToolRef(tool_id="primary_tool")),
    })
    registry.tools.create(primary_tool); registry.tools.activate(primary_tool.id, 1)
    registry.tools.create(alt_tool);     registry.tools.activate(alt_tool.id, 1)
    registry.agents.create(agent_with_two); registry.agents.activate(agent_with_two.id, 1)

    runner = HandlerStepExecutor(registry=registry, resolver=resolver)
    result = await runner.run(_step(), _envelope())
    assert result["status"] == "success"
    assert result["output"] == {"outcome": "from_primary"}
    assert calls == ["primary"]


# ── failure containment — handler exceptions never propagate ────────────


async def test_handler_exception_becomes_status_failed(registry):
    async def boom_handler(arguments, context):
        raise RuntimeError("kaboom")

    resolver = HandlerResolver()
    resolver.register("test:boom", boom_handler)
    tool = _make_tool("test_summarize", "test:boom")
    agent = _make_agent(tool_id="test_summarize")
    _seed(registry, agent, tool)

    runner = HandlerStepExecutor(registry=registry, resolver=resolver)
    result = await runner.run(_step(), _envelope())
    assert result["status"] == "failed"
    assert "RuntimeError" in result["error"]
    assert "kaboom" in result["error"]


async def test_handler_timeout_is_typed_failure(registry):
    async def slow_handler(arguments, context):
        await asyncio.sleep(2.0)
        return {"ok": True}

    resolver = HandlerResolver()
    resolver.register("test:slow", slow_handler)
    # Tool timeout = 100ms; handler sleeps for 2s ⇒ asyncio.TimeoutError.
    tool = _make_tool("test_summarize", "test:slow", timeout_ms=100)
    agent = _make_agent(tool_id="test_summarize")
    _seed(registry, agent, tool)

    runner = HandlerStepExecutor(registry=registry, resolver=resolver)
    result = await runner.run(_step(), _envelope())
    assert result["status"] == "failed"
    assert "timed out" in result["error"].lower()


# ── registry misconfiguration — typed failures, never silent ────────────


async def test_step_without_agent_id_fails_loud(registry):
    runner = HandlerStepExecutor(registry=registry)
    result = await runner.run({"step_id": "x", "parameters": {}}, _envelope())
    assert result["status"] == "failed"
    assert "no agent_id" in result["error"]


async def test_unknown_agent_id_fails_loud(registry):
    runner = HandlerStepExecutor(registry=registry)
    result = await runner.run(
        _step(agent_id="does_not_exist"), _envelope())
    assert result["status"] == "failed"
    assert "unknown agent" in result["error"]


async def test_agent_without_fast_path_fails_loud(registry):
    # An agent record with no fast_path block — executor cannot decide which
    # tool to call yet (ReAct loop is a separate build step).
    tool = _make_tool("solo", "test:solo")
    agent = AgentRecord(
        id="uc_no_fastpath", version=1, status=RecordStatus.ACTIVE,
        owner="team-test",
        description="No fast-path declared.",
        intent_family="summary", routing_shape=RoutingShape.SINGLE,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF,
            signal=ConditionSignal.INTENT_IN, values=("summary",)),
        tool_refs=(ToolRef(tool_id="solo"),),
        abac_tags=AbacTags(tier=ExecutionTier.READ),
        determinism_level=DeterminismLevel.LOW,
    )
    registry.tools.create(tool); registry.tools.activate("solo", 1)
    registry.agents.create(agent); registry.agents.activate(agent.id, 1)
    runner = HandlerStepExecutor(registry=registry)
    result = await runner.run(_step(agent_id="uc_no_fastpath"), _envelope())
    assert result["status"] == "failed"
    # The executor surfaces a loud failure when no tool can be resolved —
    # message text may evolve; what matters is the failure carries enough
    # signal to debug ("handler" or "primary_tool_id").
    assert "handler" in result["error"] or "primary_tool_id" in result["error"]


async def test_unresolvable_handler_ref_fails_loud(registry):
    # Tool declares a handler_ref that points at a missing module — the
    # resolver raises ToolHandlerError; the executor must contain it.
    resolver = HandlerResolver()
    tool = _make_tool("test_summarize",
                      "oneops.does_not_exist:missing_handler")
    agent = _make_agent(tool_id="test_summarize")
    _seed(registry, agent, tool)
    runner = HandlerStepExecutor(registry=registry, resolver=resolver)
    result = await runner.run(_step(), _envelope())
    assert result["status"] == "failed"
    assert "unresolvable" in result["error"]
