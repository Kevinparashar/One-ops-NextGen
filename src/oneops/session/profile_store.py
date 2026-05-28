"""User / tenant long-term profile store — substrate gap G1.

Per-user durable context that survives session closure: preferences,
behavior counters, historical summaries, opted-in metadata. Distinct from:

  * `conversation_history` (per-turn, in-state) — bounded by [[memory.py]].
  * Session events (`SessionEventStore`) — short-window, per-session, lossy
    beyond the retention window.

A profile entry is **the place a UC reads when it needs "what is generally
true of this user across sessions"** — e.g. "how many times has this user
escalated this quarter?", "which language do they prefer?", "what is their
default assignment group?".

Contract:

  * **Tenant-partitioned.** Every read and write takes `tenant_id` from the
    request envelope. A row is keyed by `(tenant_id, user_id)` — a row for
    user-X in tenant-A is structurally invisible to tenant-B.
  * **Structured, not free-form.** A profile is a typed dict whose keys are
    declared by UC packs. The store does not parse or validate the keys
    (that would create a per-UC catalogue in the store — Component Spec
    C12 violation). Validation lives in the UC handlers.
  * **Pluggable backend** ([[feedback_poc5mw_no_db_no_docker]]) — in-memory
    default, Dragonfly stub for FaaS. Selected by `ONEOPS_PROFILE_BACKEND`.
  * **Cold-start safe.** No I/O at import; lazy singleton; the live backend
    is selected by env at first use.
  * **No silent failure.** Reads of a missing profile return `None`. Writes
    are explicit. There is no "merge / autocreate / default-and-pretend"
    path — UC code decides whether to upsert.
"""
from __future__ import annotations

import os
import time
from typing import Any, Protocol, runtime_checkable

from oneops.observability import get_logger

_log = get_logger("oneops.session.profile_store")


@runtime_checkable
class UserProfileStore(Protocol):
    """Tenant-partitioned long-term per-user profile store."""

    async def get(
        self, *, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None: ...

    async def put(
        self, *, tenant_id: str, user_id: str, profile: dict[str, Any],
    ) -> None: ...

    async def merge(
        self, *, tenant_id: str, user_id: str, patch: dict[str, Any],
    ) -> dict[str, Any]: ...


class InMemoryUserProfileStore:
    """Deterministic in-process profile store — the no-infra default.

    Profiles are namespaced by `(tenant_id, user_id)`; a lookup with the wrong
    tenant cannot reach another tenant's row by construction."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    def clear(self) -> None:
        self._rows.clear()

    async def get(
        self, *, tenant_id: str, user_id: str,
    ) -> dict[str, Any] | None:
        if not tenant_id or not user_id:
            return None
        row = self._rows.get((tenant_id, user_id))
        return dict(row) if row is not None else None

    async def put(
        self, *, tenant_id: str, user_id: str, profile: dict[str, Any],
    ) -> None:
        if not tenant_id:
            raise ValueError("UserProfileStore.put: tenant_id is mandatory")
        if not user_id:
            raise ValueError("UserProfileStore.put: user_id is mandatory")
        if not isinstance(profile, dict):
            raise ValueError(
                "UserProfileStore.put: profile must be a dict, got "
                f"{type(profile).__name__}")
        self._rows[(tenant_id, user_id)] = {
            **dict(profile),
            "_updated_at": time.time(),
        }

    async def merge(
        self, *, tenant_id: str, user_id: str, patch: dict[str, Any],
    ) -> dict[str, Any]:
        """Shallow merge: keys in `patch` overwrite top-level keys in the
        existing profile (or create it). Returns the merged profile.

        Shallow is deliberate — nested merge invites silent corruption of
        sub-structures; if a UC needs deep merge it must read, transform, and
        `put` so the change is explicit."""
        if not tenant_id:
            raise ValueError("UserProfileStore.merge: tenant_id is mandatory")
        if not user_id:
            raise ValueError("UserProfileStore.merge: user_id is mandatory")
        if not isinstance(patch, dict):
            raise ValueError(
                "UserProfileStore.merge: patch must be a dict, got "
                f"{type(patch).__name__}")
        existing = self._rows.get((tenant_id, user_id), {})
        merged = {**existing, **patch, "_updated_at": time.time()}
        self._rows[(tenant_id, user_id)] = merged
        return dict(merged)


class DragonflyUserProfileStore:
    """Live Dragonfly-backed profile store — placeholder for FaaS deployment.

    Selected by `ONEOPS_PROFILE_BACKEND=dragonfly`. Until the cluster is
    provisioned this fails loud, never silent."""

    async def get(
        self, *, tenant_id: str, user_id: str,
    ) -> dict[str, Any] | None:
        raise NotImplementedError(
            "DragonflyUserProfileStore is not implemented yet — unset "
            "ONEOPS_PROFILE_BACKEND (or set it to 'memory') to use the "
            "in-memory backend.")

    async def put(
        self, *, tenant_id: str, user_id: str, profile: dict[str, Any],
    ) -> None:
        raise NotImplementedError(
            "DragonflyUserProfileStore is not implemented yet.")

    async def merge(
        self, *, tenant_id: str, user_id: str, patch: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "DragonflyUserProfileStore is not implemented yet.")


_store: UserProfileStore | None = None


def _build_default() -> UserProfileStore:
    backend = os.getenv("ONEOPS_PROFILE_BACKEND", "memory").strip().lower()
    if backend == "dragonfly":
        _log.info("profile_store.backend_selected", backend="dragonfly")
        return DragonflyUserProfileStore()
    _log.info("profile_store.backend_selected", backend="memory")
    return InMemoryUserProfileStore()


def get_user_profile_store() -> UserProfileStore:
    """The process-wide profile store. Lazy; cold-start-safe."""
    global _store
    if _store is None:
        _store = _build_default()
    return _store


def set_user_profile_store(store: UserProfileStore) -> None:
    """Replace the process-wide profile store — for tests and FaaS wiring."""
    global _store
    _store = store


__all__ = [
    "UserProfileStore",
    "InMemoryUserProfileStore",
    "DragonflyUserProfileStore",
    "get_user_profile_store",
    "set_user_profile_store",
]
