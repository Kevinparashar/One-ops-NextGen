"""Derive `itsm.catalog_item.intent_keywords` — discriminative phrasings of how
a user actually asks for each catalog item.

WHY (2026-06-12, RAG quality): the catalog embedding is built from the item's
name + description + category (recall-first). Generic descriptions ("Request
access to a business application…") sit near many queries, so weak items pad the
suggestion list ("Application Access" surfaced under "order a headset"). Adding a
5th embed field — DERIVED intent keywords specific to the item — sharpens the
anchor so a query matches the item the user actually means.

This is DERIVED data (LLM-generated from the item's own fields), not authored and
not hardcoded — the catalog equivalent of an embedding. It is written to the DB
column; the change flips `content_hash_catalog`, which re-embeds the item (the
field_map row added in 02_embeddings.sql pulls intent_keywords into the anchor
text). Idempotent: only items whose source text changed (or whose keywords are
empty) are regenerated, unless --force.

Run (after 01_schema.sql + 02_embeddings.sql, before backfill.py):
  .venv/bin/python database/catalog_fulfillment/derive_intent_keywords.py
  .venv/bin/python database/catalog_fulfillment/derive_intent_keywords.py --force
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import psycopg  # noqa: E402

from oneops.config import get_settings  # noqa: E402

_LLM_URL = os.environ.get("LLM_GATEWAY_URL", "http://localhost:4301").rstrip("/")
_LLM_KEY = (os.environ.get("LLM_GATEWAY_API_KEY")
            or os.environ.get("LITELLM_MASTER_KEY") or "sk-1234")
_MODEL = os.environ.get("UC08_INTENT_KEYWORDS_MODEL", "gpt-4o-mini")

_SYS = (
    "You generate SEARCH INTENT KEYWORDS for one IT service-catalog item. Given "
    "the item's name, description and category, output the short phrases a user "
    "would actually type when they need THIS item — the symptoms, the synonyms, "
    "and the plain ways of asking for it.\n\n"
    "Rules:\n"
    "- 6 to 10 phrases, comma-separated, lowercase, no numbering, no explanation.\n"
    "- Be DISCRIMINATIVE: specific enough that THIS item is told apart from other "
    "catalog items. Avoid generic filler that fits many items (e.g. bare "
    "'request', 'access', 'help', 'need', 'IT').\n"
    "- Use the user's words (problems/outcomes), not internal jargon or group "
    "names.\n"
    "Return ONLY the comma-separated list."
)


def _gen(name: str, description: str, category: str) -> str:
    body = json.dumps({
        "model": _MODEL,
        "temperature": 0.0,
        "max_tokens": 120,
        "messages": [
            {"role": "system", "content": _SYS},
            {"role": "user", "content":
                f"Name: {name}\nDescription: {description or ''}\n"
                f"Category: {category or ''}"},
        ],
    }).encode()
    req = urllib.request.Request(
        f"{_LLM_URL}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_LLM_KEY}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    text = d["choices"][0]["message"]["content"].strip()
    # normalise: comma-joined, de-duped, no trailing junk
    parts = [p.strip().strip(".").lower() for p in text.replace("\n", ",").split(",")]
    seen: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.append(p)
    return ", ".join(seen[:10])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="regenerate even when keywords already present")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    dsn = get_settings().postgres_url
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, catalog_item_id, name, description, category, "
            "intent_keywords FROM itsm.catalog_item ORDER BY catalog_item_id")
        rows = cur.fetchall()

    todo = []
    for tenant_id, cid, name, desc, cat, existing in rows:
        # skip items that already have keywords (idempotent); --force overrides.
        if (not args.force) and existing and existing.strip():
            continue
        todo.append((tenant_id, cid, name, desc, cat))

    print(f"catalog items: {len(rows)}  to (re)derive: {len(todo)}")
    if not todo:
        print("nothing to do (all current). use --force to regenerate.")
        return

    def _work(item):
        tenant_id, cid, name, desc, cat = item
        try:
            return (tenant_id, cid, _gen(name, desc, cat))
        except Exception as exc:                                  # noqa: BLE001
            print(f"  ERR {cid}: {str(exc)[:80]}")
            return None

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for r in ex.map(_work, todo):
            if r:
                results.append(r)
                print(f"  {r[1]:24} <- {r[2][:70]}")

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for tenant_id, cid, kw in results:
            cur.execute(
                "UPDATE itsm.catalog_item SET intent_keywords=%s "
                "WHERE tenant_id=%s AND catalog_item_id=%s", (kw, tenant_id, cid))
    print(f"updated {len(results)} items. content_hash flips -> re-embed via "
          "backfill.py (or the live worker).")


if __name__ == "__main__":
    main()
