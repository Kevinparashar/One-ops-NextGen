"""PostgresBackend — the DB implementation of the `RegistryBackend` Protocol.

In production the registry source of truth is the database (`itsm.agent` /
`itsm.tool` / `itsm.uc_schema`), not the `registries/v2/*.json` files — the
files are a dev/authoring convenience that `database/<kind>/sync.py` pushes
into those tables. This backend lets `RegistryService` (and therefore the
Stage-4 disambiguator, the executor allowlist, retrieval seeding — everything)
read the cards from the DB so the service runs with no JSON on disk.

Design — read-once, serve-from-memory:
  * The registry is IMMUTABLE for a process's lifetime (cf.
    `RegistryService.routing_fingerprint`, which memoizes on that basis). So
    this backend loads EVERY row ONCE at construction into an in-memory map and
    serves `read` / `list_ids` from it. A DB round-trip never touches the hot
    routing path — the disambiguator's per-call `_scope` / `_describe` reads hit
    the in-memory store exactly as they did under `FileBackend`.
  * The DB stores ONE ROW PER (id, version) with `body` = that version's card
    (verbatim from `versions[n]` in the file). The `RegistryBackend` contract
    returns the *envelope* shape `{"id", "versions": {n: body}, "active_version"}`,
    so this backend REASSEMBLES the envelope from the per-version rows, taking
    `active_version` from the row whose `status='active'`.
  * Writes are NOT served in-app: in DB mode the authoring path is
    `database/<kind>/sync.py` (offline, hash-gated, re-embeds on change). A
    runtime `write` / `delete` raises — the running service is a reader, never
    a writer, so a silent dual-write can't drift from the sync pipeline.

Wiring: `registry.loader.load_registry` selects this backend when
`ONEOPS_REGISTRY_BACKEND=postgres` (default `file`); production sets it.
"""
from __future__ import annotations

from oneops.observability import get_logger

_log = get_logger("oneops.registry.pg_backend")

# kind (RegistryService vocabulary) → (table, id column)
_TABLES: dict[str, tuple[str, str]] = {
    "agents": ("itsm.agent", "agent_id"),
    "tools": ("itsm.tool", "tool_id"),
    "schemas": ("itsm.uc_schema", "schema_id"),
}


class PostgresBackend:
    """Read-once, in-memory `RegistryBackend` over the itsm.* registry tables.

    Construction performs a single synchronous load of all three kinds (it runs
    at app startup, before the event loop owns the request path). Thereafter
    every `read` / `list_ids` is an in-memory dict lookup.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        # kind -> {record_id -> reassembled version envelope}
        self._cache: dict[str, dict[str, dict]] = {k: {} for k in _TABLES}
        self._load()

    # -- boot-time load ----------------------------------------------------

    def _load(self) -> None:
        import os
        import time

        import psycopg

        # Fail-FAST + bounded retry. Without an explicit connect_timeout, a
        # saturated pooler makes psycopg.connect block FOREVER, hanging boot
        # silently at "Waiting for application startup". A short timeout turns
        # that into a clear, retryable error; a few backoff retries ride out a
        # transient pool saturation, then the boot fails LOUDLY (never hangs).
        timeout = int(os.getenv("ONEOPS_REGISTRY_DB_CONNECT_TIMEOUT", "8"))
        attempts = int(os.getenv("ONEOPS_REGISTRY_DB_CONNECT_RETRIES", "4"))
        conn = None
        for n in range(1, attempts + 1):
            try:
                conn = psycopg.connect(self._dsn, connect_timeout=timeout)
                break
            except Exception as exc:                                 # noqa: BLE001
                _log.warning("registry.pg_backend.connect_retry",
                             attempt=n, of=attempts, error=str(exc)[:160])
                if n == attempts:
                    raise
                time.sleep(min(2 * n, 8))
        with conn, conn.cursor() as cur:
            for kind, (table, id_col) in _TABLES.items():
                cur.execute(
                    f"SELECT {id_col}, version, status, body "          # noqa: S608
                    f"FROM {table} ORDER BY {id_col}, version"
                )
                by_id: dict[str, dict] = {}
                for record_id, version, status, body in cur.fetchall():
                    env = by_id.get(record_id)
                    if env is None:
                        env = {"id": record_id, "versions": {},
                               "active_version": None}
                        by_id[record_id] = env
                    # psycopg3 returns jsonb as a parsed dict already.
                    env["versions"][str(version)] = body
                    if status == "active":
                        env["active_version"] = version
                self._cache[kind] = by_id
        _log.info(
            "registry.pg_backend.loaded",
            agents=len(self._cache["agents"]),
            tools=len(self._cache["tools"]),
            schemas=len(self._cache["schemas"]),
        )

    # -- RegistryBackend Protocol -----------------------------------------

    def read(self, kind: str, record_id: str) -> dict | None:
        return self._cache.get(kind, {}).get(record_id)

    def list_ids(self, kind: str) -> list[str]:
        return list(self._cache.get(kind, {}).keys())

    def write(self, kind: str, record_id: str, envelope: dict) -> None:
        raise NotImplementedError(
            "registry is read-only in DB mode — author cards via "
            "database/<kind>/sync.py (files → itsm.* → re-embed), not at runtime"
        )

    def delete(self, kind: str, record_id: str) -> bool:
        raise NotImplementedError(
            "registry is read-only in DB mode — retire cards via "
            "database/<kind>/sync.py --retire-missing, not at runtime"
        )

    def reload(self) -> None:
        """Re-read every kind from the DB (e.g. after an out-of-band sync).
        Not on any hot path — an operator/admin affordance."""
        self._cache = {k: {} for k in _TABLES}
        self._load()


__all__: list[str] = ["PostgresBackend"]
