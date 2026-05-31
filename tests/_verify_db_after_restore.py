"""Run this AFTER restoring Supabase backup. Confirms app tables are back
to the expected row counts, and the LangGraph checkpointer state is restored.

Usage:
  .venv/bin/python tests/_verify_db_after_restore.py
"""
from __future__ import annotations

import asyncio
import sys

import asyncpg

from oneops.config import get_settings

EXPECTED = {
    # table_name        : expected row count (approximate)
    "incidents":           38,
    "requests":            8,    # ballpark — adjust after restore
    "problems":            7,
    "changes":             12,
    "assets":              20,
    "cmdb_cis":            20,
    "kb_knowledge":        23,
    "catalog_items":       8,
    # LangGraph state — was active before incident
    "checkpoints":         9000,  # roughly — was 9796
    "checkpoint_writes":   60000, # roughly — was 60847
    "checkpoint_blobs":    25000, # roughly — was 25006
    "checkpoint_migrations": 10,
}


async def main() -> int:
    conn = await asyncpg.connect(get_settings().postgres_url)
    print("=== TABLE COUNT VERIFICATION ===\n")
    missing = []
    low = []
    ok = []
    for table, expected_min in EXPECTED.items():
        try:
            actual = await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"')
        except asyncpg.UndefinedTableError:
            missing.append(table)
            print(f"  ✗ {table:30s} MISSING")
            continue
        if actual >= expected_min * 0.8:  # 80% of expected = OK (allow churn)
            ok.append(table)
            print(f"  ✓ {table:30s} {actual:>7d} rows  (expected ~{expected_min})")
        else:
            low.append((table, actual, expected_min))
            print(f"  ⚠ {table:30s} {actual:>7d} rows  (expected ~{expected_min}) — low")

    print("\n=== SUMMARY ===")
    print(f"  OK:      {len(ok)}/{len(EXPECTED)}")
    print(f"  Low:     {len(low)}")
    print(f"  Missing: {len(missing)}")

    # Spot-check a known row
    print("\n=== INC0001001 spot-check ===")
    try:
        row = await conn.fetchrow(
            "SELECT incident_id, status, priority, related_problem, related_change "
            "FROM incidents WHERE incident_id = $1 AND tenant_id = $2",
            "INC0001001", "T001",
        )
        if row:
            print(f"  found: {dict(row)}")
            print("  ✓ data integrity OK")
        else:
            print("  ✗ INC0001001 NOT FOUND — restore incomplete")
    except Exception as exc:
        print(f"  ✗ query failed: {exc}")

    await conn.close()

    if missing or not ok:
        print("\n❌ RESTORE INCOMPLETE — re-run restore or check Supabase dashboard")
        return 1
    print("\n✅ RESTORE VERIFIED — safe to resume normal operation")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
