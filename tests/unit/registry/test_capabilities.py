"""Capability taxonomy — the bounded kind set for capability-class routing.

Proves the taxonomy loads, the deterministic cross-kind tie-break (priority),
and that the registry integrity check rejects an unknown capability (a typo
must be fatal at load, not a silent routing drop).
"""
from __future__ import annotations

import pytest

from oneops.errors import ConfigError, RegistryIntegrityError
from oneops.registry.capabilities import (
    CapabilityTaxonomy,
    get_capability_taxonomy,
)
from oneops.registry.loader import load_registry


def test_taxonomy_loads_from_registry_and_exposes_the_five_kinds():
    tax = get_capability_taxonomy()
    assert tax.ids >= {
        "knowledge", "record_retrieval", "record_summary",
        "fulfilment", "record_action",
    }
    # Every entry has a non-empty principle (semantic, not a keyword list).
    for e in tax.entries():
        assert e["principle"].strip()


def test_priority_breaks_a_cross_kind_tie_knowledge_over_retrieval():
    # The policy that fixes "database payroll issue": when the classifier is
    # torn between knowledge and record_retrieval, knowledge (higher priority)
    # wins — deterministically, every time.
    tax = get_capability_taxonomy()
    assert tax.priority("knowledge") > tax.priority("record_retrieval")
    assert tax.best_of({"record_retrieval", "knowledge"}) == "knowledge"
    assert tax.best_of(set()) is None


def test_malformed_taxonomy_is_fatal():
    with pytest.raises(ConfigError):
        CapabilityTaxonomy([])
    with pytest.raises(ConfigError):
        CapabilityTaxonomy([{"id": "x", "priority": 1}])  # missing principle
    with pytest.raises(ConfigError):
        CapabilityTaxonomy([{"id": "a", "priority": 1, "principle": "p"},
                            {"id": "a", "priority": 2, "principle": "q"}])  # dup


def test_every_active_agent_declares_a_known_capability():
    svc = load_registry("registries/v2")
    tax = get_capability_taxonomy()
    for a in svc.agents.list_active():
        assert a.capabilities, f"{a.id} declares no capability"
        for cap in a.capabilities:
            assert cap in tax.ids, f"{a.id} has unknown capability {cap}"


def test_integrity_rejects_unknown_capability(monkeypatch):
    # An agent whose capability is not in the taxonomy must fail the load-time
    # integrity check (closed vocabulary; a typo can't silently route).
    svc = load_registry("registries/v2", check_integrity=False)
    real = svc.agents.list_active

    class _Fake:
        def __init__(self, a, caps):
            self._a = a
            self.capabilities = caps
        def __getattr__(self, n):
            return getattr(self._a, n)

    agents = list(real())
    patched = [_Fake(a, ("not_a_real_kind",) if a.id == agents[0].id
                       else a.capabilities) for a in agents]
    monkeypatch.setattr(svc.agents, "list_active", lambda: patched)
    with pytest.raises(RegistryIntegrityError, match="unknown capability"):
        svc.check_integrity()
