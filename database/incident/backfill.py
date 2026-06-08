"""One-shot incident embedding backfill — embeds ALL existing incidents.

Same builders + UPSERT as database/incident/worker.py (one embedding space, no
drift between bulk and live paths). Idempotent: the CAS guard skips rows whose
content_hash is unchanged. Use after creating the table, or after a model bump.

Run:  .venv/bin/python database/incident/backfill.py
      .venv/bin/python database/incident/backfill.py --chunks symptom_anchor --limit 50
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "database"))

from _lib._loader import connect  # noqa: E402
from _lib._worker_base import build_gateway, sha256, upsert_embedding  # noqa: E402
from oneops.embeddings.summarise_diagnosis import summarise_diagnosis  # noqa: E402
from oneops.embeddings.triage_input import build_embedding_input  # noqa: E402

_TARGET = "ai.embeddings_incident"
_FETCH_ALL = """
SELECT i.tenant_id, i.incident_id, i.title, i.description, i.category,
       i.subcategory, i.service_name, i.ci_id,
       c.ci_name, c.ci_type, c.location AS ci_location, i.work_notes
FROM itsm.incident i
LEFT JOIN itsm.cmdb_ci c ON c.ci_id = i.ci_id AND c.tenant_id = i.tenant_id
ORDER BY i.incident_id
"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", default="symptom_anchor,diagnosis_trail")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    chunks = {c.strip() for c in args.chunks.split(",") if c.strip()}

    conn = await connect()
    gateway = build_gateway()
    try:
        sql = _FETCH_ALL + (f" LIMIT {int(args.limit)}" if args.limit else "")
        rows = await conn.fetch(sql)
        sym = diag = 0
        for r in rows:
            tenant_id, entity_id = r["tenant_id"], r["incident_id"]
            if "symptom_anchor" in chunks:
                text = build_embedding_input(dict(r), "incident")
                if text.strip():
                    vec = (await gateway.embed([text], model="text-embedding-3-large",
                                               tenant_id=tenant_id, dimensions=1536))[0]
                    await upsert_embedding(conn, target_table=_TARGET, entity_id=entity_id,
                                           tenant_id=tenant_id, chunk_type="symptom_anchor",
                                           embedding=vec, content_hash=sha256(text),
                                           content_text=text)
                    sym += 1
            if "diagnosis_trail" in chunks:
                text = await summarise_diagnosis(gateway=gateway, tenant_id=tenant_id,
                                                 entity_id=entity_id, raw_trail=r.get("work_notes"))
                if text:
                    vec = (await gateway.embed([text], model="text-embedding-3-large",
                                               tenant_id=tenant_id, dimensions=1536))[0]
                    await upsert_embedding(conn, target_table=_TARGET, entity_id=entity_id,
                                           tenant_id=tenant_id, chunk_type="diagnosis_trail",
                                           embedding=vec, content_hash=sha256(text),
                                           content_text=text)
                    diag += 1
        print(f"incident backfill: rows={len(rows)} symptom={sym} diagnosis={diag}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
