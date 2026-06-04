"""Shared pgvector HNSW hardening helper — unit coverage.

The helper either runs three SETs successfully, or — on older pgvector that
doesn't recognise the GUCs — logs a one-line warning and continues. The
caller never sees an exception.
"""
from __future__ import annotations

import pytest

from oneops.db import pgvector_hnsw


class _FakeConn:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.executed: list[str] = []
        self._fail_on = fail_on

    async def execute(self, sql: str) -> None:
        self.executed.append(sql)
        if self._fail_on and self._fail_on in sql:
            raise RuntimeError(f"pgvector does not recognise {self._fail_on!r}")


@pytest.fixture(autouse=True)
def _reset_log_flag():
    """The helper logs at most once per process; reset for test isolation."""
    pgvector_hnsw._LOGGED = False
    yield
    pgvector_hnsw._LOGGED = False


@pytest.mark.asyncio
async def test_applies_all_three_settings_on_modern_pgvector():
    conn = _FakeConn()
    await pgvector_hnsw.apply_hardening(conn)
    assert len(conn.executed) == 3
    assert any("iterative_scan" in s for s in conn.executed)
    assert any("max_scan_tuples" in s for s in conn.executed)
    assert any("ef_search" in s for s in conn.executed)


@pytest.mark.asyncio
async def test_iterative_scan_is_relaxed_order_by_default():
    conn = _FakeConn()
    await pgvector_hnsw.apply_hardening(conn)
    assert any("relaxed_order" in s for s in conn.executed)


@pytest.mark.asyncio
async def test_swallows_old_pgvector_failure_without_raising():
    """pgvector < 0.8 throws on these GUCs. The helper must not propagate."""
    conn = _FakeConn(fail_on="iterative_scan")
    # Must not raise
    await pgvector_hnsw.apply_hardening(conn)
    # The first failed SET is still attempted
    assert any("iterative_scan" in s for s in conn.executed)


@pytest.mark.asyncio
async def test_call_is_idempotent_across_connections():
    """Multiple connections should each get hardened independently."""
    for _ in range(3):
        conn = _FakeConn()
        await pgvector_hnsw.apply_hardening(conn)
        assert len(conn.executed) == 3


@pytest.mark.asyncio
async def test_env_overrides_respected(monkeypatch):
    """Env knobs must reach the SQL. We re-import to pick up new env."""
    monkeypatch.setenv("ONEOPS_HNSW_ITERATIVE_SCAN", "strict_order")
    monkeypatch.setenv("ONEOPS_HNSW_MAX_SCAN_TUPLES", "5000")
    monkeypatch.setenv("ONEOPS_HNSW_EF_SEARCH", "200")
    import importlib
    importlib.reload(pgvector_hnsw)
    conn = _FakeConn()
    await pgvector_hnsw.apply_hardening(conn)
    joined = " ".join(conn.executed)
    assert "strict_order" in joined
    assert "5000" in joined
    assert "200" in joined
    # Restore the original module state for subsequent tests
    importlib.reload(pgvector_hnsw)
