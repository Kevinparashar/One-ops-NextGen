"""Load the owning-group -> sys_user-attribute bridge into itsm.group_role_map.

Config-as-code, consistent with load_approval_policy.py:
  data/itsm/group_role_map.json  -->  itsm.group_role_map.
Each JSON entry ({role: X} | {department: Y}) becomes a (owner_group, attribute,
value) row. Idempotent upsert on owner_group; re-runnable. The Phase-2 HR/IdP
sync would populate the same table directly.

Run:  .venv/bin/python database/catalog_fulfillment/load_group_role_map.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _lib._loader import connect  # noqa: E402

_MAP = Path(__file__).resolve().parents[2] / "data" / "itsm" / "group_role_map.json"

_UPSERT = """
INSERT INTO itsm.group_role_map (owner_group, attribute, value)
VALUES ($1, $2, $3)
ON CONFLICT (owner_group) DO UPDATE SET
    attribute  = EXCLUDED.attribute,
    value      = EXCLUDED.value,
    updated_at = now()
"""


def _rows() -> list[tuple[str, str, str]]:
    groups = json.loads(_MAP.read_text(encoding="utf-8"))["groups"]
    out: list[tuple[str, str, str]] = []
    for owner_group, entry in groups.items():
        if "role" in entry:
            out.append((owner_group, "role", str(entry["role"])))
        elif "department" in entry:
            out.append((owner_group, "department", str(entry["department"])))
        else:
            raise ValueError(f"{owner_group}: entry needs role or department")
    return out


async def main() -> None:
    rows = _rows()
    conn = await connect()
    try:
        async with conn.transaction():
            for owner_group, attribute, value in rows:
                await conn.execute(_UPSERT, owner_group, attribute, value)
        total = await conn.fetchval("SELECT count(*) FROM itsm.group_role_map")
        print(f"  group_role_map {len(rows):3d} rows upserted | total {total}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
