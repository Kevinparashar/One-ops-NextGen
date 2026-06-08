"""Contract test for the shared embedding-worker poll/ack loop.

Locks in BaseEmbeddingWorker's failure semantics after the S3516 refactor that
dropped the vestigial `bool` return:

  * process() returns normally  → the message is acked (pgmq.delete called).
  * process() raises            → the message is NOT acked (left for the
    visibility-timeout retry); the exception is swallowed by the loop so one
    poisoned message can't kill the drain.

These workers had no coverage before; this test guards the queue contract.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
for _p in (_ROOT / "src", _ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from _lib._worker_base import BaseEmbeddingWorker  # noqa: E402

pytestmark = pytest.mark.unit

_MSG = {"entity_id": "INC1", "tenant_id": "T001", "chunk_type": "symptom_anchor"}


class _FakeConn:
    """Minimal asyncpg-conn stand-in. Yields one queued message on read and
    records every pgmq.delete so the test can assert ack behaviour."""

    def __init__(self) -> None:
        self.deleted: list[int] = []
        self.closed = False

    async def fetch(self, _sql: str, *_args: object) -> list[dict[str, object]]:
        return [{"msg_id": 7, "body": json.dumps(_MSG)}]

    async def execute(self, sql: str, *args: object) -> str:
        if "pgmq.delete" in sql:
            self.deleted.append(args[1])
        return "OK"

    async def close(self) -> None:
        self.closed = True


def _make_worker(process_impl):
    conn = _FakeConn()

    class _W(BaseEmbeddingWorker):
        SERVICE_ID = "test"
        TARGET_TABLE = "ai.embeddings_test"

        async def process(self, conn, body):  # type: ignore[override]
            return await process_impl(conn, body)

    async def _provider() -> _FakeConn:
        return conn

    return _W(gateway=object(), connection_provider=_provider), conn


class TestDrainContract:
    async def test_normal_return_acks_message(self) -> None:
        async def _ok(_conn, _body):
            return None

        worker, conn = _make_worker(_ok)
        await worker._drain_once()
        assert conn.deleted == [7], "message acked when process() returns normally"
        assert conn.closed is True

    async def test_raise_does_not_ack(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Silence the loop's failure metric so the test needs no live meter.
        import _lib._worker_base as wb
        monkeypatch.setattr(wb, "_metric_inc", lambda *a, **k: None)

        async def _boom(_conn, _body):
            raise RuntimeError("transient embed failure")

        worker, conn = _make_worker(_boom)
        await worker._drain_once()  # must NOT propagate
        assert conn.deleted == [], "message left for retry when process() raises"
