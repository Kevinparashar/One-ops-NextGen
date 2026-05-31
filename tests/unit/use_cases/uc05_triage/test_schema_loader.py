"""Unit tests for the retrieval schema loader."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from oneops.use_cases.uc05_triage.retrieval.schema_loader import (
    RetrievalSchemaError,
    load_retrieval_schema,
    reset_cache,
)


def _write_schema(tmp: Path, services: list[dict]) -> Path:
    p = tmp / "service-schema.json"
    p.write_text(json.dumps({"services": services}))
    reset_cache()
    return p


def _valid_block() -> dict:
    return {
        "table": "itsm.incident",
        "id_column": "incident_id",
        "embedding_column": "embedding",
        "tsv_column": "search_tsv",
        "neighbour_columns": ["title", "description", "category"],
        "status_filter": ["open"],
        "age_filter_days": 30,
        "aggregation_targets": ["category"],
    }


def test_loads_real_service_schema_for_incident() -> None:
    reset_cache()
    schema = load_retrieval_schema("incident")
    assert schema["table"] == "itsm.incident"
    assert "title" in schema["neighbour_columns"]
    assert "category" in schema["aggregation_targets"]


def test_loads_real_service_schema_for_request() -> None:
    reset_cache()
    schema = load_retrieval_schema("request")
    assert schema["table"] == "itsm.request"
    # Spec-aligned 2026-05-29 PM: aggregation_targets = [category, assigned_to, ci_id]
    assert "assigned_to" in schema["aggregation_targets"]
    assert "ci_id" in schema["aggregation_targets"]
    assert "catalog_item_id" not in schema["aggregation_targets"]


def test_unknown_service_raises_loud(tmp_path: Path) -> None:
    p = _write_schema(tmp_path, [
        {"service_id": "incident", "retrieval_schema": _valid_block()},
    ])
    with pytest.raises(RetrievalSchemaError, match="no retrieval_schema for"):
        load_retrieval_schema("problem", path=p)


def test_missing_required_key_raises(tmp_path: Path) -> None:
    bad = _valid_block()
    del bad["status_filter"]
    p = _write_schema(tmp_path, [
        {"service_id": "incident", "retrieval_schema": bad},
    ])
    with pytest.raises(RetrievalSchemaError, match="missing keys"):
        load_retrieval_schema("incident", path=p)


def test_empty_neighbour_columns_raises(tmp_path: Path) -> None:
    bad = _valid_block() | {"neighbour_columns": []}
    p = _write_schema(tmp_path, [
        {"service_id": "incident", "retrieval_schema": bad},
    ])
    with pytest.raises(RetrievalSchemaError, match="non-empty list"):
        load_retrieval_schema("incident", path=p)


def test_invalid_age_filter_raises(tmp_path: Path) -> None:
    bad = _valid_block() | {"age_filter_days": 0}
    p = _write_schema(tmp_path, [
        {"service_id": "incident", "retrieval_schema": bad},
    ])
    with pytest.raises(RetrievalSchemaError, match="positive int"):
        load_retrieval_schema("incident", path=p)


def test_missing_file_raises(tmp_path: Path) -> None:
    reset_cache()
    with pytest.raises(RetrievalSchemaError, match="not found"):
        load_retrieval_schema("incident", path=tmp_path / "missing.json")


def test_dynamic_new_column_flows_through(tmp_path: Path) -> None:
    """Adding a column to JSON should not require code change."""
    block = _valid_block() | {
        "neighbour_columns": ["title", "description", "category",
                              "future_field_we_dont_know_yet"],
    }
    p = _write_schema(tmp_path, [
        {"service_id": "incident", "retrieval_schema": block},
    ])
    schema = load_retrieval_schema("incident", path=p)
    assert "future_field_we_dont_know_yet" in schema["neighbour_columns"]


def test_reset_cache_after_real_load() -> None:
    """Cleanup so other tests get a fresh cache state."""
    reset_cache()
    schema = load_retrieval_schema("incident")
    assert schema is not None
    reset_cache()
