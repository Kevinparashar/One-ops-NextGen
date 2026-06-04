"""pgvector HNSW filter hardening — shared helper for every UC that does
filtered vector search.

The problem
-----------
pgvector applies WHERE predicates DURING the HNSW walk, not before. With the
default `hnsw.ef_search = 40`, a query like

    WHERE tenant_id = $1 AND status = ANY(...) AND created_at > ...
    ORDER BY embedding <=> $vec
    LIMIT $k

can finish the walk after only inspecting 40 neighbours and return < k rows
even when the index contains plenty of matching ones — because the walk
ordered them by raw distance, not by predicate-survival. Small tenants or
narrow status filters hit this silently.

pgvector 0.8 fixed this with two GUCs:

  • `hnsw.iterative_scan` — `off` (default), `relaxed_order` (fast, may yield
    slightly out-of-order results which we re-sort in Python anyway), or
    `strict_order` (slowest, in-order).
  • `hnsw.max_scan_tuples` — cap the walk so a pathological query can't
    OOM the planner.

Usage
-----
Call once per asyncpg connection BEFORE running any filtered vector query:

    from oneops.db.pgvector_hnsw import apply_hardening
    conn = await asyncpg.connect(...)
    await apply_hardening(conn)
    # ... your filtered ANN query here ...

Settings persist for the lifetime of the connection. Cheap (~1 ms total).

Env knobs
---------
  ONEOPS_HNSW_ITERATIVE_SCAN     (default: relaxed_order)
  ONEOPS_HNSW_MAX_SCAN_TUPLES    (default: 20000)
  ONEOPS_HNSW_EF_SEARCH          (default: 100)

References
----------
  • https://www.postgresql.org/about/news/pgvector-080-released-2952/
  • https://aws.amazon.com/blogs/database/supercharging-vector-search-performance
"""
from __future__ import annotations

import os
from typing import Any

from oneops.observability import get_logger

_log = get_logger("oneops.db.pgvector_hnsw")

_ITER_MODE = os.getenv("ONEOPS_HNSW_ITERATIVE_SCAN", "relaxed_order")
_MAX_SCAN_TUPLES = int(os.getenv("ONEOPS_HNSW_MAX_SCAN_TUPLES", "20000"))
_EF_SEARCH = int(os.getenv("ONEOPS_HNSW_EF_SEARCH", "100"))

# Once-per-process: log the chosen settings the first time a connection is
# hardened, so operators can confirm what's in effect by `grep`-ing the boot
# log. After that, hardening is silent.
_LOGGED = False


async def apply_hardening(conn: Any) -> None:
    """Apply HNSW filter-hardening to a single asyncpg connection.

    Safe on any pgvector version; older pgvector that doesn't recognise the
    settings will throw, we log a one-line warning, and the caller continues
    on the legacy path (slightly higher under-recall risk on small tenants).

    The first successful call per process emits an `oneops.db.pgvector_hardened`
    INFO log so operators can confirm the settings landed. Subsequent calls
    are silent — they still run the SET statements but don't log.
    """
    global _LOGGED
    try:
        await conn.execute(f"SET hnsw.iterative_scan = '{_ITER_MODE}'")
        await conn.execute(f"SET hnsw.max_scan_tuples = {_MAX_SCAN_TUPLES}")
        await conn.execute(f"SET hnsw.ef_search = {_EF_SEARCH}")
    except Exception as exc:                                       # noqa: BLE001
        # pgvector < 0.8 throws on these GUCs. Don't fail the call — just
        # log once so an operator sees the warning during boot/CI.
        if not _LOGGED:
            _log.warning(
                "oneops.db.pgvector_hardening_unavailable",
                error=str(exc)[:160],
                hint="upgrade pgvector to 0.8+ to enable filter-hardening",
            )
            _LOGGED = True
        return
    if not _LOGGED:
        _log.info(
            "oneops.db.pgvector_hardened",
            iterative_scan=_ITER_MODE,
            max_scan_tuples=_MAX_SCAN_TUPLES,
            ef_search=_EF_SEARCH,
        )
        _LOGGED = True


__all__ = ["apply_hardening"]
