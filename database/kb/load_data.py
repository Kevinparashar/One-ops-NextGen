"""Load kb_knowledge.json into itsm.kb_knowledge.

Owns only the kb_knowledge column spec. Requires _foundation (sys_user; FK
created_by -> sys_user).

Run:  .venv/bin/python database/kb/load_data.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _lib._loader import connect, count, load_table  # noqa: E402

SPEC: list[tuple[str, str]] = [
    ("tenant_id", "s"), ("kb_id", "s"), ("title", "s"), ("summary", "s"),
    ("content", "s"), ("category", "s"), ("tags", "A"), ("state", "s"),
    ("audience", "s"), ("created_by", "s"), ("created_at", "ts"),
    ("updated_at", "ts"), ("views", "i"), ("helpful_votes", "i"),
    ("related_ci_ids", "A"), ("related_incidents", "A"),
]


async def main() -> None:
    conn = await connect()
    try:
        async with conn.transaction():
            n = await load_table(conn, "kb_knowledge", SPEC)
        print(f"kb_knowledge: loaded {n} rows  (total now {await count(conn, 'kb_knowledge')})")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
