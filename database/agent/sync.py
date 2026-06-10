"""Sync registries/v2/agents/*.json into itsm.agent (files = source of truth).

One DB row per (agent_id, version). Hash-gated: a row is only UPDATEd when the
card body or status actually changed — which flips content_hash, fires the
refresh trigger, and re-embeds the agent. Re-running with no file change is a
no-op (no spurious re-embeds).

  --retire-missing   set status='retired' for agent_ids present in the DB but no
                     longer in the files (off by default — explicit, reversible).

Run:  .venv/bin/python database/agent/sync.py
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

_AGENTS_DIR = _ROOT / "registries" / "v2" / "agents"

_UPSERT = """
INSERT INTO itsm.agent (agent_id, version, status, body)
VALUES ($1, $2, $3, $4::jsonb)
ON CONFLICT (agent_id, version) DO UPDATE
  SET body = EXCLUDED.body, status = EXCLUDED.status, updated_at = now()
  WHERE itsm.agent.body   IS DISTINCT FROM EXCLUDED.body
     OR itsm.agent.status IS DISTINCT FROM EXCLUDED.status
"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--retire-missing", action="store_true")
    args = ap.parse_args()

    files = sorted(_AGENTS_DIR.glob("*.json"))
    if not files:
        raise SystemExit(f"no agent cards under {_AGENTS_DIR}")

    conn = await connect()
    try:
        seen: set[str] = set()
        rows = changed = 0
        async with conn.transaction():
            for f in files:
                card = json.loads(f.read_text())
                agent_id = card["id"]
                seen.add(agent_id)
                for vnum, vbody in (card.get("versions") or {}).items():
                    status = vbody.get("status", "active")
                    res = await conn.execute(
                        _UPSERT, agent_id, int(vnum), status, json.dumps(vbody))
                    rows += 1
                    if res.split()[-1] != "0":
                        changed += 1
            retired = 0
            if args.retire_missing:
                existing = {r["agent_id"] for r in
                            await conn.fetch("SELECT DISTINCT agent_id FROM itsm.agent")}
                for gone in existing - seen:
                    res = await conn.execute(
                        "UPDATE itsm.agent SET status='retired', updated_at=now() "
                        "WHERE agent_id=$1 AND status <> 'retired'", gone)
                    retired += int(res.split()[-1])
        print(f"agent sync: files={len(files)} rows={rows} changed={changed} retired={retired}")
        total = await conn.fetchval("SELECT count(*) FROM itsm.agent")
        print(f"itsm.agent total rows: {total}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
