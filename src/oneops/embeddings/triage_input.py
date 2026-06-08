"""Canonical text builder for UC-5 Triage / UC-2 Similar-Tickets embeddings.

Pattern: canonical anchor (common to both tables) + per-service enrichment
(table-specific JOIN fields). Used by:
  * database/<service>/backfill.py (per-service backfill)
  * UC-5 check_duplicate_candidates (query-time)
  * UC-2 similar_tickets retrieval (query-time)

Field selection rationale lives in docs/planning/phase-2-checklist.md §B4.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_INCIDENT = "incident"
_REQUEST = "request"
_SUPPORTED_SERVICES = frozenset({_INCIDENT, _REQUEST})

# Tickets with fewer characters than this after building are almost certainly
# missing title or description. Warn loud — does not block; lets the operator
# decide whether to backfill the missing fields first.
_MIN_REASONABLE_LEN = 30


def build_canonical_anchor(row: Mapping[str, Any]) -> list[str]:
    """The three fields every ticket has — incident or request.

    Returns a list of formatted lines (caller joins with newline). Missing
    fields are silently omitted — `row.get()` defensively handles the dynamic
    shape between incident and request schemas.
    """
    parts: list[str] = []
    if row.get("title"):
        parts.append(f"Title: {row['title']}")
    if row.get("description"):
        parts.append(f"Description: {row['description']}")
    if row.get("category"):
        parts.append(f"Category: {row['category']}")
    return parts


def enrich_incident(row: Mapping[str, Any]) -> list[str]:
    """Incident-only enrichment: service + subcategory + linked CI."""
    parts: list[str] = []
    if row.get("service_name"):
        parts.append(f"Service: {row['service_name']}")
    if row.get("subcategory"):
        parts.append(f"Subcategory: {row['subcategory']}")
    if row.get("ci_name"):
        parts.append(f"Primary CI: {row['ci_name']}")
    if row.get("ci_type"):
        parts.append(f"CI Type: {row['ci_type']}")
    if row.get("ci_location"):
        parts.append(f"CI Location: {row['ci_location']}")
    return parts


def enrich_request(row: Mapping[str, Any]) -> list[str]:
    """Request-only enrichment: linked catalog item + CI (when present)."""
    parts: list[str] = []
    if row.get("catalog_name"):
        parts.append(f"Catalog Item: {row['catalog_name']}")
    if row.get("catalog_category"):
        parts.append(f"Catalog Category: {row['catalog_category']}")
    if row.get("ci_name"):
        parts.append(f"Primary CI: {row['ci_name']}")
    return parts


def build_embedding_input(row: Mapping[str, Any], service_id: str) -> str:
    """Compose canonical anchor + per-service enrichment, joined by newline.

    Raises ValueError for unknown service_id (rule §2.7 no silent failures).
    """
    if service_id not in _SUPPORTED_SERVICES:
        raise ValueError(
            f"unsupported service_id {service_id!r}; expected one of {sorted(_SUPPORTED_SERVICES)}"
        )
    parts = build_canonical_anchor(row)
    if service_id == _INCIDENT:
        parts.extend(enrich_incident(row))
    else:
        parts.extend(enrich_request(row))
    return "\n".join(parts)


def validate_embed_text(text: str, entity_id: str, service_id: str) -> list[str]:
    """Boundary check before paying for an embedding call.

    Returns warnings (caller decides whether to log). Raises RuntimeError on
    empty text — a row with neither title nor description is unembeddable.
    """
    if not text.strip():
        raise RuntimeError(
            f"{entity_id} ({service_id}): empty embedding text — title and description both missing"
        )
    warnings: list[str] = []
    if len(text) < _MIN_REASONABLE_LEN:
        warnings.append(
            f"{entity_id} ({service_id}): short embedding text ({len(text)} chars) "
            f"— title or description likely missing"
        )
    return warnings
