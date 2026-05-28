"""Per-tenant LLM quota — defence in depth against runaway spend.

A tenant has a call budget for the current window. `QuotaGuard.check_and_charge`
raises `QuotaExceededError` once the budget is spent — the gateway refuses the
call rather than letting one tenant exhaust the provider account.

P8 ships an in-process counter with a per-tenant limit; `reset_window` rolls it.
A Dragonfly-backed guard (shared counters across workers, time-windowed) is the
production form — the `check_and_charge` contract does not change.
"""
from __future__ import annotations

import threading

from oneops.errors import QuotaExceededError
from oneops.observability import get_logger

_log = get_logger("oneops.llm.quota")

# A generous default — quota is a backstop, not the primary cost control
# (that is per-tenant config). 0 or negative means "unlimited".
DEFAULT_CALLS_PER_WINDOW = 100_000


class QuotaGuard:
    """Per-tenant call-count quota for the current window. Thread-safe."""

    def __init__(self, *, default_limit: int = DEFAULT_CALLS_PER_WINDOW) -> None:
        self._lock = threading.RLock()
        self._default_limit = default_limit
        self._limits: dict[str, int] = {}        # per-tenant overrides
        self._used: dict[str, int] = {}

    def set_tenant_limit(self, tenant_id: str, limit: int) -> None:
        with self._lock:
            self._limits[tenant_id] = limit

    def _limit_for(self, tenant_id: str) -> int:
        return self._limits.get(tenant_id, self._default_limit)

    def check_and_charge(self, tenant_id: str) -> None:
        """Count one call against the tenant's budget. Raise
        `QuotaExceededError` if the budget is already spent."""
        with self._lock:
            limit = self._limit_for(tenant_id)
            used = self._used.get(tenant_id, 0)
            if limit > 0 and used >= limit:
                _log.warning("llm.quota_exceeded", tenant_id=tenant_id,
                             used=used, limit=limit)
                raise QuotaExceededError(
                    f"tenant '{tenant_id}' has used its {limit}-call LLM quota "
                    "for this window")
            self._used[tenant_id] = used + 1

    def used(self, tenant_id: str) -> int:
        with self._lock:
            return self._used.get(tenant_id, 0)

    def reset_window(self) -> None:
        """Roll the window — clear all per-tenant counters."""
        with self._lock:
            self._used.clear()


__all__ = ["QuotaGuard", "DEFAULT_CALLS_PER_WINDOW"]
