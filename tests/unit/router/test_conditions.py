"""Activation-condition evaluator tests — three-valued logic, every signal.

Adversarial focus: the INDETERMINATE cases (intent unknown pre-stage-4), the
negate flag, and group short-circuit edges — not just the happy PASS path.
"""
from __future__ import annotations

from oneops.registry.models import ActivationCondition, ConditionOperator, ConditionSignal
from oneops.router.conditions import evaluate, survives_filter
from oneops.router.signals import RequestSignals, Ternary


def _leaf(signal, *values, negate=False):
    return ActivationCondition(operator=ConditionOperator.LEAF, signal=signal,
                               values=tuple(values), negate=negate)


def _signals(**over):
    base = dict(role="employee", tenant_id="t-a", present_entities=(),
                tenant_capabilities=frozenset(), has_active_focus=False, intents=None)
    base.update(over)
    return RequestSignals(**base)


# ── intent_in — the INDETERMINATE case ───────────────────────────────────


def test_intent_in_is_indeterminate_when_intent_unclassified():
    cond = _leaf(ConditionSignal.INTENT_IN, "summary")
    assert evaluate(cond, _signals(intents=None)) is Ternary.INDETERMINATE


def test_intent_in_passes_on_match_once_classified():
    cond = _leaf(ConditionSignal.INTENT_IN, "summary", "field_read")
    assert evaluate(cond, _signals(intents=frozenset({"summary"}))) is Ternary.PASS


def test_intent_in_fails_on_no_match_once_classified():
    cond = _leaf(ConditionSignal.INTENT_IN, "summary")
    assert evaluate(cond, _signals(intents=frozenset({"kb_search"}))) is Ternary.FAIL


def test_indeterminate_candidate_survives_the_stage3_filter():
    """The key property: an intent_in condition must not eliminate a candidate
    before intent is known."""
    cond = _leaf(ConditionSignal.INTENT_IN, "summary")
    assert survives_filter(cond, _signals(intents=None)) is True


# ── deterministic signals ────────────────────────────────────────────────


def test_entity_present():
    cond = _leaf(ConditionSignal.ENTITY_PRESENT)
    assert evaluate(cond, _signals(present_entities=())) is Ternary.FAIL
    assert evaluate(cond, _signals(
        present_entities=(("INC0048213", "incident"),))) is Ternary.PASS


def test_entity_service_in():
    cond = _leaf(ConditionSignal.ENTITY_SERVICE_IN, "incident", "problem")
    assert evaluate(cond, _signals(
        present_entities=(("INC1", "incident"),))) is Ternary.PASS
    assert evaluate(cond, _signals(
        present_entities=(("CHG1", "change"),))) is Ternary.FAIL


def test_role_in():
    cond = _leaf(ConditionSignal.ROLE_IN, "manager", "it_director")
    assert evaluate(cond, _signals(role="manager")) is Ternary.PASS
    assert evaluate(cond, _signals(role="employee")) is Ternary.FAIL


def test_focus_required():
    cond = _leaf(ConditionSignal.FOCUS_REQUIRED)
    assert evaluate(cond, _signals(has_active_focus=True)) is Ternary.PASS
    assert evaluate(cond, _signals(has_active_focus=False)) is Ternary.FAIL


def test_tenant_capability():
    cond = _leaf(ConditionSignal.TENANT_CAPABILITY, "beta_kb")
    assert evaluate(cond, _signals(
        tenant_capabilities=frozenset({"beta_kb"}))) is Ternary.PASS
    assert evaluate(cond, _signals(
        tenant_capabilities=frozenset())) is Ternary.FAIL


# ── negate ───────────────────────────────────────────────────────────────


def test_negate_flips_pass_and_fail():
    pos = _leaf(ConditionSignal.ROLE_IN, "viewer")
    neg = _leaf(ConditionSignal.ROLE_IN, "viewer", negate=True)
    s = _signals(role="viewer")
    assert evaluate(pos, s) is Ternary.PASS
    assert evaluate(neg, s) is Ternary.FAIL


def test_negate_leaves_indeterminate_unchanged():
    neg = _leaf(ConditionSignal.INTENT_IN, "summary", negate=True)
    assert evaluate(neg, _signals(intents=None)) is Ternary.INDETERMINATE


# ── groups ───────────────────────────────────────────────────────────────


def _group(op, *clauses):
    return ActivationCondition(operator=op, clauses=tuple(clauses))


def test_all_of_fails_if_any_clause_fails():
    cond = _group(ConditionOperator.ALL_OF,
                  _leaf(ConditionSignal.ENTITY_PRESENT),
                  _leaf(ConditionSignal.ROLE_IN, "manager"))
    # entity present, but role employee != manager → one FAIL → ALL_OF FAIL.
    assert evaluate(cond, _signals(
        present_entities=(("INC1", "incident"),), role="employee")) is Ternary.FAIL


def test_all_of_is_indeterminate_when_a_clause_is_and_none_fail():
    cond = _group(ConditionOperator.ALL_OF,
                  _leaf(ConditionSignal.ENTITY_PRESENT),
                  _leaf(ConditionSignal.INTENT_IN, "summary"))
    assert evaluate(cond, _signals(
        present_entities=(("INC1", "incident"),), intents=None)) is Ternary.INDETERMINATE


def test_any_of_passes_if_any_clause_passes():
    cond = _group(ConditionOperator.ANY_OF,
                  _leaf(ConditionSignal.ROLE_IN, "manager"),
                  _leaf(ConditionSignal.ENTITY_PRESENT))
    assert evaluate(cond, _signals(
        role="employee", present_entities=(("INC1", "incident"),))) is Ternary.PASS


def test_any_of_fails_only_when_every_clause_fails():
    cond = _group(ConditionOperator.ANY_OF,
                  _leaf(ConditionSignal.ROLE_IN, "manager"),
                  _leaf(ConditionSignal.FOCUS_REQUIRED))
    assert evaluate(cond, _signals(role="employee", has_active_focus=False)) is Ternary.FAIL


def test_nested_group_indeterminate_propagates():
    inner = _group(ConditionOperator.ANY_OF,
                   _leaf(ConditionSignal.INTENT_IN, "summary"),
                   _leaf(ConditionSignal.ROLE_IN, "manager"))
    outer = _group(ConditionOperator.ALL_OF, inner,
                   _leaf(ConditionSignal.ENTITY_PRESENT))
    # inner: role fails, intent indeterminate → inner INDETERMINATE;
    # entity present → PASS; ALL_OF → INDETERMINATE.
    assert evaluate(outer, _signals(
        role="employee", present_entities=(("INC1", "incident"),),
        intents=None)) is Ternary.INDETERMINATE
