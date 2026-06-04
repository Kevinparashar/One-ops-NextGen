"""One-shot backfill of ai.embeddings_incident and ai.embeddings_request.

For every existing incident / request, computes the symptom_anchor embedding
(and the diagnosis_trail embedding when work-notes / comments are non-empty)
and writes the row into the appropriate ai.embeddings_* table.

Idempotent: re-running on the same input is a no-op because of the UPSERT
CAS guard (`WHERE content_hash IS DISTINCT FROM …`).

Usage:
    .venv/bin/python scripts/backfill_embeddings.py [--chunks symptom_anchor]
                                                    [--limit N]
                                                    [--service incident|request|both]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from typing import Any, Mapping

import asyncpg

# Ensure src/ is on path when run directly.
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_root, "src"))

from oneops.embeddings.kb_chunker import build_kb_chunks  # noqa: E402
from oneops.embeddings.summarise_diagnosis import summarise_diagnosis  # noqa: E402
from oneops.embeddings.triage_input import build_embedding_input  # noqa: E402
from oneops.llm.gateway import LlmGateway  # noqa: E402
from oneops.llm.transport import LiteLLMTransport  # noqa: E402

_EMBED_MODEL = "text-embedding-3-large"
_EMBED_DIM = 1536


def _sha256(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


async def _fetch_rows(conn: asyncpg.Connection, service: str, limit: int | None):
    if service == "incident":
        sql = """
        SELECT i.tenant_id, i.incident_id AS entity_id, i.title, i.description,
               i.category, i.subcategory, i.service_name, i.ci_id,
               c.ci_name, c.ci_type, c.location AS ci_location,
               i.work_notes
        FROM itsm.incident i
        LEFT JOIN itsm.cmdb_ci c ON c.ci_id = i.ci_id AND c.tenant_id = i.tenant_id
        ORDER BY i.incident_id
        """
    else:
        sql = """
        SELECT r.tenant_id, r.request_id AS entity_id, r.title, r.description,
               r.category,
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
        ORDER BY r.request_id
        """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return await conn.fetch(sql)


async def _upsert(
    conn: asyncpg.Connection, *, target_table: str, entity_id: str,
    tenant_id: str, chunk_type: str, embedding: list[float],
    content_hash: bytes, content_text: str, chunk_index: int = 0,
) -> None:
    vec_literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
    if target_table.endswith("_kb_knowledge"):
        sql = f"""
        INSERT INTO {target_table}
          (entity_id, chunk_type, chunk_index, tenant_id, embedding,
           content_hash, content_text, embedding_model, embedding_version, embedded_at)
        VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8, 'v1', now())
        ON CONFLICT (entity_id, chunk_type, chunk_index, embedding_version) DO UPDATE
          SET embedding       = EXCLUDED.embedding,
              content_hash    = EXCLUDED.content_hash,
              content_text    = EXCLUDED.content_text,
              embedding_model = EXCLUDED.embedding_model,
              embedded_at     = now()
          WHERE {target_table}.content_hash IS DISTINCT FROM EXCLUDED.content_hash
        """
        await conn.execute(sql, entity_id, chunk_type, chunk_index, tenant_id,
                           vec_literal, content_hash, content_text, _EMBED_MODEL)
        return

    sql = f"""
    INSERT INTO {target_table}
      (entity_id, chunk_type, tenant_id, embedding, content_hash, content_text,
       embedding_model, embedding_version, embedded_at)
    VALUES ($1, $2, $3, $4::vector, $5, $6, $7, 'v1', now())
    ON CONFLICT (entity_id, chunk_type, embedding_version) DO UPDATE
      SET embedding       = EXCLUDED.embedding,
          content_hash    = EXCLUDED.content_hash,
          content_text    = EXCLUDED.content_text,
          embedding_model = EXCLUDED.embedding_model,
          embedded_at     = now()
      WHERE {target_table}.content_hash IS DISTINCT FROM EXCLUDED.content_hash
    """
    await conn.execute(sql, entity_id, chunk_type, tenant_id, vec_literal,
                       content_hash, content_text, _EMBED_MODEL)


async def _process_kb(
    *, conn: asyncpg.Connection, gateway: LlmGateway, limit: int | None,
) -> dict:
    target_table = "ai.embeddings_kb_knowledge"
    sql = ("SELECT tenant_id, kb_id AS entity_id, title, summary, content, "
           "       category, tags "
           "FROM itsm.kb_knowledge ORDER BY kb_id")
    if limit: sql += f" LIMIT {int(limit)}"
    rows = await conn.fetch(sql)
    stats = {"rows": len(rows), "anchor": 0, "body_chunks": 0, "skipped": 0, "errors": 0}

    for r in rows:
        row = dict(r)
        anchor_text, body_chunks = build_kb_chunks(row)
        if not anchor_text.strip():
            stats["skipped"] += 1
            continue
        try:
            # Anchor (always 1 row)
            vec = (await gateway.embed(
                [anchor_text], model=_EMBED_MODEL, tenant_id=row["tenant_id"],
                dimensions=_EMBED_DIM))[0]
            await _upsert(conn, target_table=target_table,
                          entity_id=row["entity_id"], tenant_id=row["tenant_id"],
                          chunk_type="kb_anchor", embedding=vec,
                          content_hash=_sha256(anchor_text), content_text=anchor_text,
                          chunk_index=0)
            stats["anchor"] += 1

            # Body (0..N rows)
            if body_chunks:
                vectors = await gateway.embed(
                    body_chunks, model=_EMBED_MODEL, tenant_id=row["tenant_id"],
                    dimensions=_EMBED_DIM)
                for i, (chunk_text, vec) in enumerate(zip(body_chunks, vectors)):
                    await _upsert(conn, target_table=target_table,
                                  entity_id=row["entity_id"], tenant_id=row["tenant_id"],
                                  chunk_type="kb_body", embedding=vec,
                                  content_hash=_sha256(chunk_text),
                                  content_text=chunk_text, chunk_index=i)
                stats["body_chunks"] += len(body_chunks)
        except Exception as exc:                       # noqa: BLE001
            stats["errors"] += 1
            print(f"  ✗ kb {row['entity_id']}: {exc}", file=sys.stderr)
    return stats


async def _process_service(
    *, conn: asyncpg.Connection, gateway: LlmGateway,
    service: str, chunks: list[str], limit: int | None,
) -> dict:
    if service == "kb_knowledge":
        return await _process_kb(conn=conn, gateway=gateway, limit=limit)
    target_table = f"ai.embeddings_{service}"
    rows = await _fetch_rows(conn, service, limit)
    stats = {"rows": len(rows), "symptom": 0, "diagnosis": 0, "skipped": 0, "errors": 0}

    for r in rows:
        row = dict(r)
        # symptom_anchor — always
        if "symptom_anchor" in chunks:
            try:
                text = build_embedding_input(row, service)
                vec = (await gateway.embed(
                    [text], model=_EMBED_MODEL, tenant_id=row["tenant_id"],
                    dimensions=_EMBED_DIM))[0]
                await _upsert(
                    conn, target_table=target_table,
                    entity_id=row["entity_id"], tenant_id=row["tenant_id"],
                    chunk_type="symptom_anchor", embedding=vec,
                    content_hash=_sha256(text), content_text=text,
                )
                stats["symptom"] += 1
            except Exception as exc:                       # noqa: BLE001
                stats["errors"] += 1
                print(f"  ✗ symptom {row['entity_id']}: {exc}", file=sys.stderr)

        # diagnosis_trail — only if there's content to summarise
        if "diagnosis_trail" in chunks:
            raw = row.get("work_notes") if service == "incident" else row.get("comments")
            if not raw:
                stats["skipped"] += 1
                continue
            try:
                summary = await summarise_diagnosis(
                    gateway=gateway, tenant_id=row["tenant_id"],
                    entity_id=row["entity_id"], raw_trail=raw,
                )
                if not summary.strip():
                    stats["skipped"] += 1
                    continue
                vec = (await gateway.embed(
                    [summary], model=_EMBED_MODEL, tenant_id=row["tenant_id"],
                    dimensions=_EMBED_DIM))[0]
                await _upsert(
                    conn, target_table=target_table,
                    entity_id=row["entity_id"], tenant_id=row["tenant_id"],
                    chunk_type="diagnosis_trail", embedding=vec,
                    content_hash=_sha256(summary), content_text=summary,
                )
                stats["diagnosis"] += 1
            except Exception as exc:                       # noqa: BLE001
                stats["errors"] += 1
                print(f"  ✗ diagnosis {row['entity_id']}: {exc}", file=sys.stderr)

    return stats


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--chunks", default="symptom_anchor",
                   help="comma-separated chunk types to backfill")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--service", default="both",
                   choices=("incident", "request", "kb_knowledge", "both"))
    args = p.parse_args()
    chunks = [c.strip() for c in args.chunks.split(",") if c.strip()]

    pg_url = os.environ["POSTGRES_URL"]
    gw_url = os.environ.get("LLM_GATEWAY_URL", "http://localhost:4301")
    transport = LiteLLMTransport(
        base_url=gw_url, api_key=os.environ.get("LITELLM_MASTER_KEY", "sk-1234"))
    gateway = LlmGateway(transport=transport, redact=False)

    conn = await asyncpg.connect(pg_url)
    try:
        services = ["incident", "request"] if args.service == "both" else [args.service]
        print(f"backfill: chunks={chunks} services={services} limit={args.limit}")
        for svc in services:
            stats = await _process_service(
                conn=conn, gateway=gateway, service=svc, chunks=chunks, limit=args.limit)
            print(f"  {svc}: rows={stats['rows']} symptom={stats['symptom']} "
                  f"diagnosis={stats['diagnosis']} skipped={stats['skipped']} "
                  f"errors={stats['errors']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
