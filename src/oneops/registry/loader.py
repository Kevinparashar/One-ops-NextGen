"""Registry loader — build a validated RegistryService at process startup.

`load_registry()` is the single call a service makes to obtain a registry:
it opens the file-backed store, parses + schema-validates every record, runs
the cross-record integrity check, and returns a ready `RegistryService`.

A failure here is fatal by design — a service must not start serving traffic
on an inconsistent registry. The error names every violation so the fix is
one pass, not a guessing game.
"""
from __future__ import annotations

import os
from pathlib import Path

from oneops.errors import ConfigError
from oneops.observability import get_logger
from oneops.registry.service import RegistryService

_log = get_logger("oneops.registry.loader")

# Default location of the declarative registry data (P1 file backend).
DEFAULT_REGISTRY_ROOT = "registries/v2"


def _resolve_root(root: str | None) -> Path:
    if root is None:
        root = os.getenv("REGISTRY_ROOT", DEFAULT_REGISTRY_ROOT)
    path = Path(root)
    if not path.is_absolute():
        # Resolve relative to the repo root (three parents up from this file:
        # registry/ -> oneops/ -> src/ -> repo).
        repo_root = Path(__file__).resolve().parents[3]
        path = repo_root / root
    return path


def load_registry(root: str | None = None, *, check_integrity: bool = True) -> RegistryService:
    """Load and validate the registry.

    Args:
        root: registry data directory. Defaults to env `REGISTRY_ROOT` or
            `registries/v2` relative to the repo root.
        check_integrity: run the cross-record integrity check (default True).
            Only a controlled bootstrap (seeding an empty registry) should
            pass False.

    Raises:
        ConfigError: the registry directory is missing.
        RegistryIntegrityError: a cross-record invariant is violated.
        RecordValidationError: a record fails its schema.
    """
    # Backend selection (2026-06-12). Production runs from the DB — the
    # itsm.agent/tool/uc_schema tables ARE the registry; the JSON files are a
    # dev/authoring convenience synced into them by database/<kind>/sync.py.
    # `ONEOPS_REGISTRY_BACKEND=postgres` makes the disambiguator + every other
    # registry reader source cards from the DB (no JSON on disk required).
    # Default `file` keeps dev/test/CI on the file backend with zero change.
    backend_kind = os.getenv("ONEOPS_REGISTRY_BACKEND", "file").strip().lower()

    if backend_kind in ("postgres", "db", "pg"):
        from oneops.config import get_settings
        from oneops.registry.pg_backend import PostgresBackend

        dsn = get_settings().postgres_url
        service = RegistryService(PostgresBackend(dsn))
        source = f"postgres:{dsn.split('@')[-1].split('/')[0]}"
    else:
        path = _resolve_root(root)
        if not path.is_dir():
            raise ConfigError(
                f"registry root {path} does not exist — seed it before startup "
                "(see database/ sync scripts: agent/tool/uc_schema sync.py)"
            )
        service = RegistryService.from_path(str(path))
        source = str(path)

    # Force a parse of every active record so a schema violation surfaces here,
    # at startup, not on the first request that happens to touch it. This runs
    # identically on either backend — the store reads envelopes through the
    # RegistryBackend Protocol and validates them against the record schema.
    agent_count = len(service.agents.list_active())
    tool_count = len(service.tools.list_active())
    schema_count = len(service.schemas.list_active())

    if check_integrity:
        service.check_integrity()

    _log.info(
        "registry.loaded",
        backend=backend_kind,
        source=source,
        active_agents=agent_count,
        active_tools=tool_count,
        active_schemas=schema_count,
        integrity_checked=check_integrity,
    )
    return service


__all__ = ["load_registry", "DEFAULT_REGISTRY_ROOT"]
