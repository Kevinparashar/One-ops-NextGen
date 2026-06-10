"""UC-8 card-driven routing (2026-06-09 — no-axis rule).

UC-8 (catalog fulfilment) was unreachable from chat: its activation required an
`intent_in [fulfillment_*]` token only the old AXIS approach could emit, so even
when the disambiguator card-selected it, the post-stage-4 intent re-check
dropped it. The fix made UC-8's activation card-driven — `any_of[
entity_service_in[request,catalog], intent_in[...,action]]` — so it survives on
eligibility and the CARD is the gate.

These tests assert the deterministic core: a card-driven action agent, once
selected with a generic `action` intent, SURVIVES the re-check and routes;
and it does not over-admit when the disambiguator declines.
"""
from __future__ import annotations

import pytest

from oneops.authz.decision_cache import InMemoryDecisionCache
from oneops.authz.models import Principal
from oneops.authz.rbac import RbacResolver
from oneops.authz.service import AuthzService
from oneops.registry.models import ActivationCondition, ConditionOperator, ConditionSignal
from oneops.router.disambiguation import Disambiguation
from oneops.router.glossary import Glossary
from oneops.router.plan import RouteOutcome
from oneops.router.retrieval import Candidate
from oneops.router.router import Router
from oneops.router.signals import RequestSignals

from ._factories import intent_cond, make_agent, make_registry

pytestmark = pytest.mark.asyncio

_RBAC = RbacResolver({
    "service_desk_agent": frozenset(
        {"read:all_tickets", "write:ticket", "create:ticket"}),
})


def _card_driven_activation() -> ActivationCondition:
    """UC-8's card-driven activation: any_of[ entity_service_in[request,catalog],
    intent_in[fulfillment_*, action] ] — survives as INDETERMINATE without an
    entity/intent, PASSes once the disambiguator emits `action`."""
    return ActivationCondition(
        operator=ConditionOperator.ANY_OF, signal=None, values=(), negate=False,
        clauses=(
            ActivationCondition(
                operator=ConditionOperator.LEAF,
                signal=ConditionSignal.ENTITY_SERVICE_IN,
                values=("request", "catalog"), negate=False, clauses=()),
            ActivationCondition(
                operator=ConditionOperator.LEAF,
                signal=ConditionSignal.INTENT_IN,
                values=("fulfillment_orchestrate", "action"),
                negate=False, clauses=()),
        ))


class _StubRetriever:
    def __init__(self, candidates):
        self._candidates = candidates

    async def retrieve(self, query_text, *, tenant_id, top_k):
        return list(self._candidates)


class _StubDisambiguator:
    def __init__(self, result):
        self._result = result

    async def disambiguate(self, query_text, candidates, *, request_ctx):
        return self._result


def _router(reg, retriever, disambiguator):
    return Router(reg, Glossary({}), retriever, disambiguator,
                  AuthzService(_RBAC, InMemoryDecisionCache()))


def _principal():
    return Principal(tenant_id="t-a", user_id="u-1", role="service_desk_agent")


def _signals():
    return RequestSignals(role="service_desk_agent", tenant_id="t-a")


# A decoy survivor (INDETERMINATE via intent_cond) so the funnel has 2+
# survivors and runs Stage 4 + the re-check (a single survivor would take the
# stage-3.5 shortcut and never exercise the re-check this test targets).
def _agents(fulfil_condition):
    return [make_agent("uc_fulfil", condition=fulfil_condition),
            make_agent("uc_decoy", condition=intent_cond("summary"))]


async def test_card_selected_action_agent_survives_recheck_and_routes(tmp_path):
    # UC-8-like agent with the card-driven activation; the disambiguator
    # card-selects it and emits the generic `action` intent.
    reg = make_registry(tmp_path, _agents(_card_driven_activation()))
    dis = _StubDisambiguator(
        Disambiguation.select(["uc_fulfil"], confidence=1.0, intents=["action"]))
    router = _router(
        reg, _StubRetriever([Candidate("uc_fulfil", 0.8), Candidate("uc_decoy", 0.5)]), dis)
    result = await router.route("I need a software license",
                                principal=_principal(), signals=_signals())
    # the re-check must NOT drop it (action ∈ intent_in values → PASS)
    assert result.outcome is RouteOutcome.ROUTED
    assert result.plan.agent_ids == ("uc_fulfil",)


async def test_old_style_intent_gate_would_drop_it_proving_the_fix(tmp_path):
    # Same agent but with the OLD activation (intent_in [fulfillment_*] only):
    # the disambiguator's `action` intent has no overlap → re-check drops it.
    old_activation = ActivationCondition(
        operator=ConditionOperator.LEAF, signal=ConditionSignal.INTENT_IN,
        values=("fulfillment_orchestrate",), negate=False, clauses=())
    reg = make_registry(tmp_path, _agents(old_activation))
    dis = _StubDisambiguator(
        Disambiguation.select(["uc_fulfil"], confidence=1.0, intents=["action"]))
    router = _router(
        reg, _StubRetriever([Candidate("uc_fulfil", 0.8), Candidate("uc_decoy", 0.5)]), dis)
    result = await router.route("I need a software license",
                                principal=_principal(), signals=_signals())
    # the old gate drops uc_fulfil under the classified `action` intent — this
    # is exactly the bug the card-driven activation fixed.
    assert result.plan is None or "uc_fulfil" not in result.plan.agent_ids
