"""KB embedding worker — drains `embedding_refresh_kb_knowledge`.

One trigger message per article ('kb_all'); the worker rebuilds 1 anchor +
1..N body chunks (adaptive chunking with overlap), UPSERTs them, then deletes
any leftover body rows from a previous longer version of the article.

Run standalone:  python database/kb/worker.py
"""
from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "database"))

import asyncpg  # noqa: E402

from _lib._worker_base import (  # noqa: E402
    BaseEmbeddingWorker, get_logger_, get_tracer_, metric_inc, run_cli,
    sha256, upsert_embedding,
)
from oneops.embeddings.kb_chunker import build_kb_chunks  # noqa: E402

_log = get_logger_()
_tracer = get_tracer_()

_FETCH_SQL = (
    "SELECT tenant_id, kb_id, title, summary, content, category, tags "
    "FROM itsm.kb_knowledge WHERE kb_id=$1 AND tenant_id=$2"
)


class KbEmbeddingWorker(BaseEmbeddingWorker):
    SERVICE_ID = "kb_knowledge"
    TARGET_TABLE = "ai.embeddings_kb_knowledge"

    async def process(self, conn: asyncpg.Connection, body: Mapping[str, Any]) -> None:
        entity_id = body["entity_id"]
        tenant_id = body["tenant_id"]

        with _tracer.start_as_current_span(
            "embeddings.refresh.kb",
            attributes={"oneops.tenant_id": tenant_id, "uc.entity_id": entity_id},
        ):
            row = await conn.fetchrow(_FETCH_SQL, entity_id, tenant_id)
            if row is None:
                _log.info("embeddings.refresh.kb_row_gone",
                          entity_id=entity_id, tenant_id=tenant_id)
                return  # tombstone — ack

            anchor_text, body_chunks = build_kb_chunks(dict(row))

            if anchor_text.strip():
                vec = await self.embed_one(anchor_text, tenant_id=tenant_id)
                await upsert_embedding(
                    conn, target_table=self.TARGET_TABLE, entity_id=entity_id,
                    tenant_id=tenant_id, chunk_type="kb_anchor", chunk_index=0,
                    has_chunk_index=True, embedding=vec,
                    content_hash=sha256(anchor_text), content_text=anchor_text)

            max_body_idx = -1
            if body_chunks:
                vectors = await self.embed_many(body_chunks, tenant_id=tenant_id)
                for i, (chunk_text, vec) in enumerate(zip(body_chunks, vectors, strict=False)):
                    await upsert_embedding(
                        conn, target_table=self.TARGET_TABLE, entity_id=entity_id,
                        tenant_id=tenant_id, chunk_type="kb_body", chunk_index=i,
                        has_chunk_index=True, embedding=vec,
                        content_hash=sha256(chunk_text), content_text=chunk_text)
                    max_body_idx = i

            # Drop rows from a previous longer version (content shrank).
            await conn.execute(
                f"DELETE FROM {self.TARGET_TABLE} "
                f"WHERE tenant_id=$1 AND entity_id=$2 AND chunk_type='kb_body' "
                f"AND chunk_index > $3",
                tenant_id, entity_id, max_body_idx)

            metric_inc("ai.embeddings.refreshed.total", 1,
                       service_id="kb_knowledge", chunk_type="kb_all")
            _log.info("embeddings.refresh.kb_ok", entity_id=entity_id,
                      body_chunks=len(body_chunks))


if __name__ == "__main__":
    run_cli(KbEmbeddingWorker)
