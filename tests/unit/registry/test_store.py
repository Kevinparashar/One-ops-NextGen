"""CRUD + version-lifecycle tests for VersionedStore.

Exercises the real FileBackend against a tmp directory — no mock of the
system under test. Each test asserts an observable state change, not mere
truthiness.
"""
from __future__ import annotations

import json

import pytest

from oneops.errors import RecordConflictError, RecordNotFoundError, RecordValidationError
from oneops.registry.models import (
    AbacTags,
    ActivationCondition,
    AgentRecord,
    ConditionOperator,
    ConditionSignal,
    DeterminismLevel,
    ExecutionTier,
    RecordStatus,
    RoutingShape,
)
from oneops.registry.store import FileBackend, VersionedStore


def _cond():
    return ActivationCondition(operator=ConditionOperator.LEAF,
                               signal=ConditionSignal.INTENT_IN, values=("summary",))


def _agent(agent_id="uc01", version=1, owner="team-itsm", desc="Summarise."):
    return AgentRecord(
        id=agent_id, version=version, owner=owner, description=desc,
        intent_family="entity_summary", routing_shape=RoutingShape.SINGLE,
        activation_condition=_cond(), abac_tags=AbacTags(tier=ExecutionTier.READ),
        determinism_level=DeterminismLevel.LOW)


@pytest.fixture()
def store(tmp_path):
    return VersionedStore("agents", AgentRecord, FileBackend(tmp_path))


# ── create ───────────────────────────────────────────────────────────────


def test_create_then_get_specific_version(store):
    store.create(_agent())
    fetched = store.get("uc01", version=1)
    assert fetched.id == "uc01"
    assert fetched.description == "Summarise."


def test_created_record_is_not_active_until_activated(store):
    store.create(_agent())
    with pytest.raises(RecordNotFoundError, match="no active version"):
        store.get("uc01")            # no version arg → active lookup


def test_create_duplicate_id_conflicts(store):
    store.create(_agent())
    with pytest.raises(RecordConflictError, match="already exists"):
        store.create(_agent())


def test_create_must_be_version_one(store):
    with pytest.raises(RecordValidationError, match="must be version 1"):
        store.create(_agent(version=2))


# ── activate / lifecycle ─────────────────────────────────────────────────


def test_activate_makes_record_servable(store):
    store.create(_agent())
    active = store.activate("uc01", 1)
    assert active.status is RecordStatus.ACTIVE
    assert store.get("uc01").version == 1


def test_activate_unknown_version_raises(store):
    store.create(_agent())
    with pytest.raises(RecordNotFoundError, match="no version 7"):
        store.activate("uc01", 7)


def test_retire_active_version_clears_active_pointer(store):
    store.create(_agent())
    store.activate("uc01", 1)
    store.retire("uc01", 1)
    with pytest.raises(RecordNotFoundError, match="no active version"):
        store.get("uc01")


# ── update / versioning ──────────────────────────────────────────────────


def test_update_appends_next_version(store):
    store.create(_agent())
    store.update(_agent(version=2, desc="Summarise — v2."))
    assert store.versions("uc01") == [1, 2]
    assert store.get("uc01", 2).description == "Summarise — v2."
    assert store.get("uc01", 1).description == "Summarise."     # v1 untouched


def test_update_out_of_order_version_conflicts(store):
    store.create(_agent())
    with pytest.raises(RecordConflictError, match="next version must be 2"):
        store.update(_agent(version=3))


def test_update_unknown_record_raises(store):
    with pytest.raises(RecordNotFoundError):
        store.update(_agent(version=2))


def test_activate_newer_then_rollback_to_older(store):
    # Ship v1, ship v2, then roll back to v1 — the rollback primitive.
    store.create(_agent())
    store.activate("uc01", 1)
    store.update(_agent(version=2, desc="v2"))
    store.activate("uc01", 2)
    assert store.get("uc01").version == 2

    rolled = store.activate("uc01", 1)               # rollback
    assert rolled.version == 1
    assert store.get("uc01").description == "Summarise."
    # v2 was demoted to RETIRED, not deleted — history is preserved.
    assert store.get("uc01", 2).status is RecordStatus.RETIRED
    assert store.versions("uc01") == [1, 2]


# ── list / delete ────────────────────────────────────────────────────────


def test_list_active_excludes_draft_and_retired(store):
    store.create(_agent("uc01"))
    store.activate("uc01", 1)
    store.create(_agent("uc02"))                     # left DRAFT
    active_ids = {a.id for a in store.list_active()}
    assert active_ids == {"uc01"}


def test_delete_removes_all_versions(store):
    store.create(_agent())
    store.update(_agent(version=2))
    store.delete("uc01")
    assert store.list_ids() == []
    with pytest.raises(RecordNotFoundError):
        store.get("uc01", 1)


def test_delete_unknown_record_raises(store):
    with pytest.raises(RecordNotFoundError):
        store.delete("does_not_exist")


# ── persistence / corruption ─────────────────────────────────────────────


def test_records_persist_across_store_instances(tmp_path):
    backend = FileBackend(tmp_path)
    s1 = VersionedStore("agents", AgentRecord, backend)
    s1.create(_agent())
    s1.activate("uc01", 1)
    # A fresh store over the same backend sees the persisted record.
    s2 = VersionedStore("agents", AgentRecord, FileBackend(tmp_path))
    assert s2.get("uc01").id == "uc01"


def test_corrupt_file_raises_validation_error(tmp_path, store):
    store.create(_agent())
    corrupt = tmp_path / "agents" / "uc01.json"
    corrupt.write_text("{not json", encoding="utf-8")
    with pytest.raises(RecordValidationError, match="unreadable or corrupt"):
        store.get("uc01", 1)


def test_schema_violating_envelope_raises_on_read(tmp_path, store):
    store.create(_agent())
    bad = tmp_path / "agents" / "uc01.json"
    envelope = json.loads(bad.read_text(encoding="utf-8"))
    envelope["versions"]["1"]["determinism_level"] = "not_a_level"
    bad.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(RecordValidationError, match="fails AgentRecord schema"):
        store.get("uc01", 1)
