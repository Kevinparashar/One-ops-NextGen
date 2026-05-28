"""One-shot: embed every kb_knowledge row and persist to the `embedding` column.

Uses the SAME `build_kb_embedding_input()` recipe that UC-3 will use at
query time — imported from `oneops.use_cases.uc03_kb_lookup.kb_embedder`.
This guarantees indexed vectors live in the same embedding space as
runtime query vectors. Never duplicate the recipe here.

Calls OpenAI's `/v1/embeddings` directly (bypassing LiteLLM) because:
  - one-shot maintenance task, not application code
  - avoids requiring a LiteLLM restart to register `text-embedding-3-large`
    before this script can run
The runtime UC-3 retrieval path uses `gateway.embed()` through LiteLLM as
normal — costs/tracing/replay all preserved there.

Run:
    cd "POC copy 4"
    .venv/bin/python -u tools/seed_kb_embeddings.py
    .venv/bin/python -u tools/seed_kb_embeddings.py --force   # re-embed all rows
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

# Make src/ importable so we can reuse the UC's embedding recipe verbatim.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
load_dotenv(_ROOT / ".env")

from oneops.use_cases.uc03_kb_lookup.kb_embedder import (  # noqa: E402
    KB_EMBEDDING_DIMENSIONS,
    KB_EMBEDDING_MODEL,
    build_kb_embedding_input,
)

_OPENAI_URL = "https://api.openai.com/v1/embeddings"
_BATCH_SIZE = 16  # OpenAI accepts up to 2048; 16 is plenty here


async def _embed_batch(client: httpx.AsyncClient, api_key: str, texts: list[str]) -> list[list[float]]:
    resp = await client.post(
        _OPENAI_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": KB_EMBEDDING_MODEL,
            "input": texts,
            "dimensions": KB_EMBEDDING_DIMENSIONS,
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


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="re-embed all rows (default: only rows where embedding IS NULL)")
    parser.add_argument("--dry-run", action="store_true", help="print what would be embedded, do not write")
    args = parser.parse_args()

    pg_url = os.environ.get("POSTGRES_URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not pg_url:
        print("ERROR: POSTGRES_URL not set", file=sys.stderr); return 1
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set", file=sys.stderr); return 1

    where = "" if args.force else "WHERE embedding IS NULL"
    print(f"=== KB EMBEDDING BACKFILL ===")
    print(f"  model:      {KB_EMBEDDING_MODEL}")
    print(f"  dimensions: {KB_EMBEDDING_DIMENSIONS}")
    print(f"  mode:       {'FORCE re-embed all' if args.force else 'fill NULL only'}")
    print(f"  dry_run:    {args.dry_run}")
    print()

    conn = await asyncpg.connect(pg_url)
    rows = await conn.fetch(f"""
        SELECT kb_id, tenant_id, title, summary, content, category, tags
        FROM kb_knowledge
        {where}
        ORDER BY kb_id
    """)
    if not rows:
        print("Nothing to embed — every row already has an embedding. Use --force to re-embed.")
        await conn.close()
        return 0
    print(f"Embedding {len(rows)} row(s)...")

    inputs = []
    kb_ids: list[str] = []
    for r in rows:
        text = build_kb_embedding_input(dict(r))
        inputs.append(text)
        kb_ids.append(r["kb_id"])

    if args.dry_run:
        for kb_id, text in zip(kb_ids, inputs):
            preview = text.replace("\n", " | ")[:120]
            print(f"  {kb_id}: {preview}...")
        await conn.close()
        return 0

    # Embed in batches
    async with httpx.AsyncClient() as http:
        all_vectors: list[list[float]] = []
        for i in range(0, len(inputs), _BATCH_SIZE):
            batch_texts = inputs[i : i + _BATCH_SIZE]
            t0 = time.monotonic()
            batch_vectors = await _embed_batch(http, api_key, batch_texts)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            print(f"  batch {i // _BATCH_SIZE + 1}: {len(batch_texts)} rows → {elapsed_ms}ms")
            all_vectors.extend(batch_vectors)

    # Persist — one UPDATE per row inside a single transaction
    async with conn.transaction():
        for kb_id, vec in zip(kb_ids, all_vectors):
            if len(vec) != KB_EMBEDDING_DIMENSIONS:
                raise RuntimeError(f"{kb_id}: got dim={len(vec)}, expected {KB_EMBEDDING_DIMENSIONS}")
            await conn.execute(
                "UPDATE kb_knowledge SET embedding=$1::vector, embedding_updated_at=now() WHERE kb_id=$2",
                _vec_literal(vec),
                kb_id,
            )

    print()
    embedded = await conn.fetchval("SELECT count(*) FROM kb_knowledge WHERE embedding IS NOT NULL")
    total = await conn.fetchval("SELECT count(*) FROM kb_knowledge")
    print(f"=== ✓ BACKFILL COMPLETE — {embedded}/{total} rows have embeddings ===")
    await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
