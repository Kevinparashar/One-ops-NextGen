"""Abstract TicketStore protocol — read probe + apply triage decision."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol


class TicketStore(Protocol):
    """Two operations UC-5 needs from whatever backs the ticket data."""

    async def get_ticket(
        self, *, service_id: str, ticket_id: str, tenant_id: str
    ) -> Mapping[str, Any]:
        """Fetch a single ticket row. Raises KeyError if not found."""
        ...

    async def apply(
        self,
        *,
        service_id: str,
        ticket_id: str,
        tenant_id: str,
        final_values: Mapping[str, Any],
        sla_due: datetime,
        actor_user_id: str,
        now: datetime | None = None,
    ) -> None:
        """Persist the technician's approved triage values.

        final_values keys are column names (category, subcategory, ...).
        sla_due is the computed deadline. actor_user_id goes into audit.
        Raises KeyError if the ticket no longer exists; raises RuntimeError
        on optimistic-lock conflict.
        """
        ...
