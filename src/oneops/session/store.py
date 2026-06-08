"""SessionEventStore — append-only conversation history with a hot window.

Composition (docs/architecture/ARCHITECTURE.md §6):
  * the **cold** `EventLog` is the system of record — append-only, durable;
  * the **hot** `HotWindow` caches the most recent events for fast reads.

Write path : append → cold log (durable first) → refresh hot window.
Read path  : hot window → on miss, rebuild it from cold and serve.
Replay     : always from cold — the full, authoritative history.
Retention  : policy-driven (no hardcoded TTL) — prune cold, evict hot.

Tenant isolation is by construction — `tenant_id` is mandatory on every call
and is part of every backend key.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from oneops.config import get_settings
from oneops.observability import get_logger, get_tracer
from oneops.session.backend import ConversationEvent, EventLog, HotWindow

_log = get_logger("oneops.session.store")
_tracer = get_tracer("oneops.session.store")

# Telemetry literals (single source — sonar S1192).
_ONEOPS_TENANT_ID = "oneops.tenant_id"
_SESSION_ID = "session.id"


@dataclass(frozen=True)
class RetentionPolicy:
    """Resolved retention parameters. Today these come from settings; when the
    policy engine lands (P10) `resolve_retention()` is the single seam that
    starts reading `updated_policy_v2.md` instead — no caller changes."""

    hot_window_events: int          # how many recent events the hot cache holds
    cold_retention_days: int        # how long the durable log is kept

    def cold_cutoff_unix_ms(self, *, now_unix_ms: int | None = None) -> int:
        now = now_unix_ms if now_unix_ms is not None else int(time.time() * 1000)
        return now - self.cold_retention_days * 86_400_000


def resolve_retention() -> RetentionPolicy:
    """The single seam for retention policy. Reads config now; P10 swaps the
    body to consult the policy engine. Callers are unaffected."""
    settings = get_settings()
    return RetentionPolicy(
        hot_window_events=getattr(settings, "session_hot_window_events", 40),
        cold_retention_days=getattr(settings, "session_cold_retention_days", 90),
    )


class SessionEventStore:
    """The conversation-history API every service uses.

    Construct with a cold `EventLog` and a hot `HotWindow`. The store owns the
    durable-first ordering and the cache-rebuild-on-miss logic; the backends
    own only storage.
    """

    def __init__(self, cold: EventLog, hot: HotWindow,
                 retention: RetentionPolicy | None = None) -> None:
        self._cold = cold
        self._hot = hot
        self._retention = retention or resolve_retention()

    # ── write ────────────────────────────────────────────────────────────

    async def append(self, tenant_id: str, session_id: str,
                      event: ConversationEvent) -> int:
        """Append one conversation event. Durable first (cold), then the hot
        window is refreshed. Returns the cold-log sequence number.

        Cold-then-hot ordering is deliberate: a crash between the two leaves
        the hot cache stale-but-recoverable (the next read rebuilds it from
        cold). The reverse ordering could acknowledge an event that was never
        durably stored — that is the unacceptable failure.
        """
        with _tracer.start_as_current_span(
            "session.append",
            attributes={_ONEOPS_TENANT_ID: tenant_id, _SESSION_ID: session_id,
                        "session.turn_index": event.turn_index},
        ):
            seq = await self._cold.append(tenant_id, session_id, event)
            await self._hot.push(
                tenant_id, session_id, event,
                max_events=self._retention.hot_window_events,
            )
            return seq

    # ── read ─────────────────────────────────────────────────────────────

    async def recent(self, tenant_id: str, session_id: str) -> list[ConversationEvent]:
        """The recent conversation window, oldest-first.

        Hot-window hit → served from cache. Miss → rebuilt from the cold log
        (newest `hot_window_events`), the cache is repopulated, and the window
        is returned. A miss never fails — it falls through to the system of
        record.
        """
        with _tracer.start_as_current_span(
            "session.recent",
            attributes={_ONEOPS_TENANT_ID: tenant_id, _SESSION_ID: session_id},
        ) as span:
            cached = await self._hot.window(tenant_id, session_id)
            if cached is not None:
                span.set_attribute("session.cache_hit", True)
                return cached

            span.set_attribute("session.cache_hit", False)
            full = await self._cold.read(tenant_id, session_id)
            window = full[-self._retention.hot_window_events:]
            await self._hot.replace(tenant_id, session_id, window)
            return window

    async def replay(self, tenant_id: str, session_id: str, *,
                     from_turn: int = 0) -> list[ConversationEvent]:
        """The full authoritative history from `from_turn` onward — always from
        the cold log. Used for audit, debugging, and graph-state reconstruction.
        Never served from the (bounded) hot window."""
        with _tracer.start_as_current_span(
            "session.replay",
            attributes={_ONEOPS_TENANT_ID: tenant_id, _SESSION_ID: session_id,
                        "session.from_turn": from_turn},
        ):
            return await self._cold.read(tenant_id, session_id, from_turn=from_turn)

    # ── retention ────────────────────────────────────────────────────────

    async def apply_retention(self, tenant_id: str) -> int:
        """Prune cold-log events past the retention horizon for one tenant.

        Returns the number of events removed. The hot window is not pruned by
        age — it is size-bounded already and self-corrects on the next read.
        Intended to run as a scheduled job, never on the request path.
        """
        cutoff = self._retention.cold_cutoff_unix_ms()
        with _tracer.start_as_current_span(
            "session.apply_retention",
            attributes={_ONEOPS_TENANT_ID: tenant_id,
                        "session.cold_retention_days": self._retention.cold_retention_days},
        ) as span:
            removed = await self._cold.prune(tenant_id, older_than_unix_ms=cutoff)
            span.set_attribute("session.events_pruned", removed)
            if removed:
                _log.info("session.retention_pruned", tenant_id=tenant_id,
                          removed=removed, cutoff_unix_ms=cutoff)
            return removed


__all__ = ["SessionEventStore", "RetentionPolicy", "resolve_retention"]
