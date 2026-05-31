"""PolicyEngine tests — evaluation, priority, canned responses, hot-reload."""
from __future__ import annotations

import json

import pytest

from oneops.errors import ConfigError
from oneops.policy_engine import (
    PolicyEffect,
    PolicyEngine,
    PolicyMatch,
    PolicyQuery,
    PolicyRule,
)

# ── loading ──────────────────────────────────────────────────────────────


def test_loads_the_platform_policy_file():
    engine = PolicyEngine.from_file()
    assert engine.rule_count >= 3
    assert engine.version == "1.0.0"


def test_missing_policy_file_raises():
    with pytest.raises(ConfigError, match="not found"):
        PolicyEngine.from_file("/nonexistent/policy_rules.json")


# ── evaluation ───────────────────────────────────────────────────────────


def test_default_allow_when_no_rule_matches():
    engine = PolicyEngine.from_file()
    decision = engine.evaluate(PolicyQuery(tenant_id="t-a", action="summary"))
    assert decision.effect is PolicyEffect.ALLOW
    assert decision.matched_rule_id == ""           # default — no rule


def test_deny_rule_fires():
    engine = PolicyEngine.from_file()
    decision = engine.evaluate(PolicyQuery(
        tenant_id="t-a", action="credential_disclosure"))
    assert decision.effect is PolicyEffect.DENY
    assert decision.matched_rule_id == "deny_credential_disclosure"


def test_canned_rule_returns_a_preapproved_response():
    engine = PolicyEngine.from_file()
    decision = engine.evaluate(PolicyQuery(
        tenant_id="t-a", data_classification="pii"))
    assert decision.effect is PolicyEffect.CANNED
    assert decision.is_canned
    assert "compliance" in decision.canned_response.lower()


def test_decision_carries_version_and_rule_id():
    engine = PolicyEngine.from_file()
    decision = engine.evaluate(PolicyQuery(tenant_id="t-a", surface="legal"))
    assert decision.matched_rule_id == "canned_legal_surface"
    assert decision.policy_version == "1.0.0"


# ── priority ─────────────────────────────────────────────────────────────


def test_highest_priority_matching_rule_wins():
    # Two rules both match role 'agent'; the priority-50 DENY beats the
    # priority-10 ALLOW.
    rules = [
        PolicyRule(id="low_allow", description="", match=PolicyMatch(roles=("agent",)),
                   effect=PolicyEffect.ALLOW, priority=10),
        PolicyRule(id="high_deny", description="", match=PolicyMatch(roles=("agent",)),
                   effect=PolicyEffect.DENY, reason="blocked", priority=50),
    ]
    engine = PolicyEngine(rules, version="test")
    decision = engine.evaluate(PolicyQuery(tenant_id="t", role="agent"))
    assert decision.matched_rule_id == "high_deny"
    assert decision.effect is PolicyEffect.DENY


def test_match_requires_every_specified_field():
    # A rule matching role AND action only fires when BOTH match.
    rule = PolicyRule(id="r", description="",
                      match=PolicyMatch(roles=("agent",), actions=("close",)),
                      effect=PolicyEffect.DENY, reason="x", priority=1)
    engine = PolicyEngine([rule])
    assert engine.evaluate(PolicyQuery(tenant_id="t", role="agent",
                                       action="close")).effect is PolicyEffect.DENY
    # Right role, wrong action → no match → default allow.
    assert engine.evaluate(PolicyQuery(tenant_id="t", role="agent",
                                       action="summary")).effect is PolicyEffect.ALLOW


def test_canned_rule_without_a_response_is_rejected():
    with pytest.raises(ValueError, match="requires a canned_response"):
        PolicyRule(id="bad", description="", match=PolicyMatch(),
                   effect=PolicyEffect.CANNED)        # no canned_response


# ── hot-reload (the no-redeploy guarantee) ───────────────────────────────


def test_hot_reload_picks_up_a_changed_policy(tmp_path):
    policy_file = tmp_path / "policy_rules.json"

    def _write(rules, version):
        policy_file.write_text(json.dumps({"version": version, "rules": rules}),
                               encoding="utf-8")

    # v1 — nothing denies "summary".
    _write([], "1")
    engine = PolicyEngine.from_file(str(policy_file))
    assert engine.evaluate(PolicyQuery(tenant_id="t", action="summary")).effect \
        is PolicyEffect.ALLOW

    # Policy changes on disk — a new DENY rule. No code change, no restart.
    _write([{"id": "new_deny", "description": "", "match": {"actions": ["summary"]},
             "effect": "deny", "reason": "now blocked"}], "2")
    new_version = engine.reload()

    assert new_version == "2"
    decision = engine.evaluate(PolicyQuery(tenant_id="t", action="summary"))
    assert decision.effect is PolicyEffect.DENY      # the change took effect
    assert decision.matched_rule_id == "new_deny"
