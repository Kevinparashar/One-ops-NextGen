"""Sync registries/v2/tools/<agent_id>/*.json into itsm.tool.

The owning agent_id is the parent folder (tools/<agent_id>/<tool>.json). One DB
row per (tool_id, version). Hash-gated UPSERT (only writes when body/status
changed). No embeddings — tools are selected deterministically, never by vector.

  --retire-missing   set status='retired' for tool_ids in the DB but not in files.

Run:  .venv/bin/python database/tool/sync.py
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

_TOOLS_DIR = _ROOT / "registries" / "v2" / "tools"

_UPSERT = """
INSERT INTO itsm.tool (tool_id, version, agent_id, status, body)
VALUES ($1, $2, $3, $4, $5::jsonb)
ON CONFLICT (tool_id, version) DO UPDATE
  SET agent_id = EXCLUDED.agent_id, body = EXCLUDED.body,
      status = EXCLUDED.status, updated_at = now()
  WHERE itsm.tool.body     IS DISTINCT FROM EXCLUDED.body
     OR itsm.tool.status   IS DISTINCT FROM EXCLUDED.status
     OR itsm.tool.agent_id IS DISTINCT FROM EXCLUDED.agent_id
"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--retire-missing", action="store_true")
    args = ap.parse_args()

    files = sorted(_TOOLS_DIR.glob("*/*.json"))
    if not files:
        raise SystemExit(f"no tool cards under {_TOOLS_DIR}")

    conn = await connect()
    try:
        seen: set[str] = set()
        rows = changed = 0
        async with conn.transaction():
            for f in files:
                agent_id = f.parent.name                 # owning agent = folder
                card = json.loads(f.read_text())
                tool_id = card["id"]
                seen.add(tool_id)
                for vnum, vbody in (card.get("versions") or {}).items():
                    status = vbody.get("status", "active")
                    res = await conn.execute(
                        _UPSERT, tool_id, int(vnum), agent_id, status, json.dumps(vbody))
                    rows += 1
                    if res.split()[-1] != "0":
                        changed += 1
            retired = 0
            if args.retire_missing:
                existing = {r["tool_id"] for r in
                            await conn.fetch("SELECT DISTINCT tool_id FROM itsm.tool")}
                for gone in existing - seen:
                    res = await conn.execute(
                        "UPDATE itsm.tool SET status='retired', updated_at=now() "
                        "WHERE tool_id=$1 AND status <> 'retired'", gone)
                    retired += int(res.split()[-1])
        print(f"tool sync: files={len(files)} rows={rows} changed={changed} retired={retired}")
        print(f"itsm.tool total rows: {await conn.fetchval('SELECT count(*) FROM itsm.tool')}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
