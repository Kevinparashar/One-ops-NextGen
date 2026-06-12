"""Capability classifier — deterministic band-keeping + contrastive scoring.

Hermetic: a fake embedder maps a text to a one-hot vector by the KIND token it
contains, so cosines are fully controllable. Proves the three behaviours that
matter: degrade-safely (no embedder), band-keeping (keep top + close, drop far),
and the contrastive negative push (a query near a kind's negative_examples is
pushed OFF that kind).
"""
from __future__ import annotations

import pytest

from oneops.registry.capabilities import (
    CapabilityTaxonomy,
    set_capability_taxonomy,
)
from oneops.router.capability_classifier import CapabilityClassifier

# Two orthogonal kinds. Token "K" ⇒ knowledge axis, "R" ⇒ retrieval axis.
_AXIS = {"K": [1.0, 0.0], "R": [0.0, 1.0]}


class _FakeEmbedder:
    async def embed(self, text: str, *, tenant_id: str) -> list[float]:
        for tok, vec in _AXIS.items():
            if tok in text:
                return list(vec)
        return [0.5, 0.5]  # neutral


class _Skill:
    def __init__(self, use_when, examples, negative_examples):
        self.use_when = tuple(use_when)
        self.examples = tuple(examples)
        self.description = ""
        self.negative_examples = tuple(negative_examples)


class _Agent:
    def __init__(self, agent_id, caps, skill):
        self.id = agent_id
        self.capabilities = tuple(caps)
        self.skills = (skill,)


class _Agents:
    def __init__(self, agents):
        self._a = agents

    def list_active(self):
        return self._a


class _Registry:
    def __init__(self, agents):
        self.agents = _Agents(agents)


@pytest.fixture
def taxonomy():
    tax = CapabilityTaxonomy([
        {"id": "knowledge", "priority": 60, "principle": "K principle"},
        {"id": "record_retrieval", "priority": 40, "principle": "R principle"},
    ])
    set_capability_taxonomy(tax)
    yield tax
    set_capability_taxonomy(None)


def _registry():
    # knowledge agent's positives are on the K axis; retrieval agent's positives
    # on the R axis, and its NEGATIVE example is on the K axis (a problem is NOT
    # retrieval) — the contrastive signal.
    return _Registry([
        _Agent("uc03", ["knowledge"],
               _Skill(use_when=["K use_when"], examples=["K example"],
                      negative_examples=["R negative"])),
        _Agent("uc02", ["record_retrieval"],
               _Skill(use_when=["R use_when"], examples=["R example"],
                      negative_examples=["K negative"])),
    ])


@pytest.mark.asyncio
async def test_no_embedder_returns_none(taxonomy):
    clf = CapabilityClassifier(embedder=None, registry=_registry())
    assert await clf.classify("anything", tenant_id="t") is None


@pytest.mark.asyncio
async def test_clear_query_keeps_only_its_kind(taxonomy):
    clf = CapabilityClassifier(embedder=_FakeEmbedder(), registry=_registry())
    r = await clf.classify("K", tenant_id="t")  # pure knowledge axis
    assert r is not None
    assert r.top_kind == "knowledge"
    assert r.kept_kinds == frozenset({"knowledge"})  # retrieval far ⇒ dropped


@pytest.mark.asyncio
async def test_contrastive_negative_pushes_kind_off(taxonomy, monkeypatch):
    # A query on the K axis must score LOW on record_retrieval because that
    # kind's NEGATIVE centroid is on the K axis (negative subtraction). So the
    # retrieval kind is pushed off even though the query is generic.
    monkeypatch.setenv("ONEOPS_ROUTER_KIND_NEG_WEIGHT", "1.0")
    clf = CapabilityClassifier(embedder=_FakeEmbedder(), registry=_registry())
    r = await clf.classify("K", tenant_id="t")
    assert "record_retrieval" not in r.kept_kinds
    assert "knowledge" in r.kept_kinds
