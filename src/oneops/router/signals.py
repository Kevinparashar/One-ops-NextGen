"""Request signals + three-valued logic for the routing funnel.

`RequestSignals` is the set of **deterministic facts** about one request that
stage 3 (the condition + ABAC filter) evaluates an agent's `activation_condition`
against. It is built from the request envelope and cheap deterministic
extraction — never from an LLM.

`Ternary` is why this works pre-LLM. A condition leaf is one of:

  * `PASS`          — the signal is known and satisfied;
  * `FAIL`          — the signal is known and violated;
  * `INDETERMINATE` — the signal is not yet known this stage.

Stage 3 drops a candidate only on a definite `FAIL`. An `INDETERMINATE`
candidate survives to stage 4, where the LLM disambiguates. This is what lets
intent-based conditions coexist with a deterministic pre-LLM filter: before
stage 4 the classified intent is unknown, so an `intent_in` leaf evaluates
`INDETERMINATE` and never wrongly eliminates a candidate.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Ternary(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class RequestSignals:
    """Deterministic facts about one request, for stage-3 condition evaluation.

    `intents` is `None` until stage 4 has classified intent — that is the
    deliberate trigger for `INDETERMINATE` on `intent_in` leaves pre-LLM.
    """

    role: str
    tenant_id: str
    # (entity_id, service_id) pairs the query references — from deterministic
    # prefix extraction, e.g. ("INC0048213", "incident").
    present_entities: tuple[tuple[str, str], ...] = ()
    # Capability flags enabled for this tenant.
    tenant_capabilities: frozenset[str] = frozenset()
    # Whether a prior-turn focus subject is in scope.
    has_active_focus: bool = False
    # Classified intent tokens — None means "not classified yet" (pre-stage-4).
    intents: frozenset[str] | None = None

    @property
    def entity_services(self) -> frozenset[str]:
        return frozenset(svc for _, svc in self.present_entities if svc)

    @property
    def has_entity(self) -> bool:
        return len(self.present_entities) > 0


def with_intents(signals: RequestSignals, intents: frozenset[str]) -> RequestSignals:
    """Return a copy of `signals` with classified intents attached — used to
    re-evaluate conditions once stage 4 has produced intent."""
    return RequestSignals(
        role=signals.role,
        tenant_id=signals.tenant_id,
        present_entities=signals.present_entities,
        tenant_capabilities=signals.tenant_capabilities,
        has_active_focus=signals.has_active_focus,
        intents=intents,
    )


__all__ = ["Ternary", "RequestSignals", "with_intents"]
