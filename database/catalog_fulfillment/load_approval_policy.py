"""Load the UC-8 approval matrix (data/itsm/approval_policy.json) into
itsm.approval_policy, expanded per tenant.

The matrix is one LOGICAL decision table (category/item rules + a fail-safe
catch-all). Each tenant gets its own copy of the rows so a tenant can diverge
later without touching the others; the loader seeds them identically today.
Idempotent upsert on (tenant_id, policy_id) — safe to re-run.

Run:  .venv/bin/python database/catalog_fulfillment/load_approval_policy.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _lib._loader import connect  # noqa: E402

_MATRIX = Path(__file__).resolve().parents[2] / "data" / "itsm" / "approval_policy.json"

_UPSERT = """
INSERT INTO itsm.approval_policy
    (tenant_id, policy_id, priority, match, required, stages, description)
VALUES ($1, $2, $3, $4::jsonb, $5, $6::jsonb, $7)
ON CONFLICT (tenant_id, policy_id) DO UPDATE SET
    priority    = EXCLUDED.priority,
    match       = EXCLUDED.match,
    required    = EXCLUDED.required,
    stages      = EXCLUDED.stages,
    description = EXCLUDED.description,
    enabled     = TRUE,
    updated_at  = now()
"""


async def main() -> None:
    matrix = json.loads(_MATRIX.read_text(encoding="utf-8"))
    policies = matrix["policies"]
    conn = await connect()
    try:
        # One matrix per tenant that actually has catalog items.
        tenants = [r["tenant_id"] for r in await conn.fetch(
            "SELECT DISTINCT tenant_id FROM itsm.catalog_item ORDER BY tenant_id")]
        async with conn.transaction():
            n = 0
            for tenant in tenants:
                for p in policies:
                    await conn.execute(
                        _UPSERT, tenant, p["policy_id"], p["priority"],
                        json.dumps(p.get("match", {})), p.get("required", True),
                        json.dumps(p.get("stages", [])), p.get("description"),
                    )
                    n += 1
            print(f"  approval_policy {n:4d} rows upserted "
                  f"({len(policies)} policies x {len(tenants)} tenants)")
        total = await conn.fetchval("SELECT count(*) FROM itsm.approval_policy")
        print(f"  total approval_policy {total:4d}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
