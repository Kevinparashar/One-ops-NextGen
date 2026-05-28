"""Registry load test — P1 exit criterion: 10K entries within budget.

The full catalog target is 1000 use cases; this test runs at 10x that to
prove headroom. It generates 10,000 active agent records, then measures the
two operations the platform actually performs at scale:

  * `list_active()` — the router's candidate universe before retrieval;
  * `check_integrity()` — the CI gate and startup gate.

Budget rationale: registry load happens once at process start, off the
request path. A 30s ceiling at 10x scale leaves ample margin; the production
backend (Dragonfly hot / Postgres cold, ADR territory) replaces the file
backend before this is ever a hot-path concern.
"""
from __future__ import annotations

import time

import pytest

from oneops.registry.models import (
    AbacTags,
    ActivationCondition,
    AgentRecord,
    ConditionOperator,
    ConditionSignal,
    DeterminismLevel,
    ExecutionTier,
    RecordStatus,
    RoutingShape,
)
from oneops.registry.service import RegistryService
from oneops.registry.store import FileBackend

pytestmark = [pytest.mark.integration, pytest.mark.slow]

AGENT_COUNT = 10_000
LOAD_BUDGET_SECONDS = 30.0


def _make_agent(index: int) -> AgentRecord:
    """A realistic synthetic agent. Every Nth agent depends on agent N-1 so the
    dependency graph is non-trivial (a long chain) without forming a cycle."""
    depends_on: tuple[str, ...] = ()
    if index > 0 and index % 5 == 0:
        depends_on = (f"uc_load_{index - 1:05d}",)
    return AgentRecord(
        id=f"uc_load_{index:05d}", version=1, status=RecordStatus.ACTIVE,
        owner="team-loadtest",
        description=f"Synthetic load-test agent number {index} for catalog scale.",
        intent_family="loadtest", routing_shape=RoutingShape.SINGLE,
        activation_condition=ActivationCondition(
            operator=ConditionOperator.LEAF, signal=ConditionSignal.INTENT_IN,
            values=(f"intent_{index % 50}",)),
        abac_tags=AbacTags(tier=ExecutionTier.READ),
        determinism_level=DeterminismLevel.LOW, depends_on=depends_on)


def _seed(root, count: int) -> RegistryService:
    """Write `count` active agent envelopes straight through the backend.
    create()/activate() are unit-tested elsewhere; here we want raw scale."""
    backend = FileBackend(root)
    for i in range(count):
        agent = _make_agent(i)
        backend.write("agents", agent.id, {
            "id": agent.id,
            "versions": {"1": agent.model_dump(mode="json")},
            "active_version": 1,
        })
    return RegistryService(backend)


def test_registry_loads_10k_agents_within_budget(tmp_path):
    t0 = time.monotonic()
    service = _seed(tmp_path, AGENT_COUNT)
    seed_seconds = time.monotonic() - t0

    t1 = time.monotonic()
    active = service.agents.list_active()
    list_seconds = time.monotonic() - t1

    assert len(active) == AGENT_COUNT, "every seeded agent must load as active"

    t2 = time.monotonic()
    service.check_integrity()                       # 2000 dependency edges, acyclic
    integrity_seconds = time.monotonic() - t2

    total = seed_seconds + list_seconds + integrity_seconds
    # Surface the numbers so a regression is visible in test output.
    print(f"\n[10k load] seed={seed_seconds:.2f}s list={list_seconds:.2f}s "
          f"integrity={integrity_seconds:.2f}s total={total:.2f}s")
    assert list_seconds + integrity_seconds < LOAD_BUDGET_SECONDS, (
        f"load+integrity took {list_seconds + integrity_seconds:.1f}s, "
        f"budget is {LOAD_BUDGET_SECONDS}s")


def test_integrity_detects_a_cycle_at_scale(tmp_path):
    """Inject a genuine 2-cycle into the 10K catalog and prove the integrity
    check still finds it — the cycle detector must not degrade at scale.

    A real cycle needs a closed path: making agent A depend on agent B AND
    agent B depend on agent A. One back-edge alone is not a cycle unless the
    target already leads home — so we write both edges explicitly."""
    service = _seed(tmp_path, AGENT_COUNT)
    backend = FileBackend(tmp_path)
    for src, dst in [("uc_load_00000", "uc_load_00001"),
                     ("uc_load_00001", "uc_load_00000")]:
        env = backend.read("agents", src)
        env["versions"]["1"]["depends_on"] = [dst]
        backend.write("agents", src, env)

    from oneops.errors import RegistryIntegrityError
    with pytest.raises(RegistryIntegrityError, match="cycle"):
        service.check_integrity()
