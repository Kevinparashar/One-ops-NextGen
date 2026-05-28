"""Policy engine wired into the executor — a compliance touchpoint returns the
canned response; a deny rule blocks the step."""
from __future__ import annotations

from oneops.executor.graph import build_executor_graph, run_turn
from oneops.policy_engine import PolicyEngine
from oneops.registry.models import (
    AbacTags,
    ActivationCondition,
    AgentRecord,
    ConditionOperator,
    ConditionSignal,
    DataClassification,
    DeterminismLevel,
    ExecutionTier,
    RoutingShape,
)
from oneops.registry.service import RegistryService
from oneops.registry.store import FileBackend
from oneops.router.plan import PlanStep, RoutePlan, RouteResult


def _agent(agent_id, *, data_class=DataClassification.INTERNAL):
    return AgentRecord(
        id=agent_id, version=1, owner="team-test", description="A test agent.",
        intent_family="testing", routing_shape=RoutingShape.SINGLE,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF, signal=ConditionSignal.INTENT_IN,
            values=("summary",)),
        abac_tags=AbacTags(tier=ExecutionTier.READ, data_classification=data_class),
        determinism_level=DeterminismLevel.LOW)


def _registry(tmp_path, agent):
    svc = RegistryService(FileBackend(tmp_path))
    svc.agents.create(agent)
    svc.agents.activate(agent.id, 1)
    return svc


class _StubRouter:
    def __init__(self, agent_id):
        self._agent_id = agent_id

    async def route(self, query_text, *, principal, signals,
                    conversation_history=None, request_ctx=None):
        plan = RoutePlan(steps=(PlanStep(step_id="step_1", agent_id=self._agent_id),))
        return RouteResult.routed(plan, ["d"])


def _envelope():
    return {"request_id": "r-1", "tenant_id": "t-a", "session_id": "s-1",
            "user_id": "u-1", "role": "employee", "message": "do the thing"}


async def test_pii_touchpoint_returns_the_canned_response(tmp_path):
    # An agent classified PII → the policy engine's canned rule fires →
    # the user gets the pre-approved response, not an LLM draft.
    reg = _registry(tmp_path, _agent("uc_pii", data_class=DataClassification.PII))
    graph = build_executor_graph(
        _StubRouter("uc_pii"), reg, policy_engine=PolicyEngine.from_file())
    out = await run_turn(graph, _envelope())
    assert "compliance" in out["final_response"].lower()
    # The step did not run a handler — it returned the canned response.
    assert out["step_results"][0]["output"]["policy_rule"] == "canned_pii_touchpoint"


async def test_non_pii_agent_runs_normally(tmp_path):
    # An INTERNAL-classified agent is not a compliance touchpoint — it runs.
    reg = _registry(tmp_path, _agent("uc_normal", data_class=DataClassification.INTERNAL))
    graph = build_executor_graph(
        _StubRouter("uc_normal"), reg, policy_engine=PolicyEngine.from_file())
    out = await run_turn(graph, _envelope())
    assert out["final_status"] == "executed"
    assert out["step_results"][0]["status"] == "success"
    assert "canned_response" not in (out["step_results"][0]["output"] or {})


async def test_without_a_policy_engine_no_gate_runs(tmp_path):
    # No policy engine wired → even a PII agent just runs (gate is opt-in).
    reg = _registry(tmp_path, _agent("uc_pii", data_class=DataClassification.PII))
    graph = build_executor_graph(_StubRouter("uc_pii"), reg)   # no policy_engine
    out = await run_turn(graph, _envelope())
    assert out["final_status"] == "executed"
