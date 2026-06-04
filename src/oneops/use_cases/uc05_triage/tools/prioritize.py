"""Tool 3: prioritize_entity.

Derives impact + urgency + priority for the proposal card.

Two completely separate paths by service_id:

  INCIDENT — LLM-driven (impact is a semantic judgement call):
    Prompt the LLM with FULL triage context (title, description, category,
    subcategory, service_name, ci_name/type/location, vip_flag) and ask it
    to pick impact + urgency from the Motadata vocabulary.

  REQUEST — Deterministic (no LLM cost, fully auditable):
    impact  = derive_impact_for_request[catalog_category]  (+ VIP override)
    urgency = derive_urgency_for_request[sla_state]
    Both maps live in registries/service-schema.json under the request entry.

Then for BOTH paths, priority is a matrix lookup:
    priority = priority_matrix[impact][urgency]

Failure modes (loud, never silent):
  • Empty input (no title AND no description AND no suggested_category) → refuse
  • LLM returns off-vocabulary value → Pydantic rejects → safe default + basis
  • LLM gateway exception → safe default + basis
  • Missing catalog category for a request → default_when_unmatched
  • Missing SLA state for a request → default_when_no_sla

Read-only — no DB writes. The actual UPDATE happens in apply.py on operator Yes.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from oneops.observability import span
from oneops.use_cases.uc05_triage.contracts import (
    Impact,
    PrioritizationResult,
    Priority,
    Urgency,
)

# Same path resolution pattern as schema_loader
_DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[5]
    / "registries" / "service-schema.json"
)

# Safe middle-tier default when both the LLM and any fallback fail. Picked
# deliberately to avoid auto-escalating (would cause SLA pressure) or
# auto-demoting (would mask a real outage). On Users + Medium = Low priority
# by the Motadata matrix — the technician will obviously notice and edit.
_SAFE_DEFAULT_IMPACT: Impact = "On Users"
_SAFE_DEFAULT_URGENCY: Urgency = "Medium"

_VALID_IMPACTS: frozenset[str] = frozenset(
    {"Low", "On Users", "On Department", "On Business"}
)
_VALID_URGENCIES: frozenset[str] = frozenset(
    {"Low", "Medium", "High", "Urgent"}
)

_VALID_PRIORITIES: frozenset[str] = frozenset(
    {"Low", "Medium", "High", "Urgent"}
)


def normalise_priority(
    value: str | None, *, schema_path: Path | None = None
) -> str | None:
    """Convert any priority string to the Motadata canonical vocabulary.

    Reads `priority_aliases` from registries/service-schema.json. Behaviour:
      • None / empty   → None
      • Already canonical (Low/Medium/High/Urgent) → returned unchanged
      • Known alias (P1/P2/P3/P4)                  → mapped to canonical
      • Unknown value                              → returned unchanged
        (UC-5 write side never produces unknowns; this is read-side
        normalisation only — silent identity is safer than a raise here
        because legacy rows may carry historical priority strings we
        don't yet alias for).
    """
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    if s in _VALID_PRIORITIES:
        return s
    aliases = _load_priority_aliases(schema_path)
    return aliases.get(s, s)


def _load_priority_aliases(
    schema_path: Path | None = None,
) -> dict[str, str]:
    """Read the alias map from incident.priority_matrix.priority_aliases."""
    path = schema_path or _DEFAULT_SCHEMA_PATH
    data = json.loads(Path(path).read_text())
    for svc in data.get("services", []):
        if svc.get("service_id") == "incident":
            return svc.get("priority_matrix", {}).get("priority_aliases", {}) or {}
    return {}


# LLM signature for impact+urgency inference. Caller supplies a function that
# routes through the gateway (cost-tracked + policy-composed + OTel-spanned).
#   async fn(
#       service_id: str,
#       ticket_row: dict,
#       suggested_category: str | None,
#       suggested_subcategory: str | None,
#       suggested_service_name: str | None,
#       vip_flag: bool,
#   ) -> dict[str, str]
# Returns {"impact": "...", "urgency": "..."} where both values MUST be in
# the Motadata vocabulary. Anything else → rejected → safe default.
InferFn = Callable[..., Awaitable[dict[str, str]]]


async def prioritize_entity(
    *,
    service_id: str,
    ticket_row: Mapping[str, Any],
    suggested_category: str | None = None,
    suggested_subcategory: str | None = None,
    suggested_service_name: str | None = None,
    suggested_catalog_item_id: str | None = None,
    vip_flag: bool = False,
    sla_state: str | None = None,
    infer_fn: InferFn | None = None,
    schema_path: Path | None = None,
) -> PrioritizationResult:
    """Return impact + urgency + priority for the proposal card.

    INCIDENT path: requires infer_fn (LLM via gateway). If infer_fn is None
    or raises or returns an invalid value, returns the safe middle default
    with basis explaining why — never silent.

    REQUEST path: deterministic. Reads derive_impact_for_request +
    derive_urgency_for_request from service-schema.json. infer_fn is ignored.
    """
    if service_id not in ("incident", "request"):
        raise ValueError(
            f"unsupported service_id {service_id!r}; expected 'incident' or 'request'"
        )

    _sp_cm = span("uc05.tool.prioritize",
                   **{"uc05.service_id": service_id, "uc05.vip": vip_flag})
    _sp = _sp_cm.__enter__()
    try:
        result = await _prioritize_impl(
            service_id=service_id, ticket_row=ticket_row,
            suggested_category=suggested_category,
            suggested_subcategory=suggested_subcategory,
            suggested_service_name=suggested_service_name,
            suggested_catalog_item_id=suggested_catalog_item_id,
            vip_flag=vip_flag, sla_state=sla_state,
            infer_fn=infer_fn, schema_path=schema_path,
        )
        try:
            _sp.set_attribute("uc05.impact", result.impact)
            _sp.set_attribute("uc05.urgency", result.urgency)
            _sp.set_attribute("uc05.priority", result.priority)
        except Exception:
            pass
        return result
    finally:
        _sp_cm.__exit__(None, None, None)


async def _prioritize_impl(
    *, service_id, ticket_row, suggested_category, suggested_subcategory,
    suggested_service_name, suggested_catalog_item_id, vip_flag, sla_state,
    infer_fn, schema_path,
):

    """Inner body — wrapped by the public prioritize_entity for span discipline."""
    # Refuse on truly empty input — nothing to reason from.
    title = (ticket_row.get("title") or "").strip()
    description = (ticket_row.get("description") or "").strip()
    if (
        not title
        and not description
        and not suggested_category
        and not suggested_catalog_item_id
    ):
        raise RuntimeError(
            f"{ticket_row.get('incident_id') or ticket_row.get('request_id') or '?'}: "
            f"no signal to prioritize on (title, description, category, "
            f"catalog all empty)"
        )

    matrix, derive_impact_block, derive_urgency_block = _load_priority_blocks(
        schema_path
    )

    basis: dict[str, str] = {}

    if service_id == "incident":
        impact, urgency, basis = await _incident_path(
            ticket_row=ticket_row,
            suggested_category=suggested_category,
            suggested_subcategory=suggested_subcategory,
            suggested_service_name=suggested_service_name,
            vip_flag=vip_flag,
            infer_fn=infer_fn,
        )
    else:
        impact, urgency, basis = _request_path(
            suggested_category=suggested_category,
            suggested_catalog_item_id=suggested_catalog_item_id,
            vip_flag=vip_flag,
            sla_state=sla_state,
            derive_impact_block=derive_impact_block,
            derive_urgency_block=derive_urgency_block,
        )

    priority = _lookup_priority(matrix, impact, urgency)
    basis["priority"] = f"matrix[{impact}][{urgency}]"

    return PrioritizationResult(
        impact=impact, urgency=urgency, priority=priority, basis=basis,
    )


# ── Incident path (LLM) ──────────────────────────────────────────────────────

async def _incident_path(
    *,
    ticket_row: Mapping[str, Any],
    suggested_category: str | None,
    suggested_subcategory: str | None,
    suggested_service_name: str | None,
    vip_flag: bool,
    infer_fn: InferFn | None,
) -> tuple[Impact, Urgency, dict[str, str]]:
    if infer_fn is None:
        return (
            _SAFE_DEFAULT_IMPACT,
            _SAFE_DEFAULT_URGENCY,
            {
                "impact": "safe_default_no_llm",
                "urgency": "safe_default_no_llm",
            },
        )
    try:
        result = await infer_fn(
            service_id="incident",
            ticket_row=dict(ticket_row),
            suggested_category=suggested_category,
            suggested_subcategory=suggested_subcategory,
            suggested_service_name=suggested_service_name,
            vip_flag=vip_flag,
        )
    except Exception:
        return (
            _SAFE_DEFAULT_IMPACT,
            _SAFE_DEFAULT_URGENCY,
            {
                "impact": "safe_default_llm_exception",
                "urgency": "safe_default_llm_exception",
            },
        )
    raw_impact = str(result.get("impact", "")).strip()
    raw_urgency = str(result.get("urgency", "")).strip()

    impact_ok = raw_impact in _VALID_IMPACTS
    urgency_ok = raw_urgency in _VALID_URGENCIES

    impact: Impact = raw_impact if impact_ok else _SAFE_DEFAULT_IMPACT  # type: ignore[assignment]
    urgency: Urgency = raw_urgency if urgency_ok else _SAFE_DEFAULT_URGENCY  # type: ignore[assignment]

    # VIP override — if the reporter is VIP, never drop below On Business
    if vip_flag and impact != "On Business":
        impact = "On Business"

    basis = {
        "impact": "llm_inferred" if impact_ok else "safe_default_llm_invalid",
        "urgency": "llm_inferred" if urgency_ok else "safe_default_llm_invalid",
    }
    if vip_flag:
        basis["impact"] += "+vip_override"
    return impact, urgency, basis


# ── Request path (deterministic maps) ────────────────────────────────────────

def _request_path(
    *,
    suggested_category: str | None,
    suggested_catalog_item_id: str | None,
    vip_flag: bool,
    sla_state: str | None,
    derive_impact_block: dict[str, Any],
    derive_urgency_block: dict[str, Any],
) -> tuple[Impact, Urgency, dict[str, str]]:
    # Impact map by catalog category, with VIP override + sensible default
    impact_map = (
        derive_impact_block.get("by_catalog_category") or {}
        if derive_impact_block
        else {}
    )
    impact_default = (
        derive_impact_block.get("default_when_unmatched") or _SAFE_DEFAULT_IMPACT
        if derive_impact_block
        else _SAFE_DEFAULT_IMPACT
    )
    vip_override = (
        derive_impact_block.get("vip_user_override") or "On Business"
        if derive_impact_block
        else "On Business"
    )

    cat_key = (suggested_category or "").lower().strip()
    raw_impact = impact_map.get(cat_key, impact_default)
    if vip_flag:
        raw_impact = vip_override
    impact: Impact = raw_impact if raw_impact in _VALID_IMPACTS else _SAFE_DEFAULT_IMPACT  # type: ignore[assignment]

    impact_basis = (
        "vip_override" if vip_flag
        else f"catalog_category[{cat_key}]" if cat_key in impact_map
        else "default_when_unmatched"
    )

    # Urgency map by SLA state, with default
    urgency_map = (
        derive_urgency_block.get("by_sla_state") or {}
        if derive_urgency_block
        else {}
    )
    urgency_default = (
        derive_urgency_block.get("default_when_no_sla") or _SAFE_DEFAULT_URGENCY
        if derive_urgency_block
        else _SAFE_DEFAULT_URGENCY
    )
    sla_key = (sla_state or "").strip()
    raw_urgency = urgency_map.get(sla_key, urgency_default)
    urgency: Urgency = raw_urgency if raw_urgency in _VALID_URGENCIES else _SAFE_DEFAULT_URGENCY  # type: ignore[assignment]
    urgency_basis = (
        f"sla_state[{sla_key}]" if sla_key in urgency_map
        else "default_when_no_sla"
    )

    return impact, urgency, {"impact": impact_basis, "urgency": urgency_basis}


# ── Matrix lookup ────────────────────────────────────────────────────────────

def _lookup_priority(
    matrix: dict[str, dict[str, str]], impact: str, urgency: str
) -> Priority:
    cell = matrix.get(impact, {}).get(urgency)
    if cell not in _VALID_URGENCIES:
        # Cell missing or malformed in JSON → Pydantic on PrioritizationResult
        # would reject; fall back loudly to "Medium" + record in basis dict.
        return "Medium"
    return cell  # type: ignore[return-value]


# ── service-schema.json loader (just the priority blocks) ────────────────────

def _load_priority_blocks(
    schema_path: Path | None = None,
) -> tuple[
    dict[str, dict[str, str]],  # matrix
    dict[str, Any],              # derive_impact_for_request
    dict[str, Any],              # derive_urgency_for_request
]:
    """Read the Motadata priority matrix + request derivation maps."""
    path = schema_path or _DEFAULT_SCHEMA_PATH
    data = json.loads(Path(path).read_text())
    matrix: dict[str, dict[str, str]] = {}
    derive_impact: dict[str, Any] = {}
    derive_urgency: dict[str, Any] = {}
    for svc in data.get("services", []):
        if svc.get("service_id") == "incident":
            matrix = svc.get("priority_matrix", {}).get("matrix", {})
        if svc.get("service_id") == "request":
            derive_impact = svc.get("derive_impact_for_request", {})
            derive_urgency = svc.get("derive_urgency_for_request", {})
    return matrix, derive_impact, derive_urgency
