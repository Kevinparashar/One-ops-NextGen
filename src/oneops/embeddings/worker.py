"""Embedding refresh worker — drains the `embedding_refresh` pgmq queue.

Architecture (P1):

    UC writes ticket  →  trigger fires on hash change  →  pgmq.send(...)
                                                              │
                                                              ▼
                                                  this worker (drain loop)
                                                              │
                              ┌───────────────────────────────┼───────────────────────────────┐
                              ▼                               ▼                               ▼
                  symptom_anchor:                   diagnosis_trail:               resolution: (deferred)
                    rebuild content_text              summarise via LLM
                    via build_embedding_input         then embed the summary
                              │                               │
                              └───────────────────┬───────────┘
                                                  ▼
                                        LlmGateway.embed(...)
                                                  │
                                                  ▼
                            UPSERT ai.embeddings_<service>(...)
                            CAS guard: WHERE content_hash matches enqueued_hash

Failure semantics:
  * LLM failure → log + delete the message anyway (no infinite retry — next
    UPDATE on the row re-enqueues). Add proper retry later if needed.
  * Hash mismatch at CAS time → row changed mid-flight; drop the embedding,
    the newer message in the queue will refresh.
  * Concurrent workers → pgmq.read(visibility_timeout=...) hides messages
    in-flight; SKIP LOCKED happens inside pgmq.

Started in app.py lifespan, gated by env flag `EMBEDDING_WORKER_ENABLED=true`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any, Awaitable, Callable, Mapping

import asyncpg

from oneops.embeddings.kb_chunker import build_kb_chunks
from oneops.embeddings.summarise_diagnosis import summarise_diagnosis
from oneops.embeddings.triage_input import build_embedding_input
from oneops.llm.gateway import LlmGateway
from oneops.observability import get_logger, get_tracer
from oneops.observability.metrics import increment as _metric_inc

_log = get_logger(__name__)
_tracer = get_tracer(__name__)

# How many seconds a message stays invisible after being read (worker crash =
# message reclaimed after this).
_VISIBILITY_TIMEOUT_S = 60
# How many messages to pull per poll.
_BATCH = 5
# Poll interval when the queue is empty.
_IDLE_POLL_S = 2.0
# Embedding model — keep aligned with what UC-5 uses today.
_EMBED_MODEL = "text-embedding-3-large"
_EMBED_DIM = 1536


def _sha256(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


# ── content_text builders per chunk_type ─────────────────────────────────────


def _build_symptom_text(row: Mapping[str, Any], service_id: str) -> str:
    """The symptom anchor: title + description + category + per-service enrichment.

    Uses the existing canonical builder so UC-5 and UC-2 see identical content.
    """
    return build_embedding_input(row, service_id)


def _build_diagnosis_raw(row: Mapping[str, Any], service_id: str) -> Any:
    """Pick the right JSON column for the diagnosis trail.

    Incidents: work_notes column (private agent log).
    Requests: comments column (the conversation thread is the trail).
    """
    if service_id == "incident":
        return row.get("work_notes")
    return row.get("comments")


# ── DB helpers (separate connection pool from app's main pool) ──────────────


async def _fetch_kb_row(
    conn: asyncpg.Connection, *, entity_id: str, tenant_id: str
) -> Mapping[str, Any] | None:
    return await conn.fetchrow(
      "SELECT tenant_id, kb_id, title, summary, content, category, tags "
      "FROM itsm.kb_knowledge WHERE kb_id=$1 AND tenant_id=$2",
      entity_id, tenant_id)


async def _fetch_row_with_ci(
    conn: asyncpg.Connection, *, service_id: str, entity_id: str, tenant_id: str
) -> Mapping[str, Any] | None:
    """Fetch source row + joined CI fields needed by build_embedding_input."""
    if service_id == "incident":
        sql = """
        SELECT i.tenant_id, i.incident_id, i.title, i.description, i.category,
               i.subcategory, i.service_name, i.ci_id,
               c.ci_name, c.ci_type, c.location AS ci_location,
               i.work_notes
        FROM itsm.incident i
        LEFT JOIN itsm.cmdb_ci c ON c.ci_id = i.ci_id AND c.tenant_id = i.tenant_id
        WHERE i.incident_id = $1 AND i.tenant_id = $2
        """
    elif service_id == "request":
        sql = """
        SELECT r.tenant_id, r.request_id, r.title, r.description, r.category,
               r.catalog_item_id,
               cat.name     AS catalog_name,
               cat.category AS catalog_category,
               r.ci_id,
               c.ci_name, c.ci_type,
               r.comments
        FROM itsm.request r
        LEFT JOIN itsm.catalog_item cat
               ON cat.catalog_item_id = r.catalog_item_id AND cat.tenant_id = r.tenant_id
        LEFT JOIN itsm.cmdb_ci c
               ON c.ci_id = r.ci_id AND c.tenant_id = r.tenant_id
        WHERE r.request_id = $1 AND r.tenant_id = $2
        """
    else:
        raise ValueError(f"unsupported service_id {service_id!r}")
    return await conn.fetchrow(sql, entity_id, tenant_id)


async def _upsert_embedding(
    conn: asyncpg.Connection,
    *,
    target_table: str,
    entity_id: str,
    tenant_id: str,
    chunk_type: str,
    embedding: list[float],
    content_hash: bytes,
    content_text: str,
    embedding_model: str,
    embedding_version: str = "v1",
    chunk_index: int = 0,
) -> None:
    """UPSERT — re-running on the same content_hash is a no-op.

    KB table has a chunk_index column (one anchor + N body chunks per article);
    incident/request tables don't (one row per chunk_type). We detect by name.
    """
    vec_literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
    if target_table.endswith("_kb_knowledge"):
        sql = f"""
        INSERT INTO {target_table}
          (entity_id, chunk_type, chunk_index, tenant_id, embedding,
           content_hash, content_text, embedding_model, embedding_version, embedded_at)
        VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8, $9, now())
        ON CONFLICT (entity_id, chunk_type, chunk_index, embedding_version) DO UPDATE
          SET embedding       = EXCLUDED.embedding,
              content_hash    = EXCLUDED.content_hash,
              content_text    = EXCLUDED.content_text,
              embedding_model = EXCLUDED.embedding_model,
              embedded_at     = now()
          WHERE {target_table}.content_hash IS DISTINCT FROM EXCLUDED.content_hash
        """
        await conn.execute(sql, entity_id, chunk_type, chunk_index, tenant_id,
                           vec_literal, content_hash, content_text,
                           embedding_model, embedding_version)
        return

    sql = f"""
    INSERT INTO {target_table}
      (entity_id, chunk_type, tenant_id, embedding, content_hash, content_text,
       embedding_model, embedding_version, embedded_at)
    VALUES ($1, $2, $3, $4::vector, $5, $6, $7, $8, now())
    ON CONFLICT (entity_id, chunk_type, embedding_version) DO UPDATE
      SET embedding       = EXCLUDED.embedding,
          content_hash    = EXCLUDED.content_hash,
          content_text    = EXCLUDED.content_text,
          embedding_model = EXCLUDED.embedding_model,
          embedded_at     = now()
      WHERE {target_table}.content_hash IS DISTINCT FROM EXCLUDED.content_hash
    """
    await conn.execute(sql, entity_id, chunk_type, tenant_id, vec_literal,
                       content_hash, content_text, embedding_model, embedding_version)


# ── Message processing ──────────────────────────────────────────────────────


async def _process_kb_message(
    *,
    conn: asyncpg.Connection,
    gateway: LlmGateway,
    entity_id: str,
    tenant_id: str,
    target_table: str,
) -> bool:
    """Refresh all KB chunks (anchor + body) for one article.

    Adaptive chunking: short articles → 1 anchor + 1 body chunk;
    longer articles → 1 anchor + N body chunks with overlap.

    After UPSERTing the live chunks, DELETE any leftover chunk_index rows
    from a previous (longer) version of the article — handles content
    shrinking.
    """
    with _tracer.start_as_current_span(
        "embeddings.refresh.kb",
        attributes={
            "oneops.tenant_id": tenant_id,
            "uc.entity_id":     entity_id,
        },
    ):
        row = await _fetch_kb_row(
            conn, entity_id=entity_id, tenant_id=tenant_id)
        if row is None:
            _log.info("embeddings.refresh.kb_row_gone",
                      entity_id=entity_id, tenant_id=tenant_id)
            return True

        anchor_text, body_chunks = build_kb_chunks(dict(row))

        if anchor_text.strip():
            vec = (await gateway.embed(
                [anchor_text], model=_EMBED_MODEL,
                tenant_id=tenant_id, dimensions=_EMBED_DIM))[0]
            await _upsert_embedding(
                conn, target_table=target_table,
                entity_id=entity_id, tenant_id=tenant_id,
                chunk_type="kb_anchor", chunk_index=0,
                embedding=vec, content_hash=_sha256(anchor_text),
                content_text=anchor_text, embedding_model=_EMBED_MODEL,
            )

        max_body_idx = -1
        if body_chunks:
            # Batch the body chunks into one embed call when the count is
            # small; LiteLLM accepts list[str]. Keeps the per-article cost
            # to a single HTTP round-trip.
            vectors = await gateway.embed(
                body_chunks, model=_EMBED_MODEL,
                tenant_id=tenant_id, dimensions=_EMBED_DIM)
            for i, (chunk_text, vec) in enumerate(zip(body_chunks, vectors)):
                await _upsert_embedding(
                    conn, target_table=target_table,
                    entity_id=entity_id, tenant_id=tenant_id,
                    chunk_type="kb_body", chunk_index=i,
                    embedding=vec, content_hash=_sha256(chunk_text),
                    content_text=chunk_text, embedding_model=_EMBED_MODEL,
                )
                max_body_idx = i

        # Clean up rows from a previous longer version of the article.
        # If a recent edit shortened the body from 5 chunks to 2, delete
        # rows with chunk_index >= 2.
        await conn.execute(
            f"DELETE FROM {target_table} "
            f"WHERE entity_id=$1 AND chunk_type='kb_body' "
            f"  AND chunk_index > $2",
            entity_id, max_body_idx)

        _metric_inc("ai.embeddings.refreshed.total", 1,
                    chunk_type="kb_all", service_id="kb_knowledge",
                    body_chunks=str(len(body_chunks)))
        _log.info("embeddings.refresh.kb_ok",
                  entity_id=entity_id, body_chunks=len(body_chunks),
                  target_table=target_table)
        return True


async def _process_catalog_message(
    *,
    conn: asyncpg.Connection,
    gateway: LlmGateway,
    entity_id: str,
    tenant_id: str,
    target_table: str,
    chunk_type: str,
) -> bool:
    """UC-8 catalog-item embedding refresh.

    Field-map-driven: reads `ai.embedding_field_map` to know which columns
    contribute to the embed text. Handles template-evolution scenarios
    without redeploy (Scenario A new field, Scenario C column rename).
    """
    from oneops.embeddings.catalog_input import (
        build_catalog_anchor_text, load_field_map,
    )

    if chunk_type != "catalog_anchor":
        _log.warning("embeddings.refresh.unknown_catalog_chunk",
                     entity_id=entity_id, chunk_type=chunk_type)
        return True  # tombstone — unknown chunk type, skip

    with _tracer.start_as_current_span(
        "embeddings.refresh.catalog_anchor",
        attributes={
            "oneops.tenant_id": tenant_id,
            "uc.entity_id":     entity_id,
            "uc.chunk_type":    chunk_type,
        },
    ):
        # Load active field map for catalog_anchor.
        field_map = await load_field_map(
            source_table="itsm.catalog_item",
            chunk_type="catalog_anchor",
            embedding_version="v1",
            conn=conn,
        )
        if not field_map:
            _log.error("embeddings.refresh.empty_field_map",
                       entity_id=entity_id)
            return True  # tombstone — operator config gap

        # Fetch only the columns we actually need (set by field_map).
        cols = sorted({c for _, c in field_map})
        col_list = ", ".join(f'"{c}"' for c in cols)
        row = await conn.fetchrow(
            f"SELECT {col_list} FROM itsm.catalog_item "
            f"WHERE tenant_id=$1 AND catalog_item_id=$2",
            tenant_id, entity_id,
        )
        if row is None:
            _log.info("embeddings.refresh.catalog_row_gone",
                      entity_id=entity_id, tenant_id=tenant_id)
            return True  # tombstone

        content_text = build_catalog_anchor_text(dict(row), field_map)
        if not content_text.strip():
            _log.info("embeddings.refresh.catalog_empty_text",
                      entity_id=entity_id)
            return True

        content_hash = _sha256(content_text)

        vectors = await gateway.embed(
            [content_text],
            model=_EMBED_MODEL,
            tenant_id=tenant_id,
            dimensions=_EMBED_DIM,
        )
        embedding = vectors[0]

        await _upsert_embedding(
            conn,
            target_table=target_table,
            entity_id=entity_id,
            tenant_id=tenant_id,
            chunk_type="catalog_anchor",
            content_text=content_text,
            content_hash=content_hash,
            embedding=embedding,
            embedding_model=_EMBED_MODEL,
        )
        _metric_inc("ai.embeddings.refreshed.total", 1,
                    chunk_type="catalog_anchor",
                    service_id="catalog_item")
        _log.info("embeddings.refresh.catalog_ok",
                  entity_id=entity_id, target_table=target_table,
                  fields=len(field_map))
        return True


async def _process_message(
    *,
    conn: asyncpg.Connection,
    gateway: LlmGateway,
    body: Mapping[str, Any],
) -> bool:
    """Process one message; return True on success (caller deletes msg)."""
    target_table = body["target_table"]            # e.g. ai.embeddings_incident
    entity_id    = body["entity_id"]
    tenant_id    = body["tenant_id"]
    chunk_type   = body["chunk_type"]
    enqueued_hex = body.get("enqueued_hash", "")
    enqueued_hash = bytes.fromhex(enqueued_hex) if enqueued_hex else None

    # KB takes a separate code path: one trigger fires for the article,
    # worker emits anchor (1 row) + body (1..N rows with chunking + overlap).
    if target_table.endswith("_kb_knowledge"):
        return await _process_kb_message(
            conn=conn, gateway=gateway,
            entity_id=entity_id, tenant_id=tenant_id, target_table=target_table,
        )

    # UC-8 catalog items: field-map-driven, single anchor chunk per item.
    if target_table.endswith("_catalog_item"):
        return await _process_catalog_message(
            conn=conn, gateway=gateway,
            entity_id=entity_id, tenant_id=tenant_id,
            target_table=target_table, chunk_type=chunk_type,
        )

    service_id = "incident" if target_table.endswith("_incident") else "request"

    with _tracer.start_as_current_span(
        "embeddings.refresh.process",
        attributes={
            "oneops.tenant_id": tenant_id,
            "uc.entity_id":     entity_id,
            "uc.chunk_type":    chunk_type,
        },
    ):
        row = await _fetch_row_with_ci(
            conn, service_id=service_id, entity_id=entity_id, tenant_id=tenant_id,
        )
        if row is None:
            _log.info("embeddings.refresh.row_gone",
                      entity_id=entity_id, tenant_id=tenant_id,
                      chunk_type=chunk_type)
            return True  # tombstone — delete msg, no work

        # Build content_text for the right chunk
        if chunk_type == "symptom_anchor":
            content_text = _build_symptom_text(dict(row), service_id)
        elif chunk_type == "diagnosis_trail":
            raw = _build_diagnosis_raw(row, service_id)
            content_text = await summarise_diagnosis(
                gateway=gateway, tenant_id=tenant_id,
                entity_id=entity_id, raw_trail=raw,
            )
            if not content_text:
                _log.info("embeddings.refresh.trail_empty",
                          entity_id=entity_id, chunk_type=chunk_type)
                return True
        elif chunk_type == "resolution":
            _log.info("embeddings.refresh.resolution_deferred",
                      entity_id=entity_id)
            return True
        else:
            _log.warning("embeddings.refresh.unknown_chunk_type",
                         entity_id=entity_id, chunk_type=chunk_type)
            return True

        # Hash is over the actual text we embedded (the enriched form).
        # The trigger's hash is over the raw parent fields and is intentionally
        # a DIFFERENT input — its job is staleness detection, not CAS. The
        # UPSERT below skips rewrites when the new content_hash matches the
        # stored one, giving us idempotency under concurrent refreshes.
        content_hash = _sha256(content_text)

        # Embed
        vectors = await gateway.embed(
            [content_text],
            model=_EMBED_MODEL,
            tenant_id=tenant_id,
            dimensions=_EMBED_DIM,
        )
        embedding = vectors[0]

        await _upsert_embedding(
            conn,
            target_table=target_table,
            entity_id=entity_id,
            tenant_id=tenant_id,
            chunk_type=chunk_type,
            embedding=embedding,
            content_hash=content_hash,
            content_text=content_text,
            embedding_model=_EMBED_MODEL,
        )

        _metric_inc("ai.embeddings.refreshed.total", 1,
                    chunk_type=chunk_type, service_id=service_id)
        _log.info("embeddings.refresh.ok",
                  entity_id=entity_id, chunk_type=chunk_type,
                  target_table=target_table)
        return True


# ── Worker loop ─────────────────────────────────────────────────────────────


class EmbeddingRefreshWorker:
    """Long-running async task. start() → loop → stop() cleanly drains."""

    def __init__(
        self, *, gateway: LlmGateway,
        connection_provider: Callable[[], Awaitable[asyncpg.Connection]],
    ) -> None:
        self._gateway = gateway
        self._connect = connection_provider
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="embedding-refresh-worker")
        _log.info("embeddings.worker.started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                self._task.cancel()
            finally:
                self._task = None
        _log.info("embeddings.worker.stopped")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._drain_once()
            except Exception as exc:                       # noqa: BLE001
                _log.warning("embeddings.worker.loop_error",
                             error=str(exc)[:160])
                await asyncio.sleep(_IDLE_POLL_S)

    async def _drain_once(self) -> None:
        conn = await self._connect()
        try:
            rows = await conn.fetch(
                "SELECT msg_id, message::text AS body "
                "FROM pgmq.read('embedding_refresh', $1::int, $2::int)",
                _VISIBILITY_TIMEOUT_S, _BATCH,
            )
            if not rows:
                await asyncio.sleep(_IDLE_POLL_S)
                return
            for r in rows:
                body = json.loads(r["body"])
                ok = False
                try:
                    ok = await _process_message(
                        conn=conn, gateway=self._gateway, body=body,
                    )
                except Exception as exc:                   # noqa: BLE001
                    _log.warning("embeddings.refresh.failed",
                                 error=str(exc)[:160],
                                 entity_id=body.get("entity_id"),
                                 chunk_type=body.get("chunk_type"))
                    _metric_inc("ai.embeddings.failed.total", 1,
                                chunk_type=body.get("chunk_type", "unknown"))
                if ok:
                    await conn.execute(
                        "SELECT pgmq.delete('embedding_refresh', $1::bigint)",
                        r["msg_id"],
                    )
        finally:
            await conn.close()


__all__ = ["EmbeddingRefreshWorker"]
