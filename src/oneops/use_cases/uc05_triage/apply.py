"""Section I apply — writes the technician's approved triage values.

Backend-agnostic: takes a TicketStore (JsonFixtureStore or DbStore). The
SQL / file-write happens inside the store, not here. apply.py owns:
  • SLA clock computation (now + sla_duration_by_priority[priority])
  • Outcome construction (applied / discarded)
  • Audit-ready metadata
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from oneops.observability import span
from oneops.use_cases.uc05_triage.contracts import (
    Outcome,
    Proposal,
    TriageDecision,
)
from oneops.use_cases.uc05_triage.stores.base import TicketStore

_DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[4]
    / "registries" / "service-schema.json"
)


async def apply_triage_decision(
    *,
    proposal: Proposal,
    decision: TriageDecision,
    final_values: Mapping[str, Any] | None,
    store: TicketStore,
    schema_path: Path | None = None,
    now: datetime | None = None,
) -> Outcome:
    """Apply or discard the proposal.

    YES path:
      • Merge AI suggestions with technician's final_values (Rule D)
      • Compute sla_due from final priority
      • Call store.apply(...) — UPDATE row in DB or in JSON, same shape
      • Return Outcome(applied)

    NO path:
      • No store call. Return Outcome(discarded).
    """
    when = now or datetime.now(UTC)

    _sp_cm = span("uc05.apply",
                   **{"oneops.tenant_id": proposal.tenant_id,
                      "oneops.user_id": decision.actor_user_id,
                      "uc05.ticket_id": proposal.ticket_id,
                      "uc05.service_id": proposal.service_id,
                      "uc05.choice": decision.choice})
    _sp_cm.__enter__()
    try:
        outcome = await _apply_impl(
            proposal=proposal, decision=decision, final_values=final_values,
            store=store, schema_path=schema_path, when=when,
        )
        return outcome
    finally:
        _sp_cm.__exit__(None, None, None)


async def _apply_impl(*, proposal, decision, final_values, store, schema_path, when):
    if decision.choice == "no":
        return Outcome(
            proposal_id=decision.proposal_id,
            ticket_id=proposal.ticket_id,
            outcome="discarded",
            actor_user_id=decision.actor_user_id,
            decided_at=when,
            applied_fields=None,
        )

    # YES: merge AI suggestions with technician edits
    merged = _merge_final_values(proposal, final_values or {})
    sla_due = _compute_sla_due(merged["priority"], when, schema_path)

    await store.apply(
        service_id=proposal.service_id,
        ticket_id=proposal.ticket_id,
        tenant_id=proposal.tenant_id,
        final_values=merged,
        sla_due=sla_due,
        actor_user_id=decision.actor_user_id,
        now=when,
    )

    return Outcome(
        proposal_id=decision.proposal_id,
        ticket_id=proposal.ticket_id,
        outcome="applied",
        actor_user_id=decision.actor_user_id,
        decided_at=when,
        applied_fields={**merged, "sla_due": sla_due.isoformat()},
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _merge_final_values(
    proposal: Proposal, edits: Mapping[str, Any]
) -> dict[str, Any]:
    """Default to AI's suggestions; override with technician's edits."""
    base: dict[str, Any] = {
        "category":         proposal.suggested_category,
        "subcategory":      proposal.suggested_subcategory,
        "impact":           proposal.suggested_impact,
        "urgency":          proposal.suggested_urgency,
        "priority":         proposal.suggested_priority,
        "assignment_group": proposal.suggested_assignment_group,
        "assigned_to":      proposal.suggested_assigned_to,
        "ci_id":            proposal.suggested_ci_id,
    }
    base.update({k: v for k, v in edits.items() if v is not None})
    return base


def _compute_sla_due(
    priority: str,
    now: datetime,
    schema_path: Path | None = None,
) -> datetime:
    """now + sla_duration_by_priority[priority]. Reads the map from registry."""
    durations = _load_sla_durations(schema_path)
    duration_str = durations.get(priority, "8h")
    return now + _parse_duration(duration_str)


def _load_sla_durations(schema_path: Path | None) -> dict[str, str]:
    path = schema_path or _DEFAULT_SCHEMA_PATH
    data = json.loads(Path(path).read_text())
    block = data.get("sla_duration_by_priority") or {}
    return {k: v for k, v in block.items() if not k.startswith("_")}


def _parse_duration(s: str) -> timedelta:
    """Compact format: '8h' / '4h' / '2h' / '30min' / '90sec'."""
    s = s.strip().lower()
    if s.endswith("min"):
        return timedelta(minutes=int(s[:-3]))
    if s.endswith("sec"):
        return timedelta(seconds=int(s[:-3]))
    if s.endswith("h"):
        return timedelta(hours=int(s[:-1]))
    if s.endswith("d"):
        return timedelta(days=int(s[:-1]))
    raise ValueError(f"unsupported sla duration format: {s!r}")
