"""One-shot KB embedding backfill — embeds ALL existing articles.

Same chunker + UPSERT as database/kb/worker.py. Idempotent (CAS guard); also
prunes leftover body chunks per article.

Run:  .venv/bin/python database/kb/backfill.py
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
from oneops.embeddings.kb_chunker import build_kb_chunks  # noqa: E402

_TARGET = "ai.embeddings_kb_knowledge"
_FETCH_ALL = (
    "SELECT tenant_id, kb_id, title, summary, content, category, tags "
    "FROM itsm.kb_knowledge ORDER BY kb_id"
)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    conn = await connect()
    gateway = build_gateway()
    try:
        sql = _FETCH_ALL + (f" LIMIT {int(args.limit)}" if args.limit else "")
        rows = await conn.fetch(sql)
        anchors = bodies = 0
        for r in rows:
            tenant_id, entity_id = r["tenant_id"], r["kb_id"]
            anchor_text, body_chunks = build_kb_chunks(dict(r))
            if anchor_text.strip():
                vec = (await gateway.embed([anchor_text], model="text-embedding-3-large",
                                           tenant_id=tenant_id, dimensions=1536))[0]
                await upsert_embedding(conn, target_table=_TARGET, entity_id=entity_id,
                                       tenant_id=tenant_id, chunk_type="kb_anchor",
                                       chunk_index=0, has_chunk_index=True, embedding=vec,
                                       content_hash=sha256(anchor_text), content_text=anchor_text)
                anchors += 1
            max_body = -1
            if body_chunks:
                vectors = await gateway.embed(body_chunks, model="text-embedding-3-large",
                                              tenant_id=tenant_id, dimensions=1536)
                for i, (ct, vec) in enumerate(zip(body_chunks, vectors, strict=False)):
                    await upsert_embedding(conn, target_table=_TARGET, entity_id=entity_id,
                                           tenant_id=tenant_id, chunk_type="kb_body",
                                           chunk_index=i, has_chunk_index=True, embedding=vec,
                                           content_hash=sha256(ct), content_text=ct)
                    bodies += 1
                    max_body = i
            await conn.execute(
                f"DELETE FROM {_TARGET} WHERE tenant_id=$1 AND entity_id=$2 "
                f"AND chunk_type='kb_body' AND chunk_index > $3",
                tenant_id, entity_id, max_body)
        print(f"kb backfill: articles={len(rows)} anchors={anchors} body_chunks={bodies}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
