"""One-shot: embed every itsm.incident and itsm.request row and persist to
the `embedding` column.

WHY: UC-5 Triage's check_duplicate_candidates tool uses hybrid retrieval
(vector + FTS + RRF fusion + relevance gate) — the same pattern UC-3 KB
lookup uses. Before UC-5 can semantically match a new ticket against the
existing corpus, every existing ticket must have its meaning fingerprint
(embedding) populated. This script is the one-time backfill.

MIRROR of tools/seed_kb_embeddings.py — same model, same dimensions, same
batch size, same idempotency contract. Differences:
  * targets two tables (itsm.incident + itsm.request) not one
  * embeds `title + "\\n" + description` (the UC-5 canonical input — title
    weighted higher implicitly by tsvector at the FTS layer, but the
    embedding sees both equally)
  * also writes the audit columns (embedding_model, embedding_version,
    embedded_at) so we can find stale embeddings after a model upgrade

PRE-FLIGHT (operator must have done these BEFORE running this script):
  1. psql "$DATABASE_URL" -f migrations/0003_incident_request_embedding.sql
     (adds the embedding column on both tables; this script writes to it)

Calls OpenAI's /v1/embeddings directly (bypassing LiteLLM) — see KB
seeder for the rationale. Runtime UC-5 retrieval will use gateway.embed()
through LiteLLM as normal, preserving cost tracking and tracing.

Run:
    cd Oneops-NextGen
    .venv/bin/python -u tools/seed_incident_embeddings.py
    .venv/bin/python -u tools/seed_incident_embeddings.py --force
    .venv/bin/python -u tools/seed_incident_embeddings.py --dry-run
    .venv/bin/python -u tools/seed_incident_embeddings.py --table incident
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import asyncpg
import httpx
from dotenv import load_dotenv

# Bootstrap src/ onto sys.path so the shared text builder is importable when
# this script is invoked directly (no `pip install -e .` requirement).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from oneops.embeddings.triage_input import (  # noqa: E402
    build_embedding_input,
    validate_embed_text,
)

# Match the KB embedder choices verbatim so UC-3 helpers (rrf_fuse,
# relevance gate, embed_query cache) are reusable by UC-5 without
# vector-space mismatch.
EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_DIMENSIONS = 1536
EMBEDDING_VERSION = "1.0"

# Per-table config. service_id matches registries/service-schema.json and
# is what build_embedding_input dispatches on.
_INCIDENT_SQL = """
SELECT
    i.incident_id    AS id,
    i.tenant_id      AS tenant_id,
    i.title          AS title,
    i.description    AS description,
    i.category       AS category,
    i.subcategory    AS subcategory,
    i.service_name   AS service_name,
    ci.ci_name       AS ci_name,
    ci.ci_type       AS ci_type,
    ci.location      AS ci_location
FROM itsm.incident i
LEFT JOIN itsm.cmdb_ci ci
       ON ci.tenant_id = i.tenant_id
      AND ci.ci_id     = i.ci_id
{where}
ORDER BY i.incident_id
"""

_REQUEST_SQL = """
SELECT
    r.request_id        AS id,
    r.tenant_id         AS tenant_id,
    r.title             AS title,
    r.description       AS description,
    r.category          AS category,
    cat.name            AS catalog_name,
    cat.category        AS catalog_category,
    ci.ci_name          AS ci_name
FROM itsm.request r
LEFT JOIN itsm.catalog_item cat
       ON cat.tenant_id        = r.tenant_id
      AND cat.catalog_item_id  = r.catalog_item_id
LEFT JOIN itsm.cmdb_ci ci
       ON ci.tenant_id = r.tenant_id
      AND ci.ci_id     = r.ci_id
{where}
ORDER BY r.request_id
"""

_TABLES: list[tuple[str, str, str, str]] = [
    # (fully qualified table name, primary key column, service_id, SELECT template)
    ("itsm.incident", "incident_id", "incident", _INCIDENT_SQL),
    ("itsm.request",  "request_id",  "request",  _REQUEST_SQL),
]

_OPENAI_URL = "https://api.openai.com/v1/embeddings"
_BATCH_SIZE = 16  # same as KB seeder; OpenAI accepts up to 2048

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


async def _embed_batch(
    client: httpx.AsyncClient, api_key: str, texts: list[str]
) -> list[list[float]]:
    resp = await client.post(
        _OPENAI_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": EMBEDDING_MODEL,
            "input": texts,
            "dimensions": EMBEDDING_DIMENSIONS,
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("data") or []
    if len(rows) != len(texts):
        raise RuntimeError(f"embedding count mismatch: in={len(texts)} out={len(rows)}")
    return [r["embedding"] for r in rows]


def _vec_literal(vec: list[float]) -> str:
    """pgvector text literal — `[v1,v2,...]`. Tighter than JSON."""
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


async def _embed_table(
    conn: asyncpg.Connection,
    http: httpx.AsyncClient,
    api_key: str,
    table: str,
    id_col: str,
    service_id: str,
    sql_template: str,
    force: bool,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Embed one table. Returns (total_rows_seen, embedded_this_run, skipped)."""
    # `{where}` placeholder injects the IS NULL guard for the default fill-only
    # mode; --force re-embeds every row.
    alias = "i" if service_id == "incident" else "r"
    where = "" if force else f"WHERE {alias}.embedding IS NULL"
    rows = await conn.fetch(sql_template.format(where=where))
    total = await conn.fetchval(f"SELECT count(*) FROM {table}")

    if not rows:
        print(f"  {table}: nothing to embed (every row already has an embedding; use --force to re-embed)")
        return total, 0, 0

    print(f"  {table}: {len(rows)} row(s) to embed (of {total} total)")

    inputs: list[str] = []
    ids: list[str] = []
    for r in rows:
        row_dict = dict(r)
        try:
            text = build_embedding_input(row_dict, service_id)
            warnings = validate_embed_text(text, row_dict["id"], service_id)
        except RuntimeError as exc:
            # Empty title and description — cannot embed; skip with loud notice.
            print(f"    SKIP {exc}")
            continue
        for w in warnings:
            print(f"    WARN {w}")
        inputs.append(text)
        ids.append(row_dict["id"])

    if dry_run:
        for entity_id, text in zip(ids, inputs):
            preview = text.replace("\n", " | ")[:120]
            print(f"    [dry] {entity_id}: {preview}...")
        return total, 0, len(rows) - len(ids)

    # Embed in batches
    all_vectors: list[list[float]] = []
    for i in range(0, len(inputs), _BATCH_SIZE):
        batch_texts = inputs[i : i + _BATCH_SIZE]
        t0 = time.monotonic()
        batch_vectors = await _embed_batch(http, api_key, batch_texts)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        print(f"    batch {i // _BATCH_SIZE + 1}: {len(batch_texts)} rows → {elapsed_ms}ms")
        all_vectors.extend(batch_vectors)

    # Persist — one UPDATE per row inside a single transaction
    async with conn.transaction():
        for entity_id, vec in zip(ids, all_vectors):
            if len(vec) != EMBEDDING_DIMENSIONS:
                raise RuntimeError(
                    f"{entity_id}: got dim={len(vec)}, expected {EMBEDDING_DIMENSIONS}"
                )
            await conn.execute(
                f"""
                UPDATE {table}
                SET embedding         = $1::vector,
                    embedding_model   = $2,
                    embedding_version = $3,
                    embedded_at       = now()
                WHERE {id_col} = $4
                """,
                _vec_literal(vec),
                EMBEDDING_MODEL,
                EMBEDDING_VERSION,
                entity_id,
            )

    embedded_now = len(ids)
    skipped = len(rows) - embedded_now
    return total, embedded_now, skipped


async def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill embeddings for itsm.incident + itsm.request.")
    parser.add_argument("--force", action="store_true",
                        help="re-embed all rows (default: only rows where embedding IS NULL)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be embedded, do not write")
    parser.add_argument("--table", choices=["incident", "request", "both"], default="both",
                        help="restrict to one table (default: both)")
    args = parser.parse_args()

    # Honour both POSTGRES_URL and DATABASE_URL (the migration runbook uses DATABASE_URL).
    pg_url = os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not pg_url:
        print("ERROR: POSTGRES_URL (or DATABASE_URL) not set", file=sys.stderr)
        return 1
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr)
        return 1

    selected = {
        "incident": [_TABLES[0]],
        "request":  [_TABLES[1]],
        "both":     _TABLES,
    }[args.table]

    print("=== INCIDENT + REQUEST EMBEDDING BACKFILL ===")
    print(f"  model:      {EMBEDDING_MODEL}")
    print(f"  dimensions: {EMBEDDING_DIMENSIONS}")
    print(f"  version:    {EMBEDDING_VERSION}")
    print(f"  mode:       {'FORCE re-embed all' if args.force else 'fill NULL only'}")
    print(f"  dry_run:    {args.dry_run}")
    print(f"  tables:     {[t for t, *_ in selected]}")
    print()

    conn = await asyncpg.connect(pg_url)

    # Pre-flight: ensure the embedding column exists on every selected table.
    for table, *_ in selected:
        schema_name, table_name = table.split(".")
        has_col = await conn.fetchval(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema=$1 AND table_name=$2 AND column_name='embedding'
            """,
            schema_name, table_name,
        )
        if not has_col:
            print(
                f"ERROR: {table}.embedding column does not exist. "
                f"Run migrations/0003_incident_request_embedding.sql first "
                f"(rule §2.11 — operator runs migrations).",
                file=sys.stderr,
            )
            await conn.close()
            return 2

    grand_total = grand_embedded = grand_skipped = 0
    async with httpx.AsyncClient() as http:
        for table, id_col, service_id, sql_template in selected:
            try:
                total, embedded, skipped = await _embed_table(
                    conn, http, api_key, table, id_col, service_id, sql_template,
                    args.force, args.dry_run,
                )
                grand_total += total
                grand_embedded += embedded
                grand_skipped += skipped
            except Exception as exc:
                print(f"  ✗ {table} FAILED: {exc}", file=sys.stderr)
                await conn.close()
                return 3

    print()
    print("=== ✓ BACKFILL COMPLETE ===")
    for table, *_ in selected:
        count_with_emb = await conn.fetchval(
            f"SELECT count(*) FROM {table} WHERE embedding IS NOT NULL"
        )
        count_total = await conn.fetchval(f"SELECT count(*) FROM {table}")
        print(f"  {table}: {count_with_emb}/{count_total} rows have embeddings")
    print(f"  this run embedded {grand_embedded} row(s); skipped {grand_skipped}")
    estimated_cost_usd = (grand_embedded * EMBEDDING_DIMENSIONS) * 0.00000013 / 1000 * 0.5  # rough
    print(f"  estimated cost: ~${estimated_cost_usd:.4f} (text-embedding-3-large pricing)")
    await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
