"""Stage 3.6 — confidence-gated disambiguator skip (latency, RCA 2026-06-09).

The skip fires ONLY when the top survivor's retrieval score clearly dominates
the runner-up AND it is a definite PASS. These tests prove the quality guard:
a CLOSE-score field (the axis-A/B class) and an INDETERMINATE top BOTH still
run the LLM disambiguator — the skip never short-circuits an ambiguous choice.
"""
from __future__ import annotations

import pytest

from oneops.authz.decision_cache import InMemoryDecisionCache
from oneops.authz.models import Principal
from oneops.authz.rbac import RbacResolver
from oneops.authz.service import AuthzService
from oneops.router.disambiguation import Disambiguation
from oneops.router.glossary import Glossary
from oneops.router.plan import RouteOutcome
from oneops.router.retrieval import Candidate
from oneops.router.router import Router
from oneops.router.signals import RequestSignals

from ._factories import intent_cond, make_agent, make_registry, role_cond

pytestmark = pytest.mark.asyncio

_RBAC = RbacResolver({
    "service_desk_agent": frozenset(
        {"read:all_tickets", "write:ticket", "create:ticket"}),
})


class _StubRetriever:
    def __init__(self, candidates):
        self._candidates = candidates

    async def retrieve(self, query_text, *, tenant_id, top_k):
        return list(self._candidates)


class _RecordingDisambiguator:
    """Records whether disambiguate() was called, so a test can assert the
    skip bypassed (or did not bypass) the LLM."""

    def __init__(self, selected: str) -> None:
        self.called = False
        self._selected = selected

    async def disambiguate(self, query_text, candidates, *, request_ctx):
        self.called = True
        return Disambiguation.select([self._selected], confidence=0.9)


def _router(reg, retriever, disambiguator):
    return Router(reg, Glossary({}), retriever, disambiguator,
                  AuthzService(_RBAC, InMemoryDecisionCache()))


def _principal():
    return Principal(tenant_id="t-a", user_id="u-1", role="service_desk_agent")


def _signals():
    return RequestSignals(role="service_desk_agent", tenant_id="t-a")


async def _route(tmp_path, agents, candidates, disambiguator):
    reg = make_registry(tmp_path, agents)
    router = _router(reg, _StubRetriever(candidates), disambiguator)
    return await router.route("a query", principal=_principal(),
                              signals=_signals())


# ── skip FIRES: top is PASS + dominant score + large margin ───────────────


async def test_skip_fires_on_dominant_pass_top(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEOPS_ROUTER_CONFIDENCE_SKIP", "1")
    # both PASS (role_cond matches the signal role); A dominates B by 0.40
    agents = [make_agent("uc_a", condition=role_cond("service_desk_agent")),
              make_agent("uc_b", condition=role_cond("service_desk_agent"))]
    dis = _RecordingDisambiguator("uc_a")
    result = await _route(
        tmp_path, agents,
        [Candidate("uc_a", 0.90), Candidate("uc_b", 0.50)], dis)
    assert result.outcome is RouteOutcome.ROUTED
    assert result.plan.agent_ids == ("uc_a",)
    assert dis.called is False                       # LLM was skipped


# ── skip does NOT fire: close scores (the axis-A/B guard) ─────────────────


async def test_skip_does_not_fire_on_close_scores(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEOPS_ROUTER_CONFIDENCE_SKIP", "1")
    agents = [make_agent("uc_a", condition=role_cond("service_desk_agent")),
              make_agent("uc_b", condition=role_cond("service_desk_agent"))]
    dis = _RecordingDisambiguator("uc_a")
    # margin 0.02 < 0.15 → ambiguous → LLM must run
    await _route(tmp_path, agents,
                 [Candidate("uc_a", 0.70), Candidate("uc_b", 0.68)], dis)
    assert dis.called is True                        # LLM still ran


# ── skip does NOT fire: top is INDETERMINATE (needs intent classification) ─


async def test_skip_does_not_fire_when_top_is_indeterminate(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEOPS_ROUTER_CONFIDENCE_SKIP", "1")
    # uc_a survives only as INDETERMINATE (intent_cond, intent not yet known);
    # even with a huge margin it must NOT skip — the LLM resolves the intent.
    agents = [make_agent("uc_a", condition=intent_cond("summary")),
              make_agent("uc_b", condition=role_cond("service_desk_agent"))]
    dis = _RecordingDisambiguator("uc_a")
    await _route(tmp_path, agents,
                 [Candidate("uc_a", 0.95), Candidate("uc_b", 0.40)], dis)
    assert dis.called is True                        # LLM still ran


# ── skip does NOT fire: flag off (default) ────────────────────────────────


async def test_skip_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("ONEOPS_ROUTER_CONFIDENCE_SKIP", raising=False)
    agents = [make_agent("uc_a", condition=role_cond("service_desk_agent")),
              make_agent("uc_b", condition=role_cond("service_desk_agent"))]
    dis = _RecordingDisambiguator("uc_a")
    await _route(tmp_path, agents,
                 [Candidate("uc_a", 0.90), Candidate("uc_b", 0.50)], dis)
    assert dis.called is True                        # disabled → LLM runs


# ── threshold tunable via env ─────────────────────────────────────────────


async def test_margin_threshold_is_env_tunable(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEOPS_ROUTER_CONFIDENCE_SKIP", "1")
    monkeypatch.setenv("ONEOPS_ROUTER_CONFIDENCE_SKIP_MIN_MARGIN", "0.05")
    agents = [make_agent("uc_a", condition=role_cond("service_desk_agent")),
              make_agent("uc_b", condition=role_cond("service_desk_agent"))]
    dis = _RecordingDisambiguator("uc_a")
    # margin 0.10 ≥ tuned 0.05 → now skips (would not at default 0.15)
    result = await _route(
        tmp_path, agents,
        [Candidate("uc_a", 0.72), Candidate("uc_b", 0.62)], dis)
    assert result.plan.agent_ids == ("uc_a",)
    assert dis.called is False
