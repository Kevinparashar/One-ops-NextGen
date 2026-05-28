"""Activation-condition evaluator — stage 3's deterministic core.

`evaluate(condition, signals) -> Ternary`. Walks the registry's recursive
`ActivationCondition` tree (Parlant observation) and returns three-valued
logic over `RequestSignals`. No LLM, no I/O — pure function.

Leaf semantics (`negate` flips PASS↔FAIL; INDETERMINATE is unaffected):

  * `intent_in`          — PASS iff classified intent ∩ values ≠ ∅;
                           INDETERMINATE while intent is unclassified.
  * `entity_present`     — PASS iff the query references any entity.
  * `entity_service_in`  — PASS iff a referenced entity's service ∈ values.
  * `focus_required`     — PASS iff an active focus subject exists.
  * `tenant_capability`  — PASS iff the tenant has a capability flag ∈ values.
  * `role_in`            — PASS iff the caller's role ∈ values.

Group semantics:
  * `all_of` — FAIL if any clause FAILs; else INDETERMINATE if any is; else PASS.
  * `any_of` — PASS if any clause PASSes; else INDETERMINATE if any is; else FAIL.

`survives_filter()` is the stage-3 verdict: a candidate is dropped only on a
definite FAIL — an INDETERMINATE candidate is carried to stage 4.
"""
from __future__ import annotations

from oneops.registry.models import ActivationCondition, ConditionOperator, ConditionSignal
from oneops.router.signals import RequestSignals, Ternary


def _flip(result: Ternary) -> Ternary:
    if result is Ternary.PASS:
        return Ternary.FAIL
    if result is Ternary.FAIL:
        return Ternary.PASS
    return Ternary.INDETERMINATE


def _bool(passed: bool) -> Ternary:
    return Ternary.PASS if passed else Ternary.FAIL


def _eval_leaf(cond: ActivationCondition, signals: RequestSignals) -> Ternary:
    sig = cond.signal
    values = set(cond.values)

    if sig is ConditionSignal.INTENT_IN:
        if signals.intents is None:
            return Ternary.INDETERMINATE          # intent not classified yet
        result = _bool(bool(signals.intents & values))
    elif sig is ConditionSignal.ENTITY_PRESENT:
        result = _bool(signals.has_entity)
    elif sig is ConditionSignal.ENTITY_SERVICE_IN:
        result = _bool(bool(signals.entity_services & values))
    elif sig is ConditionSignal.FOCUS_REQUIRED:
        result = _bool(signals.has_active_focus)
    elif sig is ConditionSignal.TENANT_CAPABILITY:
        result = _bool(bool(signals.tenant_capabilities & values))
    elif sig is ConditionSignal.ROLE_IN:
        result = _bool(signals.role in values)
    else:  # pragma: no cover — ConditionSignal is a closed enum
        raise ValueError(f"unknown condition signal: {sig}")

    return _flip(result) if cond.negate else result


def evaluate(cond: ActivationCondition, signals: RequestSignals) -> Ternary:
    """Three-valued evaluation of an activation condition against request signals."""
    if cond.operator is ConditionOperator.LEAF:
        return _eval_leaf(cond, signals)

    child_results = [evaluate(c, signals) for c in cond.clauses]

    if cond.operator is ConditionOperator.ALL_OF:
        if any(r is Ternary.FAIL for r in child_results):
            return Ternary.FAIL
        if any(r is Ternary.INDETERMINATE for r in child_results):
            return Ternary.INDETERMINATE
        return Ternary.PASS

    # ANY_OF
    if any(r is Ternary.PASS for r in child_results):
        return Ternary.PASS
    if any(r is Ternary.INDETERMINATE for r in child_results):
        return Ternary.INDETERMINATE
    return Ternary.FAIL


def survives_filter(cond: ActivationCondition, signals: RequestSignals) -> bool:
    """Stage-3 verdict: keep the candidate unless its condition definitely
    FAILs. PASS and INDETERMINATE both survive — the LLM decides borderline
    cases at stage 4 rather than the filter silently dropping them."""
    return evaluate(cond, signals) is not Ternary.FAIL


__all__ = ["evaluate", "survives_filter"]
