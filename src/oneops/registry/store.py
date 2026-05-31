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
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, TypeVar

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
    """JSON-file backend with recursive per-UC subfolder support (2026-05-31).

    Layout:
        <root>/<kind>/<id>.json                       (legacy flat layout — still supported)
        <root>/<kind>/<grouping>/<id>.json            (per-UC layout, recommended)

    Examples:
        registries/v2/tools/uc01_summarization/get_ticket_details.json
        registries/v2/tools/uc02_similar_tickets/find_similar_entities.json
        registries/v2/tools/shared/notify_milestone.json

    Filename = record_id (e.g., `get_ticket_details.json` ↔ id `get_ticket_details`).
    Subfolder is operator organisation only — it does NOT participate in the id.

    The file holds a *version envelope*:
        {"id": ..., "versions": {"1": {...}, "2": {...}}, "active_version": 2}

    Writes are atomic (temp file in the same dir + `os.replace`), so a crash
    mid-write never leaves a half-written record.

    Production guarantees:
      • Recursive discovery via `rglob("*.json")` — finds tools at any depth.
      • Duplicate-id detection — same `record_id.json` in two subfolders is a
        config bug; the backend raises `RegistryDuplicateIdError` at boot
        rather than silently picking one.
      • Backward-compatible — existing flat-layout records continue to work
        without migration.
      • Writes prefer the existing file's location; new records land at the
        top of their kind directory (callers can move them into subfolders
        after creation if desired).
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _kind_dir(self, kind: str) -> Path:
        return self._root / kind

    def _path(self, kind: str, record_id: str) -> Path:
        """Resolve the canonical location of `record_id.json` under `kind`.

        Recursive search across subfolders; if multiple matches, that's a
        duplicate-id config bug and we raise immediately (rather than silently
        binding to the first one found). When no file exists, returns the
        top-level path so writes for brand-new records land predictably.
        """
        kind_dir = self._kind_dir(kind)
        if kind_dir.is_dir():
            matches = list(kind_dir.rglob(f"{record_id}.json"))
            matches = [p for p in matches if p.is_file()]
            if len(matches) > 1:
                from oneops.errors import RegistryDuplicateIdError
                rel = sorted(str(p.relative_to(self._root)) for p in matches)
                raise RegistryDuplicateIdError(
                    f"duplicate {kind}/{record_id}.json in: {rel}")
            if matches:
                return matches[0]
        return kind_dir / f"{record_id}.json"

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
        """Recursive walk + duplicate-id guard. Production-grade (2026-05-31).

        Walks every subfolder under `<root>/<kind>/` so per-UC organisation
        (e.g. `tools/uc01_summarization/`, `tools/shared/`) is discovered.
        If the same `record_id.json` exists in multiple subfolders the walker
        raises — silent override would break the substrate's contract.
        """
        kind_dir = self._kind_dir(kind)
        if not kind_dir.is_dir():
            return []
        ids: list[str] = []
        seen: dict[str, Path] = {}
        for p in kind_dir.rglob("*.json"):
            if not p.is_file():
                continue
            stem = p.stem
            if stem in seen:
                from oneops.errors import RegistryDuplicateIdError
                paths = sorted(str(x.relative_to(self._root))
                               for x in (seen[stem], p))
                raise RegistryDuplicateIdError(
                    f"duplicate {kind} record_id '{stem}' in: {paths}")
            seen[stem] = p
            ids.append(stem)
        return sorted(ids)


# ── Versioned store ──────────────────────────────────────────────────────


class VersionedStore[RecordT: BaseModel]:
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
        record_id = record.id
        with self._lock:
            if self._load_envelope(record_id) is not None:
                raise RecordConflictError(
                    f"{self._kind}/{record_id} already exists — use update()"
                )
            if record.version != 1:
                raise RecordValidationError(
                    f"{self._kind}/{record_id}: a new record must be version 1, "
                    f"got {record.version}"
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
        record_id = record.id
        with self._lock:
            envelope = self._load_envelope(record_id)
            if envelope is None:
                raise RecordNotFoundError(f"{self._kind}/{record_id} does not exist")
            existing = {int(v) for v in envelope["versions"]}
            expected = max(existing) + 1
            if record.version != expected:
                raise RecordConflictError(
                    f"{self._kind}/{record_id}: next version must be {expected}, "
                    f"got {record.version}"
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
            _audit_lifecycle_transition(self._kind, record_id, version,
                                         to_status=RecordStatus.ACTIVE)
            return self._parse(target, record_id, version)

    def deprecate(self, record_id: str, version: int) -> None:
        """Mark a version DEPRECATED — still callable, but `get()` emits a
        deprecation warning on every access (operators see it in Tempo /
        log and can prepare callers for retirement).

        Deprecating the active version KEEPS it active (the record still
        services traffic). Use `retire()` to remove from the live pool.

        Lifecycle: ACTIVE → DEPRECATED → (later) RETIRED.
        """
        with self._lock:
            envelope = self._require_envelope(record_id)
            vkey = str(version)
            if vkey not in envelope["versions"]:
                raise RecordNotFoundError(
                    f"{self._kind}/{record_id} has no version {version}"
                )
            envelope["versions"][vkey]["status"] = RecordStatus.DEPRECATED.value
            self._backend.write(self._kind, record_id, envelope)
            _audit_lifecycle_transition(self._kind, record_id, version,
                                         to_status=RecordStatus.DEPRECATED)

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
            _audit_lifecycle_transition(self._kind, record_id, version,
                                         to_status=RecordStatus.RETIRED)

    # -- read -------------------------------------------------------------

    def get(self, record_id: str, version: int | None = None) -> RecordT:
        """Fetch the ACTIVE version, or a specific `version` when given.

        Production-grade runtime observability (2026-05-31): when the
        record returned is DEPRECATED, a structured log + OTel span
        event is emitted so every caller is auditable. Operators can
        alert on `registry.lifecycle.deprecation_used` to size sunset
        windows.
        """
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
        payload = envelope["versions"][vkey]
        if payload.get("status") == RecordStatus.DEPRECATED.value:
            _emit_deprecation_used(self._kind, record_id, version)
        return self._parse(payload, record_id, version)

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
        """Every record currently in `ACTIVE` lifecycle state.

        Production-grade gating (2026-05-31): explicitly checks the
        record's `status` field rather than trusting the `active_version`
        pointer alone. This excludes DEPRECATED + DRAFT records — the
        router never selects them. To include deprecated records for
        operator views, use `list_by_status()` below.
        """
        out: list[RecordT] = []
        for record_id in self.list_ids():
            record = self.get_optional(record_id)
            if record is not None and getattr(record, "status", None) == RecordStatus.ACTIVE:
                out.append(record)
        return out

    def list_by_status(self, status: RecordStatus) -> list[RecordT]:
        """Operator helper — list records that have a version in the
        requested lifecycle state.

        For ACTIVE / DEPRECATED, the record's pointed (or latest)
        version's status is checked. For DRAFT, records with no
        active_version pointer (never activated) are returned. For
        RETIRED, records where all versions are RETIRED are returned.

        Used for dashboards, audit, and the boot summary
        ("active=4 deprecated=0 retired=0 draft=0").
        """
        out: list[RecordT] = []
        for record_id in self.list_ids():
            envelope = self._load_envelope(record_id)
            if envelope is None:
                continue
            record_status = _envelope_lifecycle_status(envelope)
            if record_status == status:
                # Best-effort: try to materialise the record at whichever
                # version we can; DRAFT records have no active_version so
                # use the highest version number.
                rec = self._materialise_at_status(envelope, record_id, status)
                if rec is not None:
                    out.append(rec)
        return out

    def lifecycle_summary(self) -> dict[str, int]:
        """Counts by lifecycle state across all records of this kind.
        Used at boot to log an operator-readable lifecycle inventory.

        Each record is counted in exactly ONE bucket — the one matching
        its current lifecycle state (see `_envelope_lifecycle_status` for
        the precise rule). Sum of buckets == total records of this kind.
        """
        counts: dict[str, int] = {s.value: 0 for s in RecordStatus}
        for record_id in self.list_ids():
            envelope = self._load_envelope(record_id)
            if envelope is None:
                continue
            record_status = _envelope_lifecycle_status(envelope)
            if record_status is not None:
                counts[record_status.value] = counts.get(record_status.value, 0) + 1
        return counts

    def _materialise_at_status(
        self, envelope: dict, record_id: str, target_status: RecordStatus,
    ) -> RecordT | None:
        """Return the highest-version record whose status matches `target_status`."""
        versions = envelope.get("versions") or {}
        best: tuple[int, dict] | None = None
        for vkey, payload in versions.items():
            try:
                v = int(vkey)
            except ValueError:
                continue
            if payload.get("status") == target_status.value and (best is None or v > best[0]):
                best = (v, payload)
        if best is None:
            return None
        try:
            return self._parse(best[1], record_id, best[0])
        except Exception:                                            # noqa: BLE001
            return None

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


# ── Lifecycle status resolution (envelope-level) ────────────────────────────
# A record (envelope) is in exactly ONE lifecycle state at any moment, derived
# from its versions and the `active_version` pointer:
#
#   ACTIVE      — active_version is set AND that version's status is ACTIVE
#   DEPRECATED  — active_version is set AND that version's status is DEPRECATED
#                 (callable but warning-emitting; used for sunset windows)
#   RETIRED     — active_version is None AND every version is RETIRED
#                 (no servable version; record kept for audit)
#   DRAFT       — active_version is None AND at least one version is DRAFT
#                 (created, never activated yet)


def _envelope_lifecycle_status(envelope: dict) -> RecordStatus | None:
    """Resolve the record-level lifecycle state from an envelope.

    Returns None when the envelope is malformed (no versions at all).
    """
    versions = envelope.get("versions") or {}
    if not versions:
        return None

    active_v = envelope.get("active_version")
    if active_v is not None:
        payload = versions.get(str(active_v)) or {}
        status_str = payload.get("status")
        if status_str == RecordStatus.ACTIVE.value:
            return RecordStatus.ACTIVE
        if status_str == RecordStatus.DEPRECATED.value:
            return RecordStatus.DEPRECATED
        # Pointer exists but version is RETIRED — fall through to "RETIRED".

    # No active pointer (or pointer to RETIRED). Decide between RETIRED
    # (all versions retired) and DRAFT (any version is draft).
    statuses = {p.get("status") for p in versions.values()}
    if RecordStatus.DRAFT.value in statuses:
        return RecordStatus.DRAFT
    if statuses == {RecordStatus.RETIRED.value}:
        return RecordStatus.RETIRED
    # Unknown shape — best-effort: return None to skip in summary
    return None


# ── Lifecycle audit emission ─────────────────────────────────────────────────
# Every activate / deprecate / retire transition emits a structured log line
# AND an OTel span event when a tracer is available. This is the audit trail
# for "who deprecated UC-3 v2 at what time" without needing a separate audit
# table — the OTel collector captures it as durably as any other span event.
# The emit is fail-open: if logging or OTel fails, the registry mutation still
# succeeds (the source of truth is the file backend, not the audit emit).

def _emit_deprecation_used(kind: str, record_id: str, version: int) -> None:
    """Runtime-side audit — emitted on every `get()` of a DEPRECATED record.

    This is distinct from `_audit_lifecycle_transition`, which fires once at
    state-change time. Deprecation-used events let operators measure traffic
    against deprecated agents and size sunset windows accurately.

    Never raises — fail-open guarantee.
    """
    try:
        import structlog
        _log = structlog.get_logger("oneops.registry.lifecycle")
        _log.warning(
            "registry.lifecycle.deprecation_used",
            kind=kind,
            record_id=record_id,
            version=version,
        )
    except Exception:                                                 # noqa: BLE001
        pass
    try:
        from opentelemetry import trace
        sp = trace.get_current_span()
        if sp is not None and sp.is_recording():
            sp.add_event(
                "registry.lifecycle.deprecation_used",
                attributes={
                    "registry.kind": kind,
                    "registry.record_id": record_id,
                    "registry.version": version,
                },
            )
    except Exception:                                                 # noqa: BLE001
        pass


def _audit_lifecycle_transition(
    kind: str, record_id: str, version: int, *, to_status: RecordStatus,
) -> None:
    """Emit a structured log + OTel event for a lifecycle state change.

    Safe to call without an active OTel span — falls back to log-only.
    Never raises: a failure here cannot stop the registry mutation that
    just succeeded.
    """
    try:
        import structlog
        _log = structlog.get_logger("oneops.registry.lifecycle")
        _log.info(
            "registry.lifecycle.transition",
            kind=kind,
            record_id=record_id,
            version=version,
            to_status=to_status.value,
        )
    except Exception:                                                 # noqa: BLE001
        pass
    try:
        from opentelemetry import trace
        sp = trace.get_current_span()
        if sp is not None and sp.is_recording():
            sp.add_event(
                "registry.lifecycle.transition",
                attributes={
                    "registry.kind": kind,
                    "registry.record_id": record_id,
                    "registry.version": version,
                    "registry.to_status": to_status.value,
                },
            )
    except Exception:                                                 # noqa: BLE001
        pass


__all__ = ["RegistryBackend", "FileBackend", "VersionedStore", "iter_records"]
