"""Shared plumbing for the per-service embedding workers.

This is NOT a generic worker. Every service has its OWN worker class, its own
queue, and its own process() logic, living in database/<service>/worker.py and
runnable on its own (`python database/<service>/worker.py`). This module holds
only the mechanical bits each worker would otherwise duplicate:

  * the pgmq poll → claim → process → ack → retry loop (BaseEmbeddingWorker)
  * the tenant-scoped vector UPSERT (upsert_embedding) for entity services
  * a CLI runner that builds the gateway + connection from env and runs one
    worker until SIGINT/SIGTERM

Each service worker subclasses BaseEmbeddingWorker, sets SERVICE_ID, and
implements `process(conn, body)`. The agent worker, whose vector table has no
tenant_id and is keyed by agent_id, supplies its own UPSERT — that's the point
of per-service workers: no service is forced through a shared code path.

Per-service queues: each service drains `embedding_refresh_<service_id>`, so a
bulk reindex on one service never blocks another (no head-of-line blocking).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import asyncpg

_ENV_ROOT = Path(__file__).resolve().parents[2]   # database/_lib/_worker_base.py -> repo root


def _load_env() -> None:
    """Load repo-root .env into os.environ for standalone worker processes."""
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_ROOT / ".env")
    except Exception:                                  # noqa: BLE001
        pass

from oneops.llm.gateway import LlmGateway
from oneops.observability import get_logger, get_tracer
from oneops.observability.metrics import increment as _metric_inc

_log = get_logger(__name__)
_tracer = get_tracer(__name__)

# Embedding model — keep aligned across every worker + backfill (one space).
EMBED_MODEL = "text-embedding-3-large"
EMBED_DIM = 1536

# Loop tuning.
VISIBILITY_TIMEOUT_S = 60   # message hidden this long after read (crash reclaim)
BATCH = 5                   # messages per poll
IDLE_POLL_S = 2.0          # sleep when the queue is empty


def sha256(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def queue_name(service_id: str) -> str:
    """The per-service queue every service worker drains."""
    return f"embedding_refresh_{service_id}"


async def upsert_embedding(
    conn: asyncpg.Connection,
    *,
    target_table: str,
    entity_id: str,
    tenant_id: str,
    chunk_type: str,
    embedding: list[float],
    content_hash: bytes,
    content_text: str,
    embedding_model: str = EMBED_MODEL,
    embedding_version: str = "v1",
    chunk_index: int = 0,
    has_chunk_index: bool = False,
) -> None:
    """Tenant-scoped UPSERT for the entity vector tables (incident/request/kb/
    catalog). Re-running with the same content_hash is a no-op (CAS guard).

    `has_chunk_index=True` (kb) writes the chunk_index column and conflicts on
    (entity_id, chunk_type, chunk_index, embedding_version). Agent uses its own
    UPSERT (no tenant_id, agent_id key) — not this helper.
    """
    vec_literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
    if has_chunk_index:
        sql = f"""
        INSERT INTO {target_table}
          (entity_id, chunk_type, chunk_index, tenant_id, embedding,
           content_hash, content_text, embedding_model, embedding_version, embedded_at)
        VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8, $9, now())
        ON CONFLICT (tenant_id, entity_id, chunk_type, chunk_index, embedding_version) DO UPDATE
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
    ON CONFLICT (tenant_id, entity_id, chunk_type, embedding_version) DO UPDATE
      SET embedding       = EXCLUDED.embedding,
          content_hash    = EXCLUDED.content_hash,
          content_text    = EXCLUDED.content_text,
          embedding_model = EXCLUDED.embedding_model,
          embedded_at     = now()
      WHERE {target_table}.content_hash IS DISTINCT FROM EXCLUDED.content_hash
    """
    await conn.execute(sql, entity_id, chunk_type, tenant_id, vec_literal,
                       content_hash, content_text, embedding_model, embedding_version)


class BaseEmbeddingWorker:
    """The poll/ack loop only. Subclass per service: set SERVICE_ID + process().

    Failure semantics (unchanged from the original single worker):
      * process() raises  → log + metric, DO NOT delete (message reappears
        after the visibility timeout; the next change also re-enqueues).
      * process() returns (normally) → delete the message. Returning normally
        means "this change is handled" — whether that was a successful embed,
        a tombstone (row gone), or a deliberate skip (deferred / unknown chunk
        type). Signal a retryable failure by RAISING, never by a return value.
      * crash mid-flight → pgmq visibility timeout reclaims the message.
    """

    SERVICE_ID: str = ""        # subclass MUST set (e.g. "incident")
    TARGET_TABLE: str = ""      # subclass MUST set (e.g. "ai.embeddings_incident")

    def __init__(
        self,
        *,
        gateway: LlmGateway,
        connection_provider: Callable[[], Awaitable[asyncpg.Connection]],
    ) -> None:
        if not self.SERVICE_ID:
            raise ValueError(f"{type(self).__name__} must set SERVICE_ID")
        self._gateway = gateway
        self._connect = connection_provider
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.queue = queue_name(self.SERVICE_ID)

    # ── service hook ────────────────────────────────────────────────────────
    async def process(self, conn: asyncpg.Connection, body: Mapping[str, Any]) -> None:
        """Handle one enqueued change. Return normally to ack (delete) the
        message; raise to leave it for the visibility-timeout retry."""
        raise NotImplementedError

    # ── embedding helpers available to subclasses ───────────────────────────
    async def embed_one(self, text: str, *, tenant_id: str) -> list[float]:
        return (await self._gateway.embed(
            [text], model=EMBED_MODEL, tenant_id=tenant_id, dimensions=EMBED_DIM))[0]

    async def embed_many(self, texts: list[str], *, tenant_id: str) -> list[list[float]]:
        return await self._gateway.embed(
            texts, model=EMBED_MODEL, tenant_id=tenant_id, dimensions=EMBED_DIM)

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def ensure_queue(self) -> None:
        """Create this service's queue if absent (idempotent, defensive)."""
        conn = await self._connect()
        try:
            await conn.execute("SELECT pgmq.create($1)", self.queue)
        except Exception:                                  # noqa: BLE001 — exists
            pass
        finally:
            await conn.close()

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.ensure_queue()
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(), name=f"embedding-worker-{self.SERVICE_ID}")
        _log.info("embeddings.worker.started",
                  service=self.SERVICE_ID, queue=self.queue)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except TimeoutError:
                self._task.cancel()
            finally:
                self._task = None
        _log.info("embeddings.worker.stopped", service=self.SERVICE_ID)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._drain_once()
            except Exception as exc:                       # noqa: BLE001
                _log.warning("embeddings.worker.loop_error",
                             service=self.SERVICE_ID, error=str(exc)[:160])
                await asyncio.sleep(IDLE_POLL_S)

    async def _drain_once(self) -> None:
        conn = await self._connect()
        try:
            rows = await conn.fetch(
                "SELECT msg_id, message::text AS body "
                "FROM pgmq.read($1, $2::int, $3::int)",
                self.queue, VISIBILITY_TIMEOUT_S, BATCH,
            )
            if not rows:
                await asyncio.sleep(IDLE_POLL_S)
                return
            for r in rows:
                body = json.loads(r["body"])
                try:
                    await self.process(conn, body)
                except Exception as exc:                   # noqa: BLE001
                    _log.warning("embeddings.refresh.failed",
                                 service=self.SERVICE_ID, error=str(exc)[:160],
                                 entity_id=body.get("entity_id"),
                                 chunk_type=body.get("chunk_type"))
                    _metric_inc("ai.embeddings.failed.total", 1,
                                service_id=self.SERVICE_ID,
                                chunk_type=body.get("chunk_type", "unknown"))
                    continue  # leave the message for the visibility-timeout retry
                # process() returned normally → the change is handled; ack it.
                await conn.execute(
                    "SELECT pgmq.delete($1, $2::bigint)", self.queue, r["msg_id"])
        finally:
            await conn.close()


# ── module-level helpers for the per-service workers ─────────────────────────

def get_tracer_():  # small accessor so workers can open spans without re-importing
    return _tracer


def metric_inc(name: str, value: int = 1, **labels: str) -> None:
    _metric_inc(name, value, **labels)


def get_logger_():
    return _log


def build_gateway() -> LlmGateway:
    """Build an LlmGateway from env (mirrors backfill) for standalone workers."""
    from oneops.llm.transport import LiteLLMTransport
    gw_url = os.environ.get("LLM_GATEWAY_URL", "http://localhost:4311")
    api_key = (os.environ.get("LLM_GATEWAY_API_KEY")
               or os.environ.get("LITELLM_MASTER_KEY") or "sk-1234")
    return LlmGateway(
        transport=LiteLLMTransport(base_url=gw_url, api_key=api_key),
        redact=False,
    )


def run_cli(worker_cls: type[BaseEmbeddingWorker]) -> None:
    """Run one service worker as its own process until SIGINT/SIGTERM."""

    async def _amain() -> None:
        _load_env()
        gateway = build_gateway()

        async def _conn() -> asyncpg.Connection:
            return await asyncpg.connect(os.environ["POSTGRES_URL"])

        worker = worker_cls(gateway=gateway, connection_provider=_conn)
        await worker.start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:                    # pragma: no cover
                pass
        print(f"[{worker.SERVICE_ID}] worker draining {worker.queue} — Ctrl-C to stop")
        await stop.wait()
        await worker.stop()

    asyncio.run(_amain())
