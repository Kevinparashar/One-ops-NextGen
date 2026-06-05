"""UC-5 B-refactor Phase 2b-iii — propose on the MAIN executor (integration).

The crown-jewel test for Option B: it runs the WHOLE triage propose flow through
the compiled main executor graph — real registry (registries/v2), real
AuthzService, real HandlerStepExecutor resolving the registry handler_refs, the
authz_recheck before-hook firing on every step, the per-tool action gate, and the
data-flow bindings — and asserts:

  * a valid `Proposal` comes out of the terminal assemble step, AND
  * it is field-for-field identical (modulo the generated proposal_id +
    created_at) to a `Proposal` built directly by the pure `assemble_proposal()`
    from the same three tool outputs — i.e. the executor path is at PARITY with
    the assembly the legacy bespoke graph runs.

Only the three leaf tool IMPLS are faked (no DB / LLM); everything else is the
real production wiring. Hermetic.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import oneops.use_cases.uc05_triage.handlers as h
from oneops.authz.service import AuthzService
from oneops.executor.graph import build_executor_graph
from oneops.executor.step_runner import HandlerStepExecutor
from oneops.registry.loader import load_registry
from oneops.toolrunner.resolver import HandlerResolver
from oneops.use_cases.uc05_triage.assembly import assemble_proposal
from oneops.use_cases.uc05_triage.contracts import (
    AssignmentRecommendation,
    DuplicateCheckResult,
    PrioritizationResult,
    Proposal,
    ScoredNeighbour,
)
from oneops.use_cases.uc05_triage.executor_runner import (
    TriageExecutorError,
    make_executor_propose_runner,
)

_TENANT = "T001"
_USER = "tech1@corp"
_ROLE = "service_desk_agent"
_SERVICE = "incident"
_TICKET = "INC0001001"


# ── fixed tool outputs (the leaf impls are faked to return these) ───────────


def _neighbour():
    return ScoredNeighbour(id="INC0000002", fields={"assignment_group": "Network"},
                           vec_score=0.9, fts_score=1.0, fused_score=0.9)


def _check_result():
    return DuplicateCheckResult(
        candidates=[_neighbour()], top_match=_neighbour(),
        duplicate_verdict="none", duplicate_threshold=0.85,
        suggested_category="Network", suggested_subcategory="VPN")


def _assignment():
    return AssignmentRecommendation(
        assignment_group="Network", confidence=1.0, coverage=1.0, diversity=1,
        basis_ids=["INC0000002"], basis="majority_of_top_k", rationale="1/1")


def _prioritization():
    return PrioritizationResult(
        impact="On Department", urgency="High", priority="High",
        basis={"impact": "llm_inferred", "urgency": "sla", "priority": "matrix"})


class _Store:
    async def get_ticket(self, *, service_id, ticket_id, tenant_id):
        return {f"{service_id}_id": ticket_id, "title": "VPN down",
                "tenant_id": tenant_id}


@pytest.fixture
def wired_graph(monkeypatch):
    """Compile the real main executor graph + wire UC-5 handlers with faked
    leaf impls. Yields the executor-backed propose runner."""
    registry = load_registry("registries/v2", check_integrity=True)
    resolver = HandlerResolver()
    step_executor = HandlerStepExecutor(registry=registry, resolver=resolver)
    authz = AuthzService.create()

    # Wire handler deps (gateway/conn only satisfy _deps_ready(); the faked
    # impls ignore them) + a store that returns a row.
    h.set_uc05_gateway(MagicMock(name="gateway"))

    async def _cp():
        return MagicMock(name="conn")
    h.set_uc05_connection_provider(_cp)
    h.set_uc05_ticket_store(_Store())

    async def _fake_check(**_k):
        return _check_result()

    async def _fake_assign(**_k):
        return _assignment()

    async def _fake_prio(**_k):
        return _prioritization()

    monkeypatch.setattr(h, "check_duplicate_candidates", _fake_check)
    monkeypatch.setattr(h, "recommend_assignment", _fake_assign)
    monkeypatch.setattr(h, "prioritize_entity", _fake_prio)

    class _StubRouter:
        async def route(self, *a, **k):
            raise AssertionError("router must not run on the fast-path")

    graph = build_executor_graph(
        _StubRouter(), registry, step_executor=step_executor,
        authz_service=authz)
    yield make_executor_propose_runner(graph)

    h.set_uc05_gateway(None)
    h.set_uc05_connection_provider(None)
    h.set_uc05_ticket_store(None)


# ── the integration test ────────────────────────────────────────────────────


async def test_propose_runs_on_main_executor_and_matches_direct_assembly(wired_graph):
    proposal = await wired_graph(
        service_id=_SERVICE, ticket_id=_TICKET, tenant_id=_TENANT,
        user_id=_USER, role=_ROLE)
    assert isinstance(proposal, Proposal)

    expected = assemble_proposal(
        ticket_id=_TICKET, service_id=_SERVICE, tenant_id=_TENANT,
        duplicate=_check_result(), assignment=_assignment(),
        prioritization=_prioritization())

    # Field-for-field parity, modulo the generated id + timestamp.
    drop = {"proposal_id", "created_at"}
    got = proposal.model_dump(exclude=drop)
    want = expected.model_dump(exclude=drop)
    assert got == want


async def test_propose_through_executor_carries_bound_assignment(wired_graph):
    """The assignment_group only appears if check's candidates were data-flow
    bound into recommend_assignment by the executor — proves binding ran."""
    proposal = await wired_graph(
        service_id=_SERVICE, ticket_id=_TICKET, tenant_id=_TENANT,
        user_id=_USER, role=_ROLE)
    assert proposal.suggested_assignment_group == "Network"
    assert proposal.suggested_priority == "High"
    assert proposal.suggested_category == "Network"


async def test_propose_surfaces_upstream_not_found(wired_graph, monkeypatch):
    """If the duplicate-check handler reports not_found, the executor runner
    raises a typed error (never a malformed Proposal)."""
    async def _missing_row(self, *, service_id, ticket_id, tenant_id):
        raise KeyError(ticket_id)
    monkeypatch.setattr(_Store, "get_ticket", _missing_row)
    with pytest.raises(TriageExecutorError):
        await wired_graph(service_id=_SERVICE, ticket_id="INC9999999",
                          tenant_id=_TENANT, user_id=_USER, role=_ROLE)
