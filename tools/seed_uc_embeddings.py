"""One-shot: embed every active row in `uc_capabilities`.

Uses the SAME `build_capability_embedding_input()` recipe the runtime
shortlister uses for the query side — imported from
`oneops.routing.uc_embedder`. Guarantees indexed corpus vectors and
runtime query vectors live in the same embedding space.

Calls OpenAI's `/v1/embeddings` directly (mirrors `seed_kb_embeddings.py`)
to avoid a LiteLLM restart dependency. Runtime code goes through the
gateway as normal — cost/tracing/replay preserved there.

Run:
    cd "POC copy 4"
    .venv/bin/python -u tools/seed_uc_embeddings.py
    .venv/bin/python -u tools/seed_uc_embeddings.py --force     # re-embed all
    .venv/bin/python -u tools/seed_uc_embeddings.py --include-inactive
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

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
load_dotenv(_ROOT / ".env")

from oneops.routing.uc_embedder import (  # noqa: E402
    UC_EMBEDDING_DIMENSIONS,
    UC_EMBEDDING_MODEL,
    build_capability_embedding_input,
)


_OPENAI_URL = "https://api.openai.com/v1/embeddings"
_BATCH_SIZE = 16


async def _embed_batch(client: httpx.AsyncClient, api_key: str, texts: list[str]) -> list[list[float]]:
    resp = await client.post(
        _OPENAI_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": UC_EMBEDDING_MODEL, "input": texts, "dimensions": UC_EMBEDDING_DIMENSIONS},
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("data") or []
    if len(rows) != len(texts):
        raise RuntimeError(f"embedding count mismatch: in={len(texts)} out={len(rows)}")
    return [r["embedding"] for r in rows]


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--include-inactive", action="store_true",
                        help="also embed inactive rows (future UCs); default skips them to save cost")
    args = parser.parse_args()

    pg_url = os.environ.get("POSTGRES_URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not (pg_url and api_key):
        print("ERROR: POSTGRES_URL or OPENAI_API_KEY not set", file=sys.stderr); return 1

    where = []
    if not args.include_inactive:
        where.append("active = true")
    if not args.force:
        where.append("embedding IS NULL")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    print(f"=== Phase 2 — UC capability embedding backfill ===")
    print(f"  model: {UC_EMBEDDING_MODEL}  dim: {UC_EMBEDDING_DIMENSIONS}")
    print(f"  mode:  {'FORCE re-embed' if args.force else 'fill NULL only'}")
    print(f"  scope: {'all rows' if args.include_inactive else 'active only'}\n")

    conn = await asyncpg.connect(pg_url)
    rows = await conn.fetch(f"""
        SELECT agent_id, capability_id, uc_id, principle_description,
               supported_intents, supported_services
        FROM uc_capabilities
        {where_sql}
        ORDER BY agent_id, capability_id
    """)
    if not rows:
        print("Nothing to embed — every active row already has an embedding.")
        await conn.close(); return 0
    print(f"Embedding {len(rows)} row(s)...\n")

    inputs: list[str] = []
    keys: list[tuple[str, str]] = []
    for r in rows:
        text = build_capability_embedding_input(dict(r))
        inputs.append(text)
        keys.append((r["agent_id"], r["capability_id"]))

    # Embed in batches
    async with httpx.AsyncClient() as http:
        vectors: list[list[float]] = []
        for i in range(0, len(inputs), _BATCH_SIZE):
            batch = inputs[i:i+_BATCH_SIZE]
            t0 = time.monotonic()
            vs = await _embed_batch(http, api_key, batch)
            ms = int((time.monotonic() - t0) * 1000)
            print(f"  batch {i // _BATCH_SIZE + 1}: {len(batch)} rows → {ms}ms")
            vectors.extend(vs)

    # Persist — single transaction
    async with conn.transaction():
        for (agent_id, cap_id), vec in zip(keys, vectors):
            if len(vec) != UC_EMBEDDING_DIMENSIONS:
                raise RuntimeError(f"({agent_id},{cap_id}): wrong dim {len(vec)}")
            await conn.execute(
                "UPDATE uc_capabilities SET embedding=$1::vector, embedding_updated_at=now() "
                "WHERE agent_id=$2 AND capability_id=$3",
                _vec_literal(vec), agent_id, cap_id,
            )

    n_done = await conn.fetchval(
        "SELECT count(*) FROM uc_capabilities WHERE active=true AND embedding IS NOT NULL"
    )
    n_total = await conn.fetchval("SELECT count(*) FROM uc_capabilities WHERE active=true")
    print(f"\n=== ✓ EMBEDDING BACKFILL COMPLETE — {n_done}/{n_total} active rows have embeddings ===")
    await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
