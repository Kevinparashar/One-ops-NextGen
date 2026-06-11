"""Shared mechanics for the per-service `load_data.py` loaders under database/.

Owns NO per-table knowledge — each service's `load_data.py` declares its own
column spec and calls `load_table(...)`. This keeps the mechanical bits DRY
while leaving every service's schema fully isolated: changing one service's
columns edits only that service's loader, never this file or another service.

Conventions:
  * Connects only via POSTGRES_URL from the repo-root .env (pinned NextGen-ai).
  * Loaders wrap their own `async with conn.transaction()` so a bad row rolls
    the whole load back. `ON CONFLICT DO NOTHING` makes re-runs safe.

Column kinds (the second item of each spec tuple):
  s   plain str        b   bool          i   int
  ts  timestamptz      dt  date
  A   text[]           J[] jsonb array   J{} jsonb object
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import asyncpg

ROOT = Path(__file__).resolve().parents[2]      # database/_lib/_loader.py -> repo root
DATA_DIR = ROOT / "data" / "itsm"


def _ts(v: Any) -> datetime | None:
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


def _dt(v: Any) -> date | None:
    return date.fromisoformat(v) if v else None


# Column-kind → coercion. Data-driven dispatch (one entry per kind) instead
# of an if/elif ladder — adding a kind is a one-line table entry.
_CONVERTERS: dict[str, Callable[[Any], Any]] = {
    "s": lambda v: v,
    "b": lambda v: bool(v) if v is not None else False,
    "i": lambda v: int(v) if v is not None else None,
    "ts": _ts,
    "dt": _dt,
    "A": lambda v: list(v) if v else [],
    "J[]": lambda v: json.dumps(v if v is not None else []),
    "J{}": lambda v: json.dumps(v if v is not None else {}),
}


def convert(value: Any, kind: str) -> Any:
    """Coerce a raw JSON value to the asyncpg-acceptable type for `kind`."""
    try:
        return _CONVERTERS[kind](value)
    except KeyError:
        raise ValueError(f"unknown kind {kind}") from None


def read_env_url() -> str:
    """Parse POSTGRES_URL from the repo-root .env (no os.environ dependency)."""
    m = re.search(r"^POSTGRES_URL=(.+)$", (ROOT / ".env").read_text(), re.M)
    if not m:
        raise SystemExit("POSTGRES_URL not found in .env")
    return m.group(1).strip().strip('"').strip("'")


async def connect(*, timeout: float = 20.0) -> asyncpg.Connection:
    """Open a single asyncpg connection to the pinned NextGen-ai database."""
    return await asyncpg.connect(dsn=read_env_url(), timeout=timeout)


async def load_table(
    conn: asyncpg.Connection,
    table: str,
    spec: list[tuple[str, str]],
    *,
    data_dir: Path = DATA_DIR,
    conflict_cols: list[str] | None = None,
    update_cols: list[str] | None = None,
    validate: Callable[[list[dict[str, Any]]], None] | None = None,
) -> int:
    """Insert every row of data_dir/<table>.json into itsm.<table>.

    Default: idempotent insert (`ON CONFLICT DO NOTHING`) — existing rows are
    left untouched. The caller owns the transaction. Returns the number of rows
    submitted. Raises if the JSON file is missing (no silent skips — rule §2.7).

    Upsert mode (opt-in): pass `conflict_cols` (the conflict target, usually the
    PK) AND `update_cols` (the non-key columns to refresh) to switch to
    `ON CONFLICT (conflict_cols) DO UPDATE SET col = EXCLUDED.col ...`. This keeps
    existing rows' identity (FK-safe — no delete) while refreshing their data.

    Validation (opt-in): pass `validate` to run a service-specific invariant
    check on the parsed rows BEFORE any row is written. It must raise on a
    violation — the caller's transaction then rolls back, so a malformed file
    fails the load loudly instead of seeding bad data (rule §2.7).
    """
    rows = json.loads((data_dir / f"{table}.json").read_text())
    if validate is not None:
        validate(rows)
    cols = [c for c, _ in spec]
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    if conflict_cols and update_cols:
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        conflict = ", ".join(conflict_cols)
        on_conflict = f"ON CONFLICT ({conflict}) DO UPDATE SET {set_clause}"
    else:
        on_conflict = "ON CONFLICT DO NOTHING"
    sql = (
        f"INSERT INTO itsm.{table} ({', '.join(cols)}) "
        f"VALUES ({placeholders}) {on_conflict}"
    )
    values = [tuple(convert(r.get(c), k) for c, k in spec) for r in rows]
    await conn.executemany(sql, values)
    return len(values)


async def count(conn: asyncpg.Connection, table: str) -> int:
    return await conn.fetchval(f"SELECT count(*) FROM itsm.{table}")
