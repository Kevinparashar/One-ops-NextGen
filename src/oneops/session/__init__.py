"""Session + conversation store (P3).

A session's conversation is an append-only event log: durable in Postgres
(cold), with a bounded hot window in Dragonfly. `SessionEventStore` composes
the two; backends are pluggable behind the `EventLog` / `HotWindow` Protocols.

Public surface:
    from oneops.session import SessionEventStore, RetentionPolicy
    from oneops.session import InMemoryEventLog, InMemoryHotWindow   # tests / dev
    from oneops.session import PostgresEventLog, DragonflyHotWindow  # production
"""
from __future__ import annotations

from oneops.session.backend import (
    ConversationEvent,
    EventLog,
    HotWindow,
    InMemoryEventLog,
    InMemoryHotWindow,
)
from oneops.session.dragonfly_window import DragonflyHotWindow
from oneops.session.postgres_log import PostgresEventLog
from oneops.session.store import RetentionPolicy, SessionEventStore, resolve_retention

__all__ = [
    "ConversationEvent",
    "EventLog",
    "HotWindow",
    "InMemoryEventLog",
    "InMemoryHotWindow",
    "PostgresEventLog",
    "DragonflyHotWindow",
    "SessionEventStore",
    "RetentionPolicy",
    "resolve_retention",
]
