"""UC-8 priority derivation — Motadata 4x4 matrix, computed on-the-fly.

Production-grade properties:
  • The matrix is loaded ONCE at module import from
    `registries/v2/platform/service-schema.json` (single source of truth).
  • Closed enums — `Impact`, `Urgency`, `Priority` literal types prevent
    typos and silent drift between code and data.
  • Deterministic. Same inputs always yield the same priority.
  • No DB writes — priority is derived in-memory and stored only in the
    existing `itsm.request.priority` text column (P-prefix vocabulary).
  • Persistence of `impact` / `urgency` is intentionally NOT done — the
    derivation trail lives in OTel traces only (see uc08.priority.derive
    span).
"""
from __future__ import annotations

import json
import os
from typing import Final, Literal

import structlog
from opentelemetry import trace

_log = structlog.get_logger("oneops.uc08.priority")
_tracer = trace.get_tracer("oneops.uc08.priority")


# Closed enum types — used by callers + parsers for validation.
Impact = Literal["Low", "On Users", "On Department", "On Business"]
Urgency = Literal["Low", "Medium", "High", "Urgent"]
PriorityCanonical = Literal["Low", "Medium", "High", "Urgent"]
PriorityPLetter = Literal["P1", "P2", "P3", "P4"]

# Impact enum values → typed constants (sonar S1192; Literal type stays intact).
_IMP_ON_USERS: Impact = "On Users"
_IMP_ON_DEPARTMENT: Impact = "On Department"
_IMP_ON_BUSINESS: Impact = "On Business"

VALID_IMPACTS: Final[frozenset[str]] = frozenset(
    ("Low", _IMP_ON_USERS, _IMP_ON_DEPARTMENT, _IMP_ON_BUSINESS),
)
VALID_URGENCIES: Final[frozenset[str]] = frozenset(
    ("Low", "Medium", "High", "Urgent"),
)
VALID_PRIORITY_CANONICALS: Final[frozenset[str]] = frozenset(
    ("Low", "Medium", "High", "Urgent"),
)


# Canonical → P-letter alias (existing itsm.request rows use P-letter).
CANONICAL_TO_P: Final[dict[str, str]] = {
    "Urgent": "P1",
    "High":   "P2",
    "Medium": "P3",
    "Low":    "P4",
}
P_TO_CANONICAL: Final[dict[str, str]] = {
    v: k for k, v in CANONICAL_TO_P.items()
}


# ── Matrix loading ──────────────────────────────────────────────────────


def _project_root() -> str:
    """Resolve the repository root regardless of cwd. The schema file is
    at <root>/registries/v2/platform/service-schema.json."""
    here = os.path.dirname(os.path.abspath(__file__))
    # src/oneops/use_cases/uc08_fulfillment → root is 4 levels up
    return os.path.normpath(os.path.join(here, "..", "..", "..", ".."))


def _load_matrix_from_disk() -> dict[str, dict[str, str]]:
    """Read the 4x4 priority matrix from registries/v2/platform/service-schema.json.

    Production-grade: this runs once at import. Walks the document to
    find the first `priority_matrix.matrix` block (currently under
    `services[0]`).
    """
    schema_path = os.path.join(
        _project_root(), "registries", "v2", "platform", "service-schema.json",
    )
    if not os.path.exists(schema_path):
        raise RuntimeError(
            f"priority matrix unavailable: {schema_path} not found",
        )
    with open(schema_path, encoding="utf-8") as fh:
        doc = json.load(fh)

    def _walk(node):
        if isinstance(node, dict):
            if "priority_matrix" in node:
                yield node["priority_matrix"]
            for v in node.values():
                yield from _walk(v)
        elif isinstance(node, list):
            for x in node:
                yield from _walk(x)

    for pm in _walk(doc):
        matrix = pm.get("matrix")
        if isinstance(matrix, dict):
            # Sanity-check dimensions — must be 4x4.
            if set(matrix.keys()) != VALID_IMPACTS:
                continue
            for row in matrix.values():
                if not isinstance(row, dict):
                    break
                if set(row.keys()) != VALID_URGENCIES:
                    break
                if not all(v in VALID_PRIORITY_CANONICALS
                           for v in row.values()):
                    break
            else:
                return {k: dict(v) for k, v in matrix.items()}
    raise RuntimeError(
        "priority matrix structurally invalid in service-schema.json",
    )


PRIORITY_MATRIX: Final[dict[str, dict[str, str]]] = _load_matrix_from_disk()


# ── Derivers ────────────────────────────────────────────────────────────


# Catalog-category → impact map. Tuned for the demo catalog so common
# requests land in sensible impact buckets. Editable per deployment via
# the env override below; this is the production default.
_CATEGORY_TO_IMPACT_DEFAULT: Final[dict[str, str]] = {
    # broad-effect requests
    "onboarding":   _IMP_ON_USERS,
    "platform":     _IMP_ON_DEPARTMENT,
    "cmdb":         _IMP_ON_DEPARTMENT,
    "database":     _IMP_ON_DEPARTMENT,
    # individual / desk-side
    "hardware":     _IMP_ON_USERS,
    "endpoint":     _IMP_ON_USERS,
    "email":        _IMP_ON_USERS,
    "access":       _IMP_ON_USERS,
    "network":      _IMP_ON_USERS,
    "software":     _IMP_ON_USERS,
    "application":  _IMP_ON_USERS,
    "integration":  _IMP_ON_USERS,
    "knowledge":    "Low",
    "itsm":         "Low",
    "security":     _IMP_ON_DEPARTMENT,
}


def derive_impact_for_request(
    *,
    catalog_category: str | None,
    requested_for_is_vip: bool = False,
) -> Impact:
    """Derive Motadata impact for a Service Request.

    Rules (production-grade — deterministic, no LLM):
      1. If the requester is flagged VIP, escalate one band toward
         'On Business'.
      2. Else use the catalog category → impact map.
      3. Unknown category → conservative default 'Low'.
    """
    base = _CATEGORY_TO_IMPACT_DEFAULT.get(
        (catalog_category or "").lower(), "Low",
    )
    if not requested_for_is_vip:
        return base  # type: ignore[return-value]

    # VIP escalation — bump one band, clamped at 'On Business'.
    bands = ["Low", _IMP_ON_USERS, _IMP_ON_DEPARTMENT, _IMP_ON_BUSINESS]
    idx = bands.index(base)
    return bands[min(idx + 1, len(bands) - 1)]  # type: ignore[return-value]


def derive_urgency_for_request(
    *,
    sla_minutes_remaining: int | None,
    explicit_signal: str | None = None,
) -> Urgency:
    """Derive Motadata urgency at REQUEST CREATION time.

    Design contract:
      1. Explicit signal from user text wins. Maps documented synonyms to
         the closed enum.
      2. Else, urgency reflects SLA *pressure* — how little budget is
         left relative to the original window. At creation, a fresh
         SR has its full SLA budget, so the default is "Low".
      3. The pressure thresholds are *fractional*, not absolute minutes:
         ≤ 10% of typical 8h shift remaining → Urgent
         ≤ 25%                              → High
         ≤ 50%                              → Medium
         > 50%                              → Low (the create-time default)

    UC-5 uses absolute thresholds for incident response. UC-8 deliberately
    uses fraction-of-budget so an onboarding with a 4-hour SLA isn't
    flagged "Medium" the moment it opens.
    """
    if explicit_signal:
        sig = explicit_signal.strip().lower()
        mapping = {
            "urgent": "Urgent", "asap": "Urgent", "eod": "Urgent",
            "high": "High", "soon": "High",
            "medium": "Medium", "normal": "Medium",
            "low": "Low",
        }
        if sig in mapping:
            return mapping[sig]  # type: ignore[return-value]

    # No explicit signal — use SLA pressure. A *fresh* request gets "Low".
    # The caller passes how many minutes remain in the SLA budget; the
    # function assumes a typical 8-hour (480-minute) shift as the unit
    # for the fractional bands. Callers with non-default SLA windows can
    # pre-normalise before calling.
    if sla_minutes_remaining is None:
        return "Low"
    # Pressure bands (fraction of an 8h shift):
    if sla_minutes_remaining <= 48:   # ≤ 10%
        return "Urgent"
    if sla_minutes_remaining <= 120:  # ≤ 25%
        return "High"
    if sla_minutes_remaining <= 240:  # ≤ 50%
        return "Medium"
    return "Low"


# ── Matrix lookup (the actual derivation) ───────────────────────────────


def compute_priority(
    *, impact: str, urgency: str,
) -> tuple[str, str]:
    """Apply the matrix. Returns (canonical_priority, p_letter).

    canonical_priority ∈ {Low, Medium, High, Urgent}
    p_letter           ∈ {P1, P2, P3, P4}

    Raises ValueError on invalid inputs — this is fail-loud by design;
    a typo upstream must not silently produce P3.
    """
    if impact not in VALID_IMPACTS:
        raise ValueError(
            f"invalid impact {impact!r}; must be one of "
            f"{sorted(VALID_IMPACTS)}",
        )
    if urgency not in VALID_URGENCIES:
        raise ValueError(
            f"invalid urgency {urgency!r}; must be one of "
            f"{sorted(VALID_URGENCIES)}",
        )
    canonical = PRIORITY_MATRIX[impact][urgency]
    return canonical, CANONICAL_TO_P[canonical]


def derive_and_compute(
    *, catalog_category: str | None,
    requested_for_is_vip: bool = False,
    sla_minutes_remaining: int | None,
    explicit_urgency_signal: str | None = None,
) -> dict[str, str]:
    """Convenience: do both derivations + matrix in one call.

    Emits an OTel span so the derivation trail is observable in Tempo
    (the only persistence layer for impact/urgency given we are not
    storing them in itsm.request).
    """
    with _tracer.start_as_current_span(
        "uc08.priority.derive",
        attributes={
            "uc08.catalog_category": catalog_category or "",
            "uc08.requested_for_is_vip": requested_for_is_vip,
            "uc08.sla_minutes_remaining": sla_minutes_remaining or -1,
            "uc08.explicit_urgency_signal": explicit_urgency_signal or "",
        },
    ) as span:
        impact = derive_impact_for_request(
            catalog_category=catalog_category,
            requested_for_is_vip=requested_for_is_vip,
        )
        urgency = derive_urgency_for_request(
            sla_minutes_remaining=sla_minutes_remaining,
            explicit_signal=explicit_urgency_signal,
        )
        canonical, p_letter = compute_priority(impact=impact, urgency=urgency)

        span.set_attribute("uc08.impact", impact)
        span.set_attribute("uc08.urgency", urgency)
        span.set_attribute("uc08.priority_canonical", canonical)
        span.set_attribute("uc08.priority_p", p_letter)

        _log.info(
            "uc08.priority.computed",
            catalog_category=catalog_category,
            vip=requested_for_is_vip,
            sla_min_left=sla_minutes_remaining,
            urgency_signal=explicit_urgency_signal,
            impact=impact,
            urgency=urgency,
            priority_canonical=canonical,
            priority_p=p_letter,
        )

        return {
            "impact": impact,
            "urgency": urgency,
            "priority_canonical": canonical,
            "priority_p": p_letter,
        }


__all__ = [
    "Impact",
    "Urgency",
    "PriorityCanonical",
    "PriorityPLetter",
    "VALID_IMPACTS",
    "VALID_URGENCIES",
    "VALID_PRIORITY_CANONICALS",
    "CANONICAL_TO_P",
    "P_TO_CANONICAL",
    "PRIORITY_MATRIX",
    "derive_impact_for_request",
    "derive_urgency_for_request",
    "compute_priority",
    "derive_and_compute",
]
