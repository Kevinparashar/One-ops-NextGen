"""Load catalog_item.json + onboarding_template.json into itsm.

Owns the catalog_item + onboarding_template column specs. catalog_item first
(onboarding_template FK -> catalog_item). Requires _foundation. The fulfillment
workflow tables (request_item/task/...) have no seed data — they're populated at
runtime by UC-8.

Run:  .venv/bin/python database/catalog_fulfillment/load_data.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _lib._loader import connect, count, load_table  # noqa: E402

SPECS: dict[str, list[tuple[str, str]]] = {
    "catalog_item": [("tenant_id", "s"), ("catalog_item_id", "s"), ("name", "s"),
        ("description", "s"), ("category", "s"), ("owner_group", "s"),
        ("estimated_total_minutes", "i"), ("tasks", "J[]")],
    "onboarding_template": [("tenant_id", "s"), ("template_id", "s"), ("name", "s"),
        ("description", "s"), ("department", "s"), ("default_catalog_item_id", "s"),
        ("required_inputs", "A"), ("tasks", "J[]")],
}
ORDER = ["catalog_item", "onboarding_template"]


async def main() -> None:
    conn = await connect()
    try:
        async with conn.transaction():
            for table in ORDER:
                n = await load_table(conn, table, SPECS[table])
                print(f"  {table:20s} {n:4d} rows")
        for table in ORDER:
            print(f"  total {table:20s} {await count(conn, table):4d}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
