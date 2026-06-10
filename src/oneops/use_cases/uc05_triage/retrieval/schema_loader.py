"""Reads the `retrieval_schema` block from registries/v2/platform/service-schema.json.

Schema-driven dispatch: the engine's SQL is composed from this block instead
of hardcoded templates. Adding a column = JSON edit, not a code change.
The loader is intentionally small and dependency-free so UC-5 isolation
(no cross-UC imports, no heavyweight platform imports) holds.

Cached per-process — the JSON does not mutate at runtime; a process restart
is required to pick up an edit. That matches how every other registry in
the system is consumed.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

# Resolve from this file's location so test runs and prod runs both work.
_DEFAULT_PATH = (
    Path(__file__).resolve().parents[5]
    / "registries" / "v2" / "platform" / "service-schema.json"
)


class RetrievalSchemaError(RuntimeError):
    """Raised when service-schema.json is missing the retrieval_schema block
    for a requested service_id, or the block is malformed. Loud — never
    silently fall back to a hardcoded default (rule §2.7)."""


@lru_cache(maxsize=1)
def _load_all(path_str: str) -> dict[str, dict[str, Any]]:
    path = Path(path_str)
    if not path.exists():
        raise RetrievalSchemaError(f"service-schema.json not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise RetrievalSchemaError(f"service-schema.json invalid: {exc}") from exc

    out: dict[str, dict[str, Any]] = {}
    for svc in data.get("services", []):
        sid = svc.get("service_id")
        rs = svc.get("retrieval_schema")
        if sid and rs:
            out[sid] = rs
    return out


def load_retrieval_schema(
    service_id: str, *, path: Path | None = None
) -> dict[str, Any]:
    """Return the retrieval_schema block for a service_id.

    Raises RetrievalSchemaError loud on any miss — no silent fallbacks.
    """
    target = str((path or _DEFAULT_PATH).resolve())
    all_schemas = _load_all(target)
    if service_id not in all_schemas:
        raise RetrievalSchemaError(
            f"no retrieval_schema for service_id={service_id!r} in {target}. "
            f"Available: {sorted(all_schemas)}"
        )
    schema = all_schemas[service_id]
    _validate(schema, service_id)
    return schema


_REQUIRED_KEYS = (
    "table", "id_column", "embedding_column", "tsv_column",
    "neighbour_columns", "status_filter", "age_filter_days",
    "aggregation_targets",
)


def _validate(schema: dict[str, Any], service_id: str) -> None:
    missing = [k for k in _REQUIRED_KEYS if k not in schema]
    if missing:
        raise RetrievalSchemaError(
            f"retrieval_schema for {service_id!r} missing keys: {missing}"
        )
    if not isinstance(schema["neighbour_columns"], list) or not schema["neighbour_columns"]:
        raise RetrievalSchemaError(
            "retrieval_schema.neighbour_columns must be a non-empty list"
        )
    if not isinstance(schema["status_filter"], list) or not schema["status_filter"]:
        raise RetrievalSchemaError(
            "retrieval_schema.status_filter must be a non-empty list"
        )
    if not isinstance(schema["age_filter_days"], int) or schema["age_filter_days"] <= 0:
        raise RetrievalSchemaError(
            "retrieval_schema.age_filter_days must be a positive int"
        )


def reset_cache() -> None:
    """Test hook — clear the lru_cache so tests can swap schemas."""
    _load_all.cache_clear()
