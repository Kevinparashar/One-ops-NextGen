"""Sync registries/v2/schemas/*.json into itsm.uc_schema (files = source of truth).

One DB row per (schema_id, version). Hash-gated UPSERT. No embeddings (the schema
is structural). Mirror of database/agent/sync.py + database/tool/sync.py.

  --retire-missing   set status='retired' for schema_ids in the DB but not in files.

Run:  .venv/bin/python database/uc_schema/sync.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "database"))

from _lib._loader import connect  # noqa: E402

_SCHEMAS_DIR = _ROOT / "registries" / "v2" / "schemas"

_UPSERT = """
INSERT INTO itsm.uc_schema (schema_id, version, status, body)
VALUES ($1, $2, $3, $4::jsonb)
ON CONFLICT (schema_id, version) DO UPDATE
  SET body = EXCLUDED.body, status = EXCLUDED.status, updated_at = now()
  WHERE itsm.uc_schema.body   IS DISTINCT FROM EXCLUDED.body
     OR itsm.uc_schema.status IS DISTINCT FROM EXCLUDED.status
"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--retire-missing", action="store_true")
    args = ap.parse_args()

    files = sorted(_SCHEMAS_DIR.glob("*.json"))
    if not files:
        raise SystemExit(f"no schema cards under {_SCHEMAS_DIR}")

    conn = await connect()
    try:
        seen: set[str] = set()
        rows = changed = 0
        async with conn.transaction():
            for f in files:
                card = json.loads(f.read_text())
                schema_id = card["id"]
                seen.add(schema_id)
                for vnum, vbody in (card.get("versions") or {}).items():
                    status = vbody.get("status", "active")
                    res = await conn.execute(
                        _UPSERT, schema_id, int(vnum), status, json.dumps(vbody))
                    rows += 1
                    if res.split()[-1] != "0":
                        changed += 1
            retired = 0
            if args.retire_missing:
                existing = {r["schema_id"] for r in
                            await conn.fetch("SELECT DISTINCT schema_id FROM itsm.uc_schema")}
                for gone in existing - seen:
                    res = await conn.execute(
                        "UPDATE itsm.uc_schema SET status='retired', updated_at=now() "
                        "WHERE schema_id=$1 AND status <> 'retired'", gone)
                    retired += int(res.split()[-1])
        print(f"uc_schema sync: files={len(files)} rows={rows} changed={changed} retired={retired}")
        print(f"itsm.uc_schema total rows: {await conn.fetchval('SELECT count(*) FROM itsm.uc_schema')}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
