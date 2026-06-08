"""Request embedding worker — drains `embedding_refresh_request`.

Same two chunk types as incident, but request-specific: enrichment is the
linked catalog item + CI, and the diagnosis trail is the `comments` thread.

Run standalone:  python database/request/worker.py
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
from oneops.embeddings.summarise_diagnosis import summarise_diagnosis  # noqa: E402
from oneops.embeddings.triage_input import build_embedding_input  # noqa: E402

_log = get_logger_()
_tracer = get_tracer_()

_FETCH_SQL = """
SELECT r.tenant_id, r.request_id, r.title, r.description, r.category,
       r.catalog_item_id,
       cat.name     AS catalog_name,
       cat.category AS catalog_category,
       r.ci_id, c.ci_name, c.ci_type,
       r.comments
FROM itsm.request r
LEFT JOIN itsm.catalog_item cat
       ON cat.catalog_item_id = r.catalog_item_id AND cat.tenant_id = r.tenant_id
LEFT JOIN itsm.cmdb_ci c
       ON c.ci_id = r.ci_id AND c.tenant_id = r.tenant_id
WHERE r.request_id = $1 AND r.tenant_id = $2
"""


class RequestEmbeddingWorker(BaseEmbeddingWorker):
    SERVICE_ID = "request"
    TARGET_TABLE = "ai.embeddings_request"

    async def process(self, conn: asyncpg.Connection, body: Mapping[str, Any]) -> None:
        entity_id = body["entity_id"]
        tenant_id = body["tenant_id"]
        chunk_type = body["chunk_type"]

        with _tracer.start_as_current_span(
            "embeddings.refresh.request",
            attributes={"oneops.tenant_id": tenant_id, "uc.entity_id": entity_id,
                        "uc.chunk_type": chunk_type},
        ):
            row = await conn.fetchrow(_FETCH_SQL, entity_id, tenant_id)
            if row is None:
                _log.info("embeddings.refresh.row_gone", service="request",
                          entity_id=entity_id, tenant_id=tenant_id)
                return  # tombstone — ack

            if chunk_type == "symptom_anchor":
                content_text = build_embedding_input(dict(row), "request")
            elif chunk_type == "diagnosis_trail":
                content_text = await summarise_diagnosis(
                    gateway=self._gateway, tenant_id=tenant_id,
                    entity_id=entity_id, raw_trail=row.get("comments"))
                if not content_text:
                    return
            elif chunk_type == "resolution":
                return  # deferred — ack
            else:
                _log.warning("embeddings.refresh.unknown_chunk_type",
                             service="request", chunk_type=chunk_type)
                return

            embedding = await self.embed_one(content_text, tenant_id=tenant_id)
            await upsert_embedding(
                conn, target_table=self.TARGET_TABLE, entity_id=entity_id,
                tenant_id=tenant_id, chunk_type=chunk_type, embedding=embedding,
                content_hash=sha256(content_text), content_text=content_text)
            metric_inc("ai.embeddings.refreshed.total", 1,
                       service_id="request", chunk_type=chunk_type)
            _log.info("embeddings.refresh.ok", service="request",
                      entity_id=entity_id, chunk_type=chunk_type)


if __name__ == "__main__":
    run_cli(RequestEmbeddingWorker)
