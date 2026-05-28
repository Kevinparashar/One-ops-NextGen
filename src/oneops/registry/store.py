"""Versioned registry store — CRUD + version lifecycle over a pluggable backend.

Design influences:

  * AgentScript — the store holds *specifications*. Records are versioned;
    an old version stays runnable until explicitly RETIRED; rollback is
    re-activating a prior version. The runtime never edits a record.
  * 5-year horizon — the store talks to a `RegistryBackend` Protocol. The
    file backend here is the honest P1 implementation; a Dragonfly-hot /
    Postgres-cold backend (MIGRATION.md target) drops in without touching
    `VersionedStore` or any caller. Vendor exit is a backend swap.

Concurrency: the file backend writes atomically (temp file + os.replace) and
guards the in-process index with a lock. It is correct for a single process.
The production backend (Postgres) carries the multi-process story; this store's
interface does not change when that lands.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Generic, Iterable, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from oneops.errors import (
    RecordConflictError,
    RecordNotFoundError,
    RecordValidationError,
)
from oneops.registry.models import RecordStatus

RecordT = TypeVar("RecordT", bound=BaseModel)


# ── Backend abstraction ──────────────────────────────────────────────────


class RegistryBackend(Protocol):
    """Persistence contract. The store depends only on this — never on a
    concrete store technology. Implementations: `FileBackend` (P1),
    `PostgresBackend` (later, MIGRATION.md P1 target shape)."""

    def read(self, kind: str, record_id: str) -> dict | None:
        """Return the stored envelope for (kind, id), or None if absent."""
        ...

    def write(self, kind: str, record_id: str, envelope: dict) -> None:
        """Persist the envelope atomically."""
        ...

    def delete(self, kind: str, record_id: str) -> bool:
        """Remove (kind, id). Return True if something was removed."""
        ...

    def list_ids(self, kind: str) -> list[str]:
        """Every record id stored under `kind`."""
        ...


class FileBackend:
    """JSON-file backend. One file per record: `<root>/<kind>/<id>.json`.

    The file holds a *version envelope*:
        {"id": ..., "versions": {"1": {...}, "2": {...}}, "active_version": 2}

    Writes are atomic (temp file in the same dir + `os.replace`), so a crash
    mid-write never leaves a half-written record.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _path(self, kind: str, record_id: str) -> Path:
        return self._root / kind / f"{record_id}.json"

    def read(self, kind: str, record_id: str) -> dict | None:
        path = self._path(kind, record_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecordValidationError(
                f"registry file for {kind}/{record_id} is unreadable or corrupt",
                cause=exc,
            ) from exc

    def write(self, kind: str, record_id: str, envelope: dict) -> None:
        path = self._path(kind, record_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: write to a temp file in the same directory, then replace.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(envelope, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            # Never leave a temp file behind on any failure.
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def delete(self, kind: str, record_id: str) -> bool:
        path = self._path(kind, record_id)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def list_ids(self, kind: str) -> list[str]:
        kind_dir = self._root / kind
        if not kind_dir.is_dir():
            return []
        return sorted(p.stem for p in kind_dir.glob("*.json"))


# ── Versioned store ──────────────────────────────────────────────────────


class VersionedStore(Generic[RecordT]):
    """CRUD + version lifecycle for one record type.

    Version model:
      * `create`  — version 1, status DRAFT.
      * `update`  — appends version N+1 (DRAFT); prior versions untouched.
      * `activate(id, v)` — marks version v ACTIVE, demotes the previously
                            active version to RETIRED. Rollback = activate an
                            older v.
      * `retire(id, v)`   — marks version v RETIRED.
      * `get(id)`         — the ACTIVE version (raises if none active).
      * `get(id, v)`      — a specific version.

    A record's id is immutable. `record_type` enforces the schema on every
    read and write — a corrupt or schema-violating envelope raises rather
    than silently degrading.
    """

    def __init__(self, kind: str, record_type: type[RecordT], backend: RegistryBackend) -> None:
        self._kind = kind
        self._type = record_type
        self._backend = backend
        self._lock = threading.RLock()

    # -- internal envelope helpers ----------------------------------------

    def _load_envelope(self, record_id: str) -> dict | None:
        env = self._backend.read(self._kind, record_id)
        if env is None:
            return None
        if not isinstance(env, dict) or "versions" not in env:
            raise RecordValidationError(
                f"{self._kind}/{record_id}: malformed version envelope"
            )
        return env

    def _parse(self, raw: dict, record_id: str, version: int) -> RecordT:
        try:
            return self._type.model_validate(raw)
        except ValidationError as exc:
            raise RecordValidationError(
                f"{self._kind}/{record_id} v{version} fails {self._type.__name__} schema",
                cause=exc,
            ) from exc

    # -- create / update --------------------------------------------------

    def create(self, record: RecordT) -> RecordT:
        """Persist a brand-new record as version 1 (DRAFT). Conflicts if the
        id already exists."""
        record_id = getattr(record, "id")
        with self._lock:
            if self._load_envelope(record_id) is not None:
                raise RecordConflictError(
                    f"{self._kind}/{record_id} already exists — use update()"
                )
            if getattr(record, "version") != 1:
                raise RecordValidationError(
                    f"{self._kind}/{record_id}: a new record must be version 1, "
                    f"got {getattr(record, 'version')}"
                )
            envelope = {
                "id": record_id,
                "versions": {"1": record.model_dump(mode="json")},
                "active_version": None,    # DRAFT — not active until activate()
            }
            self._backend.write(self._kind, record_id, envelope)
        return record

    def update(self, record: RecordT) -> RecordT:
        """Append the next version. `record.version` must be exactly the
        current max version + 1 — out-of-order writes are a conflict, not a
        silent overwrite."""
        record_id = getattr(record, "id")
        with self._lock:
            envelope = self._load_envelope(record_id)
            if envelope is None:
                raise RecordNotFoundError(f"{self._kind}/{record_id} does not exist")
            existing = {int(v) for v in envelope["versions"]}
            expected = max(existing) + 1
            if getattr(record, "version") != expected:
                raise RecordConflictError(
                    f"{self._kind}/{record_id}: next version must be {expected}, "
                    f"got {getattr(record, 'version')}"
                )
            envelope["versions"][str(expected)] = record.model_dump(mode="json")
            self._backend.write(self._kind, record_id, envelope)
        return record

    # -- lifecycle --------------------------------------------------------

    def activate(self, record_id: str, version: int) -> RecordT:
        """Make `version` the ACTIVE one; demote the prior active to RETIRED.
        This is also the rollback primitive — activate an older version."""
        with self._lock:
            envelope = self._require_envelope(record_id)
            vkey = str(version)
            if vkey not in envelope["versions"]:
                raise RecordNotFoundError(
                    f"{self._kind}/{record_id} has no version {version}"
                )
            prior = envelope.get("active_version")
            if prior is not None and str(prior) != vkey:
                prev = envelope["versions"][str(prior)]
                prev["status"] = RecordStatus.RETIRED.value
            target = envelope["versions"][vkey]
            target["status"] = RecordStatus.ACTIVE.value
            envelope["active_version"] = version
            self._backend.write(self._kind, record_id, envelope)
            return self._parse(target, record_id, version)

    def retire(self, record_id: str, version: int) -> None:
        """Mark a version RETIRED. Retiring the active version clears the
        active pointer — the record then has no servable version."""
        with self._lock:
            envelope = self._require_envelope(record_id)
            vkey = str(version)
            if vkey not in envelope["versions"]:
                raise RecordNotFoundError(
                    f"{self._kind}/{record_id} has no version {version}"
                )
            envelope["versions"][vkey]["status"] = RecordStatus.RETIRED.value
            if envelope.get("active_version") == version:
                envelope["active_version"] = None
            self._backend.write(self._kind, record_id, envelope)

    # -- read -------------------------------------------------------------

    def get(self, record_id: str, version: int | None = None) -> RecordT:
        """Fetch the ACTIVE version, or a specific `version` when given."""
        envelope = self._require_envelope(record_id)
        if version is None:
            active = envelope.get("active_version")
            if active is None:
                raise RecordNotFoundError(
                    f"{self._kind}/{record_id} has no active version "
                    "(still DRAFT or fully retired)"
                )
            version = int(active)
        vkey = str(version)
        if vkey not in envelope["versions"]:
            raise RecordNotFoundError(
                f"{self._kind}/{record_id} has no version {version}"
            )
        return self._parse(envelope["versions"][vkey], record_id, version)

    def get_optional(self, record_id: str, version: int | None = None) -> RecordT | None:
        """Like `get`, but returns None instead of raising when absent."""
        try:
            return self.get(record_id, version)
        except RecordNotFoundError:
            return None

    def versions(self, record_id: str) -> list[int]:
        envelope = self._require_envelope(record_id)
        return sorted(int(v) for v in envelope["versions"])

    def list_ids(self) -> list[str]:
        return self._backend.list_ids(self._kind)

    def list_active(self) -> list[RecordT]:
        """Every record that currently has an ACTIVE version. This is the set
        the router and executor operate over."""
        out: list[RecordT] = []
        for record_id in self.list_ids():
            record = self.get_optional(record_id)
            if record is not None:
                out.append(record)
        return out

    def delete(self, record_id: str) -> None:
        """Hard-delete a record and all its versions. Distinct from `retire`,
        which keeps history. Use only for records created in error."""
        with self._lock:
            if not self._backend.delete(self._kind, record_id):
                raise RecordNotFoundError(f"{self._kind}/{record_id} does not exist")

    # -- helper -----------------------------------------------------------

    def _require_envelope(self, record_id: str) -> dict:
        envelope = self._load_envelope(record_id)
        if envelope is None:
            raise RecordNotFoundError(f"{self._kind}/{record_id} does not exist")
        return envelope


def iter_records(stores: Iterable[VersionedStore]) -> Iterable[BaseModel]:
    """Flatten the active records of several stores — convenience for
    integrity checks and load tests."""
    for store in stores:
        yield from store.list_active()


__all__ = ["RegistryBackend", "FileBackend", "VersionedStore", "iter_records"]
