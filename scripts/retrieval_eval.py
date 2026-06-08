#!/usr/bin/env python3
"""Retrieval eval — isolates SEARCH quality (recall@K) + mines HARD NEGATIVES.

Complements scripts/routing_eval.py, which measures END-TO-END accuracy via
/api/chat (retrieve + rerank + funnel). This one calls the pgvector retriever
DIRECTLY (no server, no LLM decision) to answer two things the end-to-end
number cannot:

  1. RECALL@K — is the correct agent even in the retrieved shortlist?
       • recall HIGH but end-to-end accuracy LOWER  → the RERANKER (LLM
         disambiguator) is the bottleneck.
       • recall LOW                                 → the EMBEDDINGS / card
         descriptions are the bottleneck → sharpen cards or fine-tune the
         embedding model. (This is the diagnostic split the routing research
         says matters most before adding any domain pre-filter.)

  2. HARD NEGATIVES — for every query where the right agent is NOT rank-1, which
     WRONG (look-alike) agents outranked it, and by how much. These confusable
     pairs are exactly the training negatives for embedding fine-tuning and the
     targets for description sharpening / the overlap gate.

Off-domain (NONE) cases report the TOP score, so you can see how separable
chit-chat is from real intents (calibrates the abstain-gate floor).

Offline; needs only Postgres + the LLM gateway (for the query embedding).
Run:  .venv/bin/python scripts/retrieval_eval.py [--top-k 10] [--dump hard_negatives.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:                                              # noqa: BLE001
    pass

import asyncpg  # noqa: E402

# Reuse the SAME labelled cases the end-to-end eval uses, so the two report
# cards are comparable. routing_eval's main() is __main__-guarded, so importing
# only pulls the dataset + labels (no server calls).
from routing_eval import DATASET, NONE  # noqa: E402

from oneops.llm import LlmGateway  # noqa: E402
from oneops.llm.transport import LiteLLMTransport  # noqa: E402
from oneops.registry.loader import load_registry  # noqa: E402
from oneops.router.retrieval import (  # noqa: E402
    GatewayEmbedder,
    PgVectorRetriever,
    configure_hnsw_connection,
)

_KS = (1, 3, 5, 10)


def _pg_url() -> str:
    m = re.search(r"^POSTGRES_URL=(.+)$", (_ROOT / ".env").read_text(), re.M)
    if not m:
        raise SystemExit("POSTGRES_URL not found in .env")
    return m.group(1).strip().strip('"').strip("'")


def _gateway() -> LlmGateway:
    return LlmGateway(
        transport=LiteLLMTransport(
            base_url=os.environ.get("LLM_GATEWAY_URL", "http://localhost:4311"),
            api_key=(os.environ.get("LLM_GATEWAY_API_KEY")
                     or os.environ.get("LITELLM_MASTER_KEY") or "sk-1234")),
        redact=False)


def _exp_set(expected: object) -> set[str]:
    if isinstance(expected, (set, frozenset, list, tuple)):
        return set(expected)
    return {expected}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=10, help="shortlist depth to score recall over")
    ap.add_argument("--tenant", default=os.getenv("ONEOPS_EVAL_TENANT", "T001"))
    ap.add_argument("--domain", default=None, help="scope retrieval to one domain (default: all)")
    ap.add_argument("--dump", default=None, help="write hard negatives + misses to this JSON path")
    args = ap.parse_args()

    registry = load_registry()
    gw = _gateway()
    pool = await asyncpg.create_pool(
        _pg_url(), min_size=1, max_size=4, init=configure_hnsw_connection)
    retr = PgVectorRetriever(registry, embedder=GatewayEmbedder(gw), pool=pool)

    positives = [(q, _exp_set(e)) for (q, e, _) in DATASET if e != NONE]
    negatives = [q for (q, e, _) in DATASET if e == NONE]

    recall_hits = dict.fromkeys(_KS, 0)
    rr_sum = 0.0                                    # for MRR
    per_agent: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # agent -> [hit@K, n]
    hard_negs: list[dict] = []                      # right agent present but outranked
    misses: list[dict] = []                         # right agent not in top-K at all

    try:
        for q, exp in positives:
            cands = await retr.retrieve(
                q, tenant_id=args.tenant, top_k=args.top_k, domain=args.domain)
            ranked = [c.agent_id for c in cands]
            score = {c.agent_id: round(c.score, 3) for c in cands}
            for k in _KS:
                if exp.issubset(set(ranked[:k])):
                    recall_hits[k] += 1
            # rank of the best-placed expected agent (1-indexed)
            rank = min((ranked.index(a) + 1 for a in exp if a in ranked), default=None)
            rr_sum += (1.0 / rank) if rank else 0.0
            for a in exp:
                per_agent[a][1] += 1
                per_agent[a][0] += int(a in set(ranked[: args.top_k]))
            if rank is None:
                misses.append({"query": q, "expected": sorted(exp),
                               "top5": [(a, score[a]) for a in ranked[:5]]})
            elif rank > 1:
                outranking = [(a, score[a]) for a in ranked[:rank - 1] if a not in exp]
                hard_negs.append({"query": q, "expected": sorted(exp),
                                  "expected_rank": rank,
                                  "expected_score": next((score[a] for a in exp if a in score), None),
                                  "outranked_by": outranking})

        neg_rows = []
        for q in negatives:
            cands = await retr.retrieve(q, tenant_id=args.tenant, top_k=1, domain=args.domain)
            neg_rows.append((q, round(cands[0].score, 3) if cands else 0.0,
                             cands[0].agent_id if cands else "-"))
    finally:
        await pool.close()

    n = len(positives)
    print(f"=== retrieval eval — {n} positive queries, top_k={args.top_k}, "
          f"domain={args.domain or 'all'} ===\n")
    print("RECALL@K  (is the correct agent in the top-K shortlist?)")
    for k in _KS:
        pct = recall_hits[k] / n * 100 if n else 0
        bar = "█" * int(pct / 5)
        print(f"  recall@{k:<2} {recall_hits[k]:>2}/{n} = {pct:5.1f}%  {bar}")
    print(f"  MRR       {rr_sum / n:.3f}   (1.0 = right agent always rank-1)\n")

    print("PER-AGENT RECALL@{}  (was THIS agent retrieved when it should be?)".format(args.top_k))
    for a in sorted(per_agent):
        hit, tot = per_agent[a]
        print(f"  {a:<26} {hit}/{tot} = {hit / tot * 100 if tot else 0:3.0f}%")

    print("\nHARD NEGATIVES  (right agent retrieved but OUTRANKED by a look-alike)")
    print("  → these confusable pairs are the fine-tuning negatives + sharpening targets")
    if not hard_negs:
        print("  (none — every retrieved-correct agent was already rank-1)")
    for h in sorted(hard_negs, key=lambda x: -x["expected_rank"]):
        exp = "+".join(h["expected"])
        beat = "  ".join(f"{a}({s})" for a, s in h["outranked_by"])
        print(f"  ✗ {h['query']!r}")
        print(f"      want {exp} @rank{h['expected_rank']} (score {h['expected_score']}) — beaten by: {beat}")

    if misses:
        print(f"\nMISSES  (correct agent NOT in top-{args.top_k} at all — worst cases)")
        for m in misses:
            print(f"  ✗ {m['query']!r}  want {'+'.join(m['expected'])}")
            print("      top5: " + "  ".join(f"{a}({s})" for a, s in m["top5"]))

    if neg_rows:
        worst = max(s for _, s, _ in neg_rows)
        avg = sum(s for _, s, _ in neg_rows) / len(neg_rows)
        print("\nOFF-DOMAIN SEPARABILITY  (chit-chat top score — want LOW; informs abstain floor)")
        print(f"  max={worst:.3f}  avg={avg:.3f}  over {len(neg_rows)} chit-chat queries")
        for q, s, a in sorted(neg_rows, key=lambda x: -x[1])[:5]:
            print(f"    {s:.3f}  {a:<22} ← {q!r}")

    if args.dump:
        Path(args.dump).write_text(json.dumps(
            {"hard_negatives": hard_negs, "misses": misses,
             "recall": {f"@{k}": recall_hits[k] / n for k in _KS} if n else {}},
            indent=2) + "\n")
        print(f"\n✏  wrote {len(hard_negs)} hard negatives + {len(misses)} misses to {args.dump}")

    # Non-zero exit if recall@top_k isn't perfect — gates CI / signals work to do.
    return 0 if recall_hits[args.top_k] == n else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
