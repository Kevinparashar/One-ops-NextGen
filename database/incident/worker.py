"""Incident embedding worker — drains `embedding_refresh_incident`.

Its own service-specific worker: fetches the incident (+ joined CI) for each
enqueued change, builds the right chunk text, embeds it, and UPSERTs into
ai.embeddings_incident. Two chunk types:
  * symptom_anchor  — title/description/category/service/CI (the problem)
  * diagnosis_trail — work_notes summarised by the LLM (the resolution path)

Run standalone (one process):  python database/incident/worker.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from collections.abc import Mapping

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))        # oneops.*
sys.path.insert(0, str(_ROOT / "database"))   # _worker_base

import asyncpg  # noqa: E402

from _lib._worker_base import (  # noqa: E402
    BaseEmbeddingWorker,
    get_logger_,
    get_tracer_,
    metric_inc,
    run_cli,
    sha256,
    upsert_embedding,
)
from oneops.embeddings.summarise_diagnosis import summarise_diagnosis  # noqa: E402
from oneops.embeddings.triage_input import build_embedding_input  # noqa: E402

_log = get_logger_()
_tracer = get_tracer_()

_FETCH_SQL = """
SELECT i.tenant_id, i.incident_id, i.title, i.description, i.category,
       i.subcategory, i.service_name, i.ci_id,
       c.ci_name, c.ci_type, c.location AS ci_location,
       i.work_notes
FROM itsm.incident i
LEFT JOIN itsm.cmdb_ci c ON c.ci_id = i.ci_id AND c.tenant_id = i.tenant_id
WHERE i.incident_id = $1 AND i.tenant_id = $2
"""


class IncidentEmbeddingWorker(BaseEmbeddingWorker):
    SERVICE_ID = "incident"
    TARGET_TABLE = "ai.embeddings_incident"

    async def process(self, conn: asyncpg.Connection, body: Mapping[str, Any]) -> None:
        entity_id = body["entity_id"]
        tenant_id = body["tenant_id"]
        chunk_type = body["chunk_type"]

        with _tracer.start_as_current_span(
            "embeddings.refresh.incident",
            attributes={"oneops.tenant_id": tenant_id, "uc.entity_id": entity_id,
                        "uc.chunk_type": chunk_type},
        ):
            row = await conn.fetchrow(_FETCH_SQL, entity_id, tenant_id)
            if row is None:
                _log.info("embeddings.refresh.row_gone", service="incident",
                          entity_id=entity_id, tenant_id=tenant_id)
                return  # tombstone — ack

            if chunk_type == "symptom_anchor":
                content_text = build_embedding_input(dict(row), "incident")
            elif chunk_type == "diagnosis_trail":
                content_text = await summarise_diagnosis(
                    gateway=self._gateway, tenant_id=tenant_id,
                    entity_id=entity_id, raw_trail=row.get("work_notes"))
                if not content_text:
                    return
            elif chunk_type == "resolution":
                return  # deferred — ack
            else:
                _log.warning("embeddings.refresh.unknown_chunk_type",
                             service="incident", chunk_type=chunk_type)
                return

            embedding = await self.embed_one(content_text, tenant_id=tenant_id)
            await upsert_embedding(
                conn, target_table=self.TARGET_TABLE, entity_id=entity_id,
                tenant_id=tenant_id, chunk_type=chunk_type, embedding=embedding,
                content_hash=sha256(content_text), content_text=content_text)
            metric_inc("ai.embeddings.refreshed.total", 1,
                       service_id="incident", chunk_type=chunk_type)
            _log.info("embeddings.refresh.ok", service="incident",
                      entity_id=entity_id, chunk_type=chunk_type)


if __name__ == "__main__":
    run_cli(IncidentEmbeddingWorker)
