"""UC-5 storage layer — pluggable backend for probe ticket reads and apply writes.

Two implementations:
  • JsonFixtureStore — tests + demo (zero DB writes)
  • DbStore          — production (real itsm.incident / itsm.request UPDATE)

Same Protocol on both. apply.py is agnostic.
"""
from oneops.use_cases.uc05_triage.stores.base import TicketStore
from oneops.use_cases.uc05_triage.stores.db_store import DbStore
from oneops.use_cases.uc05_triage.stores.json_store import JsonFixtureStore

__all__ = ["TicketStore", "JsonFixtureStore", "DbStore"]
