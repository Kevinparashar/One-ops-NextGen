"""Catalog embedding worker — drains `embedding_refresh_catalog_item`.

Field-map-driven: reads ai.embedding_field_map to know which catalog columns
contribute to the anchor text, so adding/renaming an embeddable field is a
data change (field_map row), not a code change. Single anchor chunk per item.

Run standalone:  python database/catalog_fulfillment/worker.py
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
from oneops.embeddings.catalog_input import (  # noqa: E402
    build_catalog_anchor_text, load_field_map,
)

_log = get_logger_()
_tracer = get_tracer_()


class CatalogEmbeddingWorker(BaseEmbeddingWorker):
    SERVICE_ID = "catalog_item"
    TARGET_TABLE = "ai.embeddings_catalog_item"

    async def process(self, conn: asyncpg.Connection, body: Mapping[str, Any]) -> None:
        entity_id = body["entity_id"]
        tenant_id = body["tenant_id"]
        chunk_type = body["chunk_type"]
        if chunk_type != "catalog_anchor":
            _log.warning("embeddings.refresh.unknown_catalog_chunk",
                         entity_id=entity_id, chunk_type=chunk_type)
            return  # unknown chunk type — ack

        with _tracer.start_as_current_span(
            "embeddings.refresh.catalog_anchor",
            attributes={"oneops.tenant_id": tenant_id, "uc.entity_id": entity_id},
        ):
            field_map = await load_field_map(
                source_table="itsm.catalog_item", chunk_type="catalog_anchor",
                embedding_version="v1", conn=conn)
            if not field_map:
                _log.error("embeddings.refresh.empty_field_map", entity_id=entity_id)
                return

            cols = sorted({c for _, c in field_map})
            col_list = ", ".join(f'"{c}"' for c in cols)
            row = await conn.fetchrow(
                f"SELECT {col_list} FROM itsm.catalog_item "
                f"WHERE tenant_id=$1 AND catalog_item_id=$2", tenant_id, entity_id)
            if row is None:
                _log.info("embeddings.refresh.catalog_row_gone", entity_id=entity_id)
                return  # tombstone — ack

            content_text = build_catalog_anchor_text(dict(row), field_map)
            if not content_text.strip():
                return

            embedding = await self.embed_one(content_text, tenant_id=tenant_id)
            await upsert_embedding(
                conn, target_table=self.TARGET_TABLE, entity_id=entity_id,
                tenant_id=tenant_id, chunk_type="catalog_anchor", chunk_index=0,
                has_chunk_index=True, embedding=embedding,
                content_hash=sha256(content_text), content_text=content_text)
            metric_inc("ai.embeddings.refreshed.total", 1,
                       service_id="catalog_item", chunk_type="catalog_anchor")
            _log.info("embeddings.refresh.catalog_ok", entity_id=entity_id,
                      fields=len(field_map))


if __name__ == "__main__":
    run_cli(CatalogEmbeddingWorker)
