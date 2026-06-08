"""One-shot catalog embedding backfill — embeds ALL existing catalog items.

Field-map-driven, same builder + UPSERT as the worker. Idempotent (CAS guard).

Run:  .venv/bin/python database/catalog_fulfillment/backfill.py
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "database"))

from _lib._loader import connect  # noqa: E402
from _lib._worker_base import build_gateway, sha256, upsert_embedding  # noqa: E402
from oneops.embeddings.catalog_input import (  # noqa: E402
    build_catalog_anchor_text, load_field_map,
)

_TARGET = "ai.embeddings_catalog_item"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    conn = await connect()
    gateway = build_gateway()
    try:
        field_map = await load_field_map(
            source_table="itsm.catalog_item", chunk_type="catalog_anchor",
            embedding_version="v1", conn=conn)
        if not field_map:
            print("catalog backfill: empty field_map — nothing to do")
            return
        cols = sorted({c for _, c in field_map})
        col_list = ", ".join(f'"{c}"' for c in cols)
        sql = (f"SELECT tenant_id, catalog_item_id, {col_list} FROM itsm.catalog_item "
               f"ORDER BY catalog_item_id"
               + (f" LIMIT {int(args.limit)}" if args.limit else ""))
        rows = await conn.fetch(sql)
        done = 0
        for r in rows:
            text = build_catalog_anchor_text(dict(r), field_map)
            if not text.strip():
                continue
            vec = (await gateway.embed([text], model="text-embedding-3-large",
                                       tenant_id=r["tenant_id"], dimensions=1536))[0]
            await upsert_embedding(conn, target_table=_TARGET,
                                   entity_id=r["catalog_item_id"], tenant_id=r["tenant_id"],
                                   chunk_type="catalog_anchor", chunk_index=0,
                                   has_chunk_index=True, embedding=vec,
                                   content_hash=sha256(text), content_text=text)
            done += 1
        print(f"catalog backfill: items={len(rows)} embedded={done}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
