"""Load the shared reference tables (sys_user, cmdb_ci, asset, problem, change).

These are FK-referenced by the service tables, so they must exist + be loaded
BEFORE any service slice runs. One transaction in FK-dependency order; the
sys_user self-reference is DEFERRABLE so order within the file is safe.

Run:  .venv/bin/python database/_foundation/load_reference_data.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # database/

from _lib._loader import connect, count, load_table  # noqa: E402

# Each reference table owns its column spec here (foundation-owned tables).
SPECS: dict[str, list[tuple[str, str]]] = {
    "sys_user": [("tenant_id", "s"), ("user_id", "s"), ("name", "s"), ("email", "s"),
        ("role", "s"), ("department", "s"), ("location", "s"), ("manager_id", "s"),
        ("vip", "b"), ("locale", "s"), ("is_active", "b")],
    "cmdb_ci": [("tenant_id", "s"), ("ci_id", "s"), ("ci_name", "s"), ("ci_type", "s"),
        ("environment", "s"), ("status", "s"), ("owner", "s"), ("location", "s"),
        ("criticality", "s"), ("relationships", "J[]"), ("attributes", "J{}")],
    "asset": [("tenant_id", "s"), ("asset_id", "s"), ("asset_name", "s"),
        ("asset_class", "s"), ("subtype", "s"), ("model", "s"), ("vendor", "s"),
        ("serial_number", "s"), ("assigned_to", "s"), ("linked_ci", "s"),
        ("location", "s"), ("status", "s"), ("purchase_date", "dt"),
        ("warranty_expiry", "dt")],
    "problem": [("tenant_id", "s"), ("problem_id", "s"), ("title", "s"),
        ("description", "s"), ("status", "s"), ("priority", "s"), ("category", "s"),
        ("root_cause", "s"), ("workaround", "s"), ("known_error", "b"),
        ("related_incidents", "A"), ("related_changes", "A"), ("owner", "s"),
        ("created_at", "ts"), ("updated_at", "ts")],
    "change": [("tenant_id", "s"), ("change_id", "s"), ("title", "s"),
        ("description", "s"), ("state", "s"), ("change_type", "s"),
        ("risk_level", "s"), ("impact", "s"), ("approval_status", "s"),
        ("approved_by", "A"), ("requested_by", "s"), ("assigned_to", "s"),
        ("assignment_group", "s"), ("affected_ci", "A"), ("related_problem", "s"),
        ("planned_start", "ts"), ("planned_end", "ts"), ("actual_start", "ts"),
        ("actual_end", "ts"), ("created_at", "ts"), ("updated_at", "ts")],
}
ORDER = ["sys_user", "cmdb_ci", "asset", "problem", "change"]


async def main() -> None:
    conn = await connect()
    try:
        print("── _foundation: loading reference tables ──")
        async with conn.transaction():
            for table in ORDER:
                n = await load_table(conn, table, SPECS[table])
                print(f"  {table:12s} {n:4d} rows")
        print("\n── verification ──")
        for table in ORDER:
            print(f"  {table:12s} {await count(conn, table):4d}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
