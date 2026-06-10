"""One-shot request embedding backfill — embeds ALL existing requests.

Same builders + UPSERT as database/request/worker.py. Idempotent (CAS guard).

Run:  .venv/bin/python database/request/backfill.py
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
from oneops.embeddings.summarise_diagnosis import summarise_diagnosis  # noqa: E402
from oneops.embeddings.triage_input import build_embedding_input  # noqa: E402

_TARGET = "ai.embeddings_request"
_FETCH_ALL = """
SELECT r.tenant_id, r.request_id, r.title, r.description, r.category,
       r.catalog_item_id, cat.name AS catalog_name, cat.category AS catalog_category,
       r.ci_id, c.ci_name, c.ci_type, r.comments
FROM itsm.request r
LEFT JOIN itsm.catalog_item cat
       ON cat.catalog_item_id = r.catalog_item_id AND cat.tenant_id = r.tenant_id
LEFT JOIN itsm.cmdb_ci c ON c.ci_id = r.ci_id AND c.tenant_id = r.tenant_id
ORDER BY r.request_id
"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", default="symptom_anchor,diagnosis_trail")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    chunks = {c.strip() for c in args.chunks.split(",") if c.strip()}

    conn = await connect()
    gateway = build_gateway()
    try:
        sql = _FETCH_ALL + (f" LIMIT {int(args.limit)}" if args.limit else "")
        rows = await conn.fetch(sql)
        sym = diag = 0
        for r in rows:
            tenant_id, entity_id = r["tenant_id"], r["request_id"]
            if "symptom_anchor" in chunks:
                text = build_embedding_input(dict(r), "request")
                if text.strip():
                    vec = (await gateway.embed([text], model="text-embedding-3-large",
                                               tenant_id=tenant_id, dimensions=1536))[0]
                    await upsert_embedding(conn, target_table=_TARGET, entity_id=entity_id,
                                           tenant_id=tenant_id, chunk_type="symptom_anchor",
                                           embedding=vec, content_hash=sha256(text),
                                           content_text=text)
                    sym += 1
            if "diagnosis_trail" in chunks:
                text = await summarise_diagnosis(gateway=gateway, tenant_id=tenant_id,
                                                 entity_id=entity_id, raw_trail=r.get("comments"))
                if text:
                    vec = (await gateway.embed([text], model="text-embedding-3-large",
                                               tenant_id=tenant_id, dimensions=1536))[0]
                    await upsert_embedding(conn, target_table=_TARGET, entity_id=entity_id,
                                           tenant_id=tenant_id, chunk_type="diagnosis_trail",
                                           embedding=vec, content_hash=sha256(text),
                                           content_text=text)
                    diag += 1
        print(f"request backfill: rows={len(rows)} symptom={sym} diagnosis={diag}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
