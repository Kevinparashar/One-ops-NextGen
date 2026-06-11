"""Load catalog_item.json into itsm.

Owns the catalog_item column spec. Requires _foundation. The fulfillment
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

from oneops.use_cases.uc08_fulfillment.catalog_validation import (  # noqa: E402
    validate_catalog_items,
)

SPEC: list[tuple[str, str]] = [
    ("tenant_id", "s"), ("catalog_item_id", "s"), ("name", "s"),
    ("description", "s"), ("category", "s"), ("owner_group", "s"),
    ("estimated_total_minutes", "i"), ("tasks", "J[]"), ("request_fields", "J[]"),
]
# Upsert so re-running refreshes existing rows (incl. request_fields) WITHOUT
# deleting them — FK-safe (request / request_item / etc. FK -> catalog_item).
CONFLICT_COLS = ["tenant_id", "catalog_item_id"]
UPDATE_COLS = ["name", "description", "category", "owner_group",
               "estimated_total_minutes", "tasks", "request_fields"]


async def main() -> None:
    conn = await connect()
    try:
        async with conn.transaction():
            n = await load_table(conn, "catalog_item", SPEC,
                                  conflict_cols=CONFLICT_COLS, update_cols=UPDATE_COLS,
                                  validate=validate_catalog_items)
            print(f"  catalog_item {n:4d} rows upserted")
        print(f"  total catalog_item {await count(conn, 'catalog_item'):4d}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
