"""Seed the production CAT_ONBOARDING catalog template with real
tool_ids + per-task input_templates.

Idempotent — re-runnable. Tenant-scoped to T001. Production-grade
substitute for the test-fixture hot-fix that previously patched task
rows post-materialise.

Other catalog items (CAT_LAPTOP_STD, CAT_VPN_ACCESS, …) need the same
treatment — see TODO at end.
"""
from __future__ import annotations

import asyncio
import json
import os

import asyncpg


_ONBOARDING_TASKS = [
    {
        "task_id": "T1", "name": "Create AD account",
        "type": "automated", "owner_group": "GRP-SECOPS",
        "depends_on": [],
        "tool_id": "create_directory_account",
        "input_template": {
            "user_full_name": "{employee_name}",
            "email_suggested": "{employee_email}",
        },
        "sla_minutes": 30,
    },
    {
        "task_id": "T2", "name": "Order laptop",
        "type": "automated", "owner_group": "GRP-PROCUREMENT",
        "depends_on": [],
        "tool_id": "order_hardware_asset",
        "input_template": {
            "asset_type": "laptop",
            "model_preferred": "{laptop_model}",
            "deliver_to": "{office_location}",
        },
        "sla_minutes": 120,
    },
    {
        "task_id": "T3", "name": "Schedule HR induction",
        "type": "manual", "owner_group": "GRP-HR",
        "depends_on": [], "sla_minutes": 240,
    },
    {
        "task_id": "T4", "name": "Provision mailbox",
        "type": "automated", "owner_group": "GRP-EXCHANGE",
        "depends_on": ["T1"],
        "tool_id": "provision_email_mailbox",
        "input_template": {
            "user_full_name": "{employee_name}",
            "primary_smtp": "{employee_email}",
        },
        "sla_minutes": 60,
    },
    {
        "task_id": "T5", "name": "Grant VPN access",
        "type": "automated", "owner_group": "GRP-NETSEC",
        "depends_on": ["T1"],
        "tool_id": "grant_vpn_access",
        "input_template": {"user_id": "{requested_for}"},
        "sla_minutes": 30,
    },
    {
        "task_id": "T6", "name": "Add to AD groups",
        "type": "automated", "owner_group": "GRP-SECOPS",
        "depends_on": ["T1"],
        "tool_id": "add_to_groups",
        "input_template": {
            "user_id": "{requested_for}",
            "groups": ["all-staff", "{department}"],
        },
        "sla_minutes": 30,
    },
    {
        "task_id": "T7", "name": "Manager welcome call",
        "type": "manual", "owner_group": "GRP-MANAGER",
        "depends_on": ["T1", "T2", "T3"], "sla_minutes": 1440,
    },
    {
        "task_id": "T8", "name": "Issue access badge",
        "type": "manual", "owner_group": "GRP-FACILITIES",
        "depends_on": ["T1", "T2", "T3"], "sla_minutes": 1440,
    },
    {
        "task_id": "T9", "name": "Send onboarding-complete email",
        "type": "automated", "owner_group": "GRP-HR",
        "depends_on": ["T7", "T8"],
        "tool_id": "notify_milestone",
        "input_template": {
            "recipient_user_id": "{requested_for}",
            "message": "Onboarding complete for {employee_name}",
            "level": "info",
        },
        "sla_minutes": 15,
    },
]


async def main() -> None:
    pg_url = os.environ["POSTGRES_URL"]
    conn = await asyncpg.connect(pg_url)
    try:
        await conn.execute(
            "UPDATE itsm.catalog_item SET tasks = $1::jsonb "
            "WHERE tenant_id = $2 AND catalog_item_id = $3",
            json.dumps(_ONBOARDING_TASKS),
            "T001", "CAT_ONBOARDING",
        )
        n = await conn.fetchval(
            "SELECT jsonb_array_length(tasks) FROM itsm.catalog_item "
            "WHERE tenant_id='T001' AND catalog_item_id='CAT_ONBOARDING'",
        )
        print(f"OK — CAT_ONBOARDING now has {n} tasks with tool_ids + input_templates")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
