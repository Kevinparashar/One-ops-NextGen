"""One-time backfill: embed every catalog_item missing from the vector store.

Production-grade properties:
  • **Single-pass drain.** Uses pgmq.read with low visibility timeout
    (vt=1s) and batch size 100 so the script processes everything in
    one execution without waiting on locks from prior reads.
  • **Idempotent.** Re-running embeds nothing if vectors already exist.
  • **Self-healing.** After processing the queue, sweeps for any rows
    still missing from the vector store and enqueues + processes them
    directly (handles cases where the trigger didn't fire, e.g. rows
    inserted before migration 0009 landed).
  • **Same code path as production.** Calls the actual worker dispatcher
    so this script also serves as the substrate end-to-end smoke test.

Usage:
    set -a; source .env; set +a
    .venv/bin/python scripts/uc08_backfill_catalog_embeddings.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import asyncpg


# Visibility-timeout for backfill reads. Production worker uses 30s
# (handles real network errors); backfill processes immediately and
# crashes are recovered by re-running the script.
_BACKFILL_VT_SECONDS = 1
_BATCH_SIZE = 100


async def _enqueue_missing(conn: asyncpg.Connection) -> int:
    """Enqueue refresh messages for catalog rows whose vector is missing."""
    return await conn.fetchval(
        """
        WITH missing AS (
          SELECT c.tenant_id, c.catalog_item_id, c.content_hash_catalog
            FROM itsm.catalog_item c
            LEFT JOIN ai.embeddings_catalog_item e
              ON e.entity_id   = c.catalog_item_id
             AND e.tenant_id   = c.tenant_id
             AND e.chunk_type  = 'catalog_anchor'
           WHERE e.entity_id IS NULL
        ),
        sent AS (
          SELECT pgmq.send('embedding_refresh', jsonb_build_object(
            'target_table',  'ai.embeddings_catalog_item',
            'entity_id',     catalog_item_id,
            'tenant_id',     tenant_id,
            'chunk_type',    'catalog_anchor',
            'enqueued_hash', encode(content_hash_catalog, 'hex')
          )) FROM missing
        )
        SELECT count(*) FROM sent
        """,
    )


async def _drain_catalog_queue(
    conn: asyncpg.Connection, gateway, *, verbose: bool = True,
) -> int:
    """Drain every visible catalog message in one pass. Returns the
    number processed. Stops when no catalog message is visible (either
    queue is empty, or remaining messages are locked by other workers).
    """
    from oneops.embeddings.worker import _process_message

    processed = 0
    while True:
        # Read a batch with short VT. Filter client-side because pgmq.read
        # has no message-filter pushdown.
        rows = await conn.fetch(
            "SELECT msg_id, message FROM pgmq.read("
            "'embedding_refresh', $1::int, $2::int)",
            _BACKFILL_VT_SECONDS, _BATCH_SIZE,
        )
        if not rows:
            break

        catalog_in_batch = 0
        for row in rows:
            body = row["message"]
            if isinstance(body, str):
                body = json.loads(body)
            if body.get("target_table") != "ai.embeddings_catalog_item":
                # Not ours — release the VT lock so the real worker can
                # pick it up later. pgmq.archive followed by re-send would
                # be too aggressive; pgmq has no "release" op so we just
                # let VT expire (1 second).
                continue

            catalog_in_batch += 1
            ok = await _process_message(
                conn=conn, gateway=gateway, body=body,
            )
            if ok:
                await conn.fetchval(
                    "SELECT pgmq.delete('embedding_refresh'::text, $1::bigint)",
                    row["msg_id"],
                )
                processed += 1
                if verbose:
                    print(f"  embedded {body.get('entity_id')}")
            else:
                if verbose:
                    print(f"  FAILED {body.get('entity_id')} — staying on queue")

        # If we read a full batch but none were ours, the queue is
        # effectively drained of catalog messages — stop.
        if catalog_in_batch == 0:
            break

    return processed


async def _drain_with_retries(
    conn: asyncpg.Connection, gateway, *, max_passes: int = 5,
) -> int:
    """Drain across multiple passes: enqueue missing, drain queue, repeat
    until no catalog rows are missing OR max passes exhausted. Production-
    grade because it handles:
      - VT-locked messages from concurrent workers
      - Race where a trigger fires during our drain
      - Rows added by other processes mid-backfill
    """
    total = 0
    for pass_no in range(1, max_passes + 1):
        n_enqueued = await _enqueue_missing(conn)
        if n_enqueued > 0:
            print(f"[pass {pass_no}] enqueued {n_enqueued} missing")
        n_drained = await _drain_catalog_queue(conn, gateway)
        total += n_drained
        if n_drained > 0:
            print(f"[pass {pass_no}] drained {n_drained}")

        # Are we done?
        n_missing = await conn.fetchval(
            """
            SELECT count(*) FROM itsm.catalog_item c
             WHERE NOT EXISTS (
               SELECT 1 FROM ai.embeddings_catalog_item e
                WHERE e.entity_id = c.catalog_item_id
                  AND e.tenant_id = c.tenant_id
                  AND e.chunk_type = 'catalog_anchor'
             )
            """,
        )
        if n_missing == 0:
            print(f"[pass {pass_no}] all catalog items embedded.")
            return total
        if n_enqueued == 0 and n_drained == 0:
            # Nothing to enqueue, nothing to drain, but rows still missing.
            # Likely VT-locked by another worker. Brief wait then retry.
            print(f"[pass {pass_no}] {n_missing} still missing — waiting for VT…")
            await asyncio.sleep(_BACKFILL_VT_SECONDS + 1)

    return total


async def main() -> None:
    pg_url = os.environ.get("POSTGRES_URL")
    if not pg_url:
        sys.exit("POSTGRES_URL not set")

    from oneops.llm.gateway import LlmGateway
    from oneops.llm.transport import LiteLLMTransport

    base_url = os.environ.get("LLM_GATEWAY_URL", "http://127.0.0.1:4001")
    api_key = os.environ.get("LLM_GATEWAY_API_KEY", "")
    transport = LiteLLMTransport(
        base_url=base_url, api_key=api_key, timeout_s=60.0,
    )
    gateway = LlmGateway(transport=transport)

    conn = await asyncpg.connect(pg_url)
    try:
        n_total = await conn.fetchval("SELECT count(*) FROM itsm.catalog_item")
        n_before = await conn.fetchval(
            "SELECT count(*) FROM ai.embeddings_catalog_item",
        )
        # Queue depth gate — even when row counts match, there may be
        # re-embed messages from UPDATEs waiting to be processed.
        q_depth = await conn.fetchval(
            "SELECT count(*) FROM pgmq.q_embedding_refresh "
            "WHERE message->>'target_table' = 'ai.embeddings_catalog_item'",
        )
        print(f"Starting: {n_before}/{n_total} catalog items embedded; "
              f"{q_depth} message(s) on queue.")
        if n_before >= n_total and q_depth == 0:
            print("Nothing to do.")
            return

        processed = await _drain_with_retries(conn, gateway)

        n_after = await conn.fetchval(
            "SELECT count(*) FROM ai.embeddings_catalog_item",
        )
        print(f"\nDone. Processed {processed} messages.")
        print(f"Final: {n_after}/{n_total} catalog items embedded.")
        if n_after < n_total:
            sys.exit(
                f"ERROR: {n_total - n_after} catalog items still missing "
                f"after backfill. Inspect ai.embeddings_catalog_item.",
            )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
