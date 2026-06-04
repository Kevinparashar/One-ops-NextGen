"""JSON-file-backed TicketStore — tests + demos. Zero DB writes."""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oneops.observability import span


class JsonFixtureStore:
    """File-backed implementation. Reads + writes a JSON fixture file in
    place. Production paths use DbStore instead — the two satisfy the
    same TicketStore protocol so apply.py is backend-agnostic.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)

    async def get_ticket(
        self, *, service_id: str, ticket_id: str, tenant_id: str
    ) -> Mapping[str, Any]:
        with span("uc05.store.get_ticket",
                  **{"oneops.tenant_id": tenant_id,
                     "uc05.service_id": service_id,
                     "uc05.ticket_id": ticket_id,
                     "uc05.store": "json"}):
            bucket = "incidents" if service_id == "incident" else "requests"
            id_field = f"{service_id}_id"
            data = json.loads(self.path.read_text())
            if data.get("tenant_id") != tenant_id:
                raise KeyError(f"tenant mismatch for {ticket_id}")
            for row in data.get(bucket, []):
                if row.get(id_field) == ticket_id:
                    return dict(row)
            raise KeyError(ticket_id)

    async def list_all(
        self, *, service_id: str, tenant_id: str
    ) -> list[dict[str, Any]]:
        """Return all rows for a service in this tenant. Used by the queue endpoint."""
        bucket = "incidents" if service_id == "incident" else "requests"
        data = json.loads(self.path.read_text())
        if data.get("tenant_id") != tenant_id:
            return []
        return [dict(r) for r in data.get(bucket, [])]

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
        with span("uc05.store.apply",
                  **{"oneops.tenant_id": tenant_id,
                     "oneops.user_id": actor_user_id,
                     "uc05.service_id": service_id,
                     "uc05.ticket_id": ticket_id,
                     "uc05.store": "json"}):
            await self._apply_impl(
                service_id=service_id, ticket_id=ticket_id, tenant_id=tenant_id,
                final_values=final_values, sla_due=sla_due,
                actor_user_id=actor_user_id, now=now,
            )

    async def _apply_impl(self, *, service_id, ticket_id, tenant_id,
                           final_values, sla_due, actor_user_id, now):
        bucket = "incidents" if service_id == "incident" else "requests"
        id_field = f"{service_id}_id"
        data = json.loads(self.path.read_text())
        if data.get("tenant_id") != tenant_id:
            raise KeyError(f"tenant mismatch for {ticket_id}")
        when = (now or datetime.now(UTC)).isoformat()
        for row in data.get(bucket, []):
            if row.get(id_field) != ticket_id:
                continue
            # Optimistic-lock: refuse if already triaged (status != "new"
            # and not the empty pre-triage shape).
            if row.get("triaged_at"):
                raise RuntimeError(
                    f"{ticket_id} already triaged at {row['triaged_at']}"
                )
            for k, v in final_values.items():
                row[k] = v
            row["sla_due"] = sla_due.isoformat()
            row["triaged_at"] = when
            row["triaged_by"] = actor_user_id
            row["status"] = "assigned"
            row["updated_at"] = when
            self.path.write_text(json.dumps(data, indent=2))
            return
        raise KeyError(ticket_id)
