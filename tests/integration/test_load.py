"""P11 — sustained multi-tenant load over the full in-process pipeline.

No external infrastructure here (no docker / no DB), so this exercises the
real code path — registry → router (glossary → retrieval → condition+ABAC →
disambiguation) → executor (Send fan-out, hooks, policy gate, memory) — under
concurrency. It proves three things at load:

  1. every turn completes (no lost or hung turns under fan-out concurrency);
  2. tenant isolation holds — a session only ever sees its own conversation;
  3. nothing raises — the pipeline is exception-free on the happy path.

The live-infra load test (real NATS / Postgres / LLM at 10x catalog scale) is
the operator's pre-prod gate; this is the code-level load proof.
"""
from __future__ import annotations

import asyncio

import pytest

from oneops.authz.decision_cache import InMemoryDecisionCache
from oneops.authz.rbac import RbacResolver
from oneops.authz.service import AuthzService
from oneops.executor.graph import build_executor_graph, run_turn
from oneops.policy_engine import PolicyEngine
from oneops.registry.models import (
    AbacTags,
    ActivationCondition,
    AgentRecord,
    ConditionOperator,
    ConditionSignal,
    DeterminismLevel,
    ExecutionTier,
    RoutingShape,
)
from oneops.registry.service import RegistryService
from oneops.registry.store import FileBackend
from oneops.router.disambiguation import ThresholdDisambiguator
from oneops.router.glossary import Glossary
from oneops.router.retrieval import LexicalRetriever
from oneops.router.router import Router
from oneops.session import InMemoryEventLog, InMemoryHotWindow, SessionEventStore

pytestmark = pytest.mark.timeout(120)


def _build_stack(tmp_path):
    """The full pipeline, wired with the no-infrastructure backends."""
    reg = RegistryService(FileBackend(tmp_path))
    agent = AgentRecord(
        id="uc_summary", version=1, owner="team-itsm",
        description="summarize an incident ticket record overview",
        intent_family="entity_summary", routing_shape=RoutingShape.SINGLE,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF, signal=ConditionSignal.INTENT_IN,
            values=("summary",)),
        abac_tags=AbacTags(tier=ExecutionTier.READ),
        determinism_level=DeterminismLevel.LOW)
    reg.agents.create(agent)
    reg.agents.activate("uc_summary", 1)

    authz = AuthzService(
        RbacResolver({"employee": frozenset({"read:own_tickets"})}),
        InMemoryDecisionCache())
    router = Router(reg, Glossary({}), LexicalRetriever(reg),
                    ThresholdDisambiguator(), authz)
    store = SessionEventStore(InMemoryEventLog(), InMemoryHotWindow())
    graph = build_executor_graph(router, reg, session_store=store,
                                 policy_engine=PolicyEngine.from_file())
    return graph, store


def _envelope(tenant: str, session: str, turn: int):
    return {
        "request_id": f"{session}-r{turn}", "tenant_id": tenant,
        "session_id": session, "user_id": f"u-{tenant}", "role": "employee",
        "message": "summarize incident overview",
    }


async def test_eighty_concurrent_turns_all_complete(tmp_path):
    graph, _ = _build_stack(tmp_path)
    # 20 tenants × 4 sessions — 80 turns fired concurrently.
    turns = [_envelope(f"tenant-{t}", f"tenant-{t}-s{s}", 0)
             for t in range(20) for s in range(4)]

    results = await asyncio.gather(
        *(run_turn(graph, e) for e in turns), return_exceptions=True)

    # Nothing raised — the pipeline is exception-free under fan-out concurrency.
    raised = [r for r in results if isinstance(r, BaseException)]
    assert not raised, f"{len(raised)} turn(s) raised: {raised[:3]}"
    # Every turn produced a terminal status.
    assert all(r.get("final_status") for r in results)
    assert all(r["final_status"] == "executed" for r in results)


async def test_tenant_isolation_holds_under_concurrency(tmp_path):
    graph, store = _build_stack(tmp_path)
    # Each tenant runs a 2-turn conversation; all tenants concurrently.
    tenants = [f"tenant-{i}" for i in range(15)]

    async def two_turn(tenant: str):
        session = f"{tenant}-s"
        await run_turn(graph, _envelope(tenant, session, 0))
        await run_turn(graph, _envelope(tenant, session, 1))

    await asyncio.gather(*(two_turn(t) for t in tenants))

    # Every tenant's session holds exactly its own conversation — 2 turns =
    # 4 events (user+assistant ×2), none from any other tenant.
    for tenant in tenants:
        events = await store.replay(tenant, f"{tenant}-s")
        assert len(events) == 4, f"{tenant}: expected 4 events, got {len(events)}"
        assert all(e.session_id == f"{tenant}-s" for e in events)


async def test_repeated_load_is_stable(tmp_path):
    """Three waves of 30 concurrent turns — steady, no degradation or leak in
    the in-process state stores."""
    graph, _ = _build_stack(tmp_path)
    for wave in range(3):
        turns = [_envelope(f"w{wave}-t{i}", f"w{wave}-t{i}-s", 0) for i in range(30)]
        results = await asyncio.gather(*(run_turn(graph, e) for e in turns))
        assert all(r["final_status"] == "executed" for r in results)
