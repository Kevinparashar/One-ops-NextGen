"""Agent embedding worker — drains `embedding_refresh_agent`.

Service-specific: the agent vector table has NO tenant_id and is keyed by
agent_id, so this worker uses its OWN UPSERT (not the tenant-scoped base helper).
One trigger message per agent ('agent_all'); the worker rebuilds the active
version's description + use_when + example chunks, UPSERTs them, then prunes any
leftover chunks from a previous (larger) card.

Run standalone:  python database/agent/worker.py
"""
from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "database"))

import asyncpg  # noqa: E402

from _lib._worker_base import (  # noqa: E402
    BaseEmbeddingWorker, EMBED_MODEL, get_logger_, get_tracer_, metric_inc,
    run_cli, sha256,
)
from oneops.embeddings.agent_input import build_agent_chunks  # noqa: E402

_log = get_logger_()
_tracer = get_tracer_()


async def _upsert_agent_chunk(
    conn: asyncpg.Connection, *, agent_id: str, chunk_type: str, chunk_index: int,
    domain: str, content_text: str, content_hash: bytes, embedding: list[float],
) -> None:
    vec = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
    await conn.execute(
        """
        INSERT INTO ai.embeddings_agent
          (agent_id, chunk_type, chunk_index, domain, content_text, embedding,
           content_hash, embedding_model, embedding_version, embedded_at)
        VALUES ($1, $2, $3, $4, $5, $6::vector, $7, $8, 'v1', now())
        ON CONFLICT (agent_id, chunk_type, chunk_index, embedding_version) DO UPDATE
          SET domain          = EXCLUDED.domain,
              content_text    = EXCLUDED.content_text,
              embedding       = EXCLUDED.embedding,
              content_hash    = EXCLUDED.content_hash,
              embedding_model = EXCLUDED.embedding_model,
              embedded_at     = now()
          WHERE ai.embeddings_agent.content_hash IS DISTINCT FROM EXCLUDED.content_hash
             OR ai.embeddings_agent.domain       IS DISTINCT FROM EXCLUDED.domain
        """,
        agent_id, chunk_type, chunk_index, domain, content_text, vec,
        content_hash, EMBED_MODEL)


class AgentEmbeddingWorker(BaseEmbeddingWorker):
    SERVICE_ID = "agent"
    TARGET_TABLE = "ai.embeddings_agent"

    async def process(self, conn: asyncpg.Connection, body: Mapping[str, Any]) -> None:
        agent_id = body["entity_id"]

        with _tracer.start_as_current_span(
            "embeddings.refresh.agent", attributes={"uc.entity_id": agent_id},
        ):
            row = await conn.fetchrow(
                "SELECT body FROM itsm.agent WHERE agent_id=$1 AND status='active' "
                "ORDER BY version DESC LIMIT 1", agent_id)
            if row is None:
                _log.info("embeddings.refresh.agent_row_gone", agent_id=agent_id)
                return  # tombstone — ack

            card = row["body"]
            if isinstance(card, str):
                card = json.loads(card)
            chunks = build_agent_chunks(card)
            if not chunks:
                _log.info("embeddings.refresh.agent_no_chunks", agent_id=agent_id)
                return  # nothing to embed — ack

            domain = (card.get("domain") or "itsm").strip() or "itsm"
            vectors = await self.embed_many([c[2] for c in chunks], tenant_id="_platform")
            max_idx: dict[str, int] = {"description": -1, "use_when": -1, "example": -1}
            for (chunk_type, chunk_index, text), vec in zip(chunks, vectors, strict=False):
                await _upsert_agent_chunk(
                    conn, agent_id=agent_id, chunk_type=chunk_type,
                    chunk_index=chunk_index, domain=domain, content_text=text,
                    content_hash=sha256(text), embedding=vec)
                max_idx[chunk_type] = max(max_idx[chunk_type], chunk_index)

            # Prune leftover chunks from a previous (larger) version of the card.
            for chunk_type, mi in max_idx.items():
                await conn.execute(
                    "DELETE FROM ai.embeddings_agent "
                    "WHERE agent_id=$1 AND chunk_type=$2 AND chunk_index > $3",
                    agent_id, chunk_type, mi)

            metric_inc("ai.embeddings.refreshed.total", 1,
                       service_id="agent", chunk_type="agent_all")
            _log.info("embeddings.refresh.agent_ok", agent_id=agent_id,
                      chunks=len(chunks))


if __name__ == "__main__":
    run_cli(AgentEmbeddingWorker)
