"""Catalog-item embedding text builder.

Production-grade for catalog-template evolution:
  • Scenario A (new field added)   — INSERT a field_map row, no code change.
  • Scenario B (new catalog type)  — no field_map change; same pipeline serves it.
  • Scenario C (column renamed)    — UPDATE the field_map row's source_column.

The builder is **schema-agnostic** — it reads `ai.embedding_field_map` at
refresh time and concatenates the active fields in `ordinal` order using
their `field_role` as the stable conceptual label.

This is a small mirror of `src/oneops/embeddings/triage_input.py` but
with the field set sourced from config rather than hardcoded.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import asyncpg


_FIELD_MAP_CACHE: dict[tuple[str, str, str], list[tuple[str, str]]] = {}


async def load_field_map(
    *, source_table: str, chunk_type: str,
    embedding_version: str, conn: asyncpg.Connection,
) -> list[tuple[str, str]]:
    """Returns [(field_role, source_column), …] ordered by `ordinal`,
    filtered to is_active=true.

    Cached for the lifetime of the worker process — field_map rarely
    changes; restart picks up edits. For frequent edits we can drop the
    cache and read every time (still a single fast query).
    """
    cache_key = (source_table, chunk_type, embedding_version)
    if cache_key in _FIELD_MAP_CACHE:
        return _FIELD_MAP_CACHE[cache_key]

    rows = await conn.fetch(
        """
        SELECT field_role, source_column
          FROM ai.embedding_field_map
         WHERE source_table = $1
           AND chunk_type   = $2
           AND embedding_version = $3
           AND is_active = true
         ORDER BY ordinal
        """,
        source_table, chunk_type, embedding_version,
    )
    mapping = [(r["field_role"], r["source_column"]) for r in rows]
    _FIELD_MAP_CACHE[cache_key] = mapping
    return mapping


def clear_field_map_cache() -> None:
    """Drop the in-process cache. Call after editing ai.embedding_field_map
    if you want zero-downtime pickup; otherwise restart the worker."""
    _FIELD_MAP_CACHE.clear()


def build_catalog_anchor_text(
    row: Mapping[str, Any],
    field_map: list[tuple[str, str]],
) -> str:
    """Build the canonical embed-text for one catalog item.

    Each line is `{Field Role}: {value}` — the role label is stable across
    column renames (the column shifts, the role stays). Empty / NULL
    fields are skipped (no `Owner: None` noise in the embedding).

    Example output:
        Name: Standard developer laptop
        Description: ThinkPad T14 with standard image, VPN client, asset tag
        Category: onboarding
        Owner: GRP-PROCUREMENT
    """
    lines: list[str] = []
    for role, col in field_map:
        val = row.get(col)
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        # Title-case the role for the embed label (name -> Name)
        label = role.replace("_", " ").title()
        lines.append(f"{label}: {val}")
    return "\n".join(lines)


__all__ = [
    "load_field_map",
    "clear_field_map_cache",
    "build_catalog_anchor_text",
]
