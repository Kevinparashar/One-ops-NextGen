"""Agent distinctiveness / overlap report — the cross-agent routing-quality gate.

The per-card contract (tests/unit/registry/test_skill_card_contract.py) checks
each card in isolation. THIS checks the thing that breaks routing at scale: two
agents whose embedded content is too SIMILAR, so the retriever (and the LLM)
can't tell them apart. The #1 precision risk once there are hundreds of
overlapping ITOM use cases.

Method: over the live ai.embeddings_agent vectors, compute the max cosine
similarity between ANY chunk of agent A and ANY chunk of agent B, for every
distinct pair. Report pairs sorted by similarity; flag any above --threshold as
"differentiate these (sharpen description / add not_when)".

Runs today on the 5 ITSM agents (proves the tool); auto-covers every ITOM agent
the moment its embeddings exist. Read-only.

Run:  .venv/bin/python scripts/agent_overlap_report.py [--threshold 0.85] [--domain itsm]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

import asyncpg

_ROOT = Path(__file__).resolve().parents[1]


def _pg_url() -> str:
    m = re.search(r"^POSTGRES_URL=(.+)$", (_ROOT / ".env").read_text(), re.M)
    if not m:
        sys.exit("POSTGRES_URL not found in .env")
    return m.group(1).strip().strip('"').strip("'")


# Max cosine similarity between any chunk of A and any chunk of B, per pair.
# a.agent_id < b.agent_id dedupes the symmetric pair and drops self-pairs.
_SQL = """
SELECT a.agent_id AS a, b.agent_id AS b,
       max(1 - (a.embedding <=> b.embedding)) AS sim
FROM ai.embeddings_agent a
JOIN ai.embeddings_agent b
  ON a.agent_id < b.agent_id
 AND a.embedding_version = b.embedding_version
WHERE ($1::text IS NULL OR (a.domain = $1 AND b.domain = $1))
GROUP BY a.agent_id, b.agent_id
ORDER BY sim DESC
"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.85,
                    help="flag pairs at/above this max-chunk cosine similarity")
    ap.add_argument("--domain", default=None, help="scope to one domain (e.g. itsm)")
    ap.add_argument("--top", type=int, default=20, help="how many closest pairs to print")
    args = ap.parse_args()

    conn = await asyncpg.connect(_pg_url())
    try:
        n_agents = await conn.fetchval(
            "SELECT count(DISTINCT agent_id) FROM ai.embeddings_agent "
            "WHERE ($1::text IS NULL OR domain=$1)", args.domain)
        rows = await conn.fetch(_SQL, args.domain)
    finally:
        await conn.close()

    scope = f"domain={args.domain}" if args.domain else "all domains"
    print(f"=== agent overlap report ({scope}) — {n_agents} agents, "
          f"{len(rows)} pairs, flag >= {args.threshold} ===")
    if not rows:
        print("  (no agent pairs — need >=2 embedded agents)")
        return
    flagged = [r for r in rows if r["sim"] >= args.threshold]
    print(f"\nClosest {min(args.top, len(rows))} pairs (higher = more confusable):")
    for r in rows[: args.top]:
        mark = "  ⚠ TOO SIMILAR" if r["sim"] >= args.threshold else ""
        print(f"  {r['sim']:.3f}  {r['a']:24} ~ {r['b']:24}{mark}")
    print(f"\n{len(flagged)} pair(s) over the {args.threshold} threshold "
          f"→ differentiate (sharpen description / add not_when).")
    if flagged:
        sys.exit(1)   # non-zero so this can gate CI when wired


if __name__ == "__main__":
    asyncio.run(main())
