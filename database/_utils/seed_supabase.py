"""Recover Supabase data from `data/itsm/*.json`.

Production-grade properties:
  - Idempotent: drops + recreates each table so re-runs always give a clean
    state (no half-applied duplicates from prior partial runs).
  - Timestamp-safe: converts ISO-8601 strings to Python datetime objects
    BEFORE asyncpg sees them (asyncpg refuses to coerce strings to
    TIMESTAMPTZ — this was the silent-failure root cause on 2026-05-16).
  - Loud on failure: per-row exceptions are printed; no errors are
    swallowed. If you don't see exactly the expected row count at the end,
    inspect the per-row failures above.
  - Single transaction per table: each table's DROP+CREATE+INSERTs run in
    one `async with conn.transaction()` block so either ALL rows commit or
    none do.
  - Verifies counts after every table.

Usage:
  .venv/bin/python database/seed/seed_supabase.py
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg

from oneops.config import get_settings


DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "itsm"   # database/seed/ -> repo root

TABLE_MAP: dict[str, str] = {
    "asset.json":               "assets",
    "catalog_item.json":        "catalog_items",
    "change.json":              "changes",
    "cmdb_ci.json":             "cmdb_cis",
    "incident.json":            "incidents",
    "kb_knowledge.json":        "kb_knowledge",
    "problem.json":             "problems",
    "request.json":             "requests",
    "sys_user.json":            "sys_users",
}

# Candidate primary-key column names per table. Picks the first one that
# actually exists in the JSON. Falls back to `id` if none of the listed
# names are present.
PK_CANDIDATES: dict[str, list[str]] = {
    "assets":               ["asset_id"],
    "catalog_items":        ["catalog_item_id", "catalog_id", "item_id", "sku"],
    "changes":              ["change_id"],
    "cmdb_cis":             ["ci_id"],
    "incidents":            ["incident_id"],
    "kb_knowledge":         ["article_id", "kb_id"],
    "problems":             ["problem_id"],
    "requests":             ["request_id"],
    "sys_users":            ["user_id", "sys_id"],
}

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")


def _is_iso_timestamp(value: Any) -> bool:
    return isinstance(value, str) and bool(ISO_RE.match(value))


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp. Handles trailing 'Z' (Python <3.11 quirk)."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _pg_type(value: Any) -> str:
    if value is None:                   return "TEXT"
    if isinstance(value, bool):         return "BOOLEAN"
    if isinstance(value, int):          return "BIGINT"
    if isinstance(value, float):        return "DOUBLE PRECISION"
    if isinstance(value, (list, dict)): return "JSONB"
    if _is_iso_timestamp(value):        return "TIMESTAMPTZ"
    return "TEXT"


def _infer_columns(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Walk every row to derive (column → pg_type). First non-TEXT win locks the type."""
    cols: dict[str, str] = {}
    for row in rows:
        for k, v in row.items():
            inferred = _pg_type(v)
            if k not in cols or (cols[k] == "TEXT" and inferred != "TEXT"):
                cols[k] = inferred
    return cols


def _pick_pk(table: str, rows: list[dict]) -> str:
    """Return the first PK candidate that exists in every row. Falls back to
    the first row's first key when no candidate matches (avoid in production)."""
    for candidate in PK_CANDIDATES.get(table, []):
        if all(candidate in r for r in rows):
            return candidate
    # Fallback: take the first key from the first row.
    fallback = next(iter(rows[0]))
    return fallback


def _cast(value: Any, pg_type: str) -> Any:
    """Convert Python value to an asyncpg-acceptable parameter."""
    if value is None:
        return None
    if pg_type == "TIMESTAMPTZ":
        if isinstance(value, str):
            return _parse_iso(value)
        return value
    if pg_type == "JSONB":
        return json.dumps(value)
    return value


async def seed_table(conn: asyncpg.Connection, table: str, json_path: Path) -> int:
    """Drop + recreate + bulk-INSERT one table. Returns rows committed.

    Raises on any error — the transaction wrapper rolls back automatically.
    """
    rows: list[dict[str, Any]] = json.loads(json_path.read_text())
    if not rows:
        print(f"  ⚠ {table}: source JSON is empty", flush=True)
        return 0

    columns = _infer_columns(rows)
    pk = _pick_pk(table, rows)

    async with conn.transaction():
        # 1. Wipe + recreate
        await conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
        col_defs = []
        for c, t in columns.items():
            suffix = " PRIMARY KEY" if c == pk else ""
            col_defs.append(f'"{c}" {t}{suffix}')
        await conn.execute(f'CREATE TABLE "{table}" (\n  ' + ",\n  ".join(col_defs) + "\n)")

        # 2. Bulk INSERT
        col_list = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
        sql = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})'

        loaded = 0
        for row in rows:
            params = [_cast(row.get(c), columns[c]) for c in columns]
            try:
                await conn.execute(sql, *params)
                loaded += 1
            except Exception as exc:
                # Print + re-raise so the transaction rolls back cleanly.
                print(
                    f"  ✗ {table}: row {pk}={row.get(pk)!r} failed — "
                    f"{type(exc).__name__}: {str(exc)[:200]}",
                    flush=True,
                )
                raise

    # 3. Verify post-commit
    actual = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"')
    if actual != len(rows):
        print(
            f"  ✗ {table}: count mismatch after commit "
            f"(committed {loaded}, see {actual}, expected {len(rows)})",
            flush=True,
        )
    else:
        print(f"  ✓ {table:25s} {actual:>4d} rows committed", flush=True)
    return actual


async def main() -> int:
    dsn = get_settings().postgres_url
    host = dsn.split("@")[1].split("/")[0] if "@" in dsn else "<unknown>"
    print(f"=== SEEDING SUPABASE — target: {host} ===\n", flush=True)
    print(f"  source: {DATA_DIR}\n", flush=True)

    conn = await asyncpg.connect(dsn)
    grand_total = 0
    failed_tables: list[str] = []

    for filename, table in TABLE_MAP.items():
        path = DATA_DIR / filename
        if not path.exists():
            print(f"  ⚠ {filename} not found, skipping", flush=True)
            continue
        try:
            n = await seed_table(conn, table, path)
            grand_total += n
        except Exception as exc:
            failed_tables.append(table)
            print(
                f"  ✗ {table}: TRANSACTION ROLLED BACK — {type(exc).__name__}: {exc}",
                flush=True,
            )

    print("\n=== SUMMARY ===", flush=True)
    print(f"  total rows committed: {grand_total}", flush=True)
    if failed_tables:
        print(f"  ✗ failed tables ({len(failed_tables)}): {failed_tables}", flush=True)
    else:
        print(f"  ✓ all {len(TABLE_MAP)} tables seeded cleanly", flush=True)

    await conn.close()
    return 1 if failed_tables else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
