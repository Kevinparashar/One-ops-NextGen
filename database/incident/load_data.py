"""Load incident.json into itsm.incident.

Owns ONLY the incident column spec — changing incident columns edits this file
and 01_schema.sql, nothing else. Requires _foundation reference tables to exist
first (FK: reported_by/assigned_to -> sys_user, ci_id -> cmdb_ci,
related_problem -> problem, related_change -> change).

Run:  .venv/bin/python database/incident/load_data.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # database/

from _lib._loader import connect, count, load_table  # noqa: E402

SPEC: list[tuple[str, str]] = [
    ("tenant_id", "s"), ("incident_id", "s"), ("title", "s"), ("description", "s"),
    ("status", "s"), ("priority", "s"), ("severity", "s"), ("impact", "s"),
    ("urgency", "s"), ("category", "s"), ("subcategory", "s"), ("service_name", "s"),
    ("reported_by", "s"), ("assigned_to", "s"), ("assignment_group", "s"),
    ("ci_id", "s"), ("linked_ci_ids", "A"), ("related_problem", "s"),
    ("related_change", "s"), ("attachments", "J[]"), ("work_notes", "J[]"),
    ("comments", "J[]"), ("sla_due", "ts"), ("sla_breached", "b"),
    ("created_at", "ts"), ("updated_at", "ts"), ("resolved_at", "ts"),
]


async def main() -> None:
    conn = await connect()
    try:
        async with conn.transaction():
            n = await load_table(conn, "incident", SPEC)
        print(f"incident: loaded {n} rows  (total now {await count(conn, 'incident')})")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
