"""Load request.json into itsm.request.

Owns only the request column spec. Requires _foundation (sys_user, cmdb_ci)
AND catalog_fulfillment loaded first (FK: catalog_item_id -> catalog_item).

Run:  .venv/bin/python database/request/load_data.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _lib._loader import connect, count, load_table  # noqa: E402

SPEC: list[tuple[str, str]] = [
    ("tenant_id", "s"), ("request_id", "s"), ("title", "s"), ("description", "s"),
    ("status", "s"), ("stage", "s"), ("priority", "s"), ("category", "s"),
    ("catalog_item_id", "s"), ("requested_for", "s"), ("requested_by", "s"),
    ("approved_by", "A"), ("assigned_to", "s"), ("assignment_group", "s"),
    ("ci_id", "s"), ("sla_due", "ts"), ("sla_breached", "b"), ("comments", "J[]"),
    ("created_at", "ts"), ("updated_at", "ts"), ("fulfilled_at", "ts"),
]


async def main() -> None:
    conn = await connect()
    try:
        async with conn.transaction():
            n = await load_table(conn, "request", SPEC)
        print(f"request: loaded {n} rows  (total now {await count(conn, 'request')})")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
