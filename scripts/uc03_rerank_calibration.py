#!/usr/bin/env python3
"""UC-3 reranker threshold calibration (Cohere method).

Locks `UC03_RERANK_MIN_RELEVANCE` on DATA, not a guessed cliff. Runs the REAL
KB pipeline (hybrid FTS+dense → RRF → LLM listwise reranker) over a labelled
query set, captures every candidate's normalised rerank score ONCE per query,
then sweeps candidate floors offline and reports, per floor:

  • find-recall     — find-queries where an on-topic article clears the floor
                      (it WOULD be surfaced)
  • top1-accuracy   — find-queries where the #1 surfaced article is on-topic
  • abstain-acc     — abstain-queries where the top candidate stays BELOW the
                      floor (correctly returns no_match)
  • combined        — mean of the three (the score we maximise)

Labels are by TOPIC; the accept-set of kb_ids is resolved from the LIVE
published+visible corpus at runtime (title-substring), so it tracks the seed
instead of mirroring ids by hand (§2.1 / never-hardcode). Off-domain and
content-gap queries are labelled ABSTAIN (topic=None).

Offline; needs Postgres + the LLM gateway. Run:
  .venv/bin/python scripts/uc03_rerank_calibration.py [--floors 0.33,0.5,1.0]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:                                              # noqa: BLE001
    pass

from oneops.llm import LlmGateway  # noqa: E402
from oneops.llm.transport import LiteLLMTransport  # noqa: E402
from oneops.use_cases._shared.field_policy import get_field_policy  # noqa: E402
from oneops.use_cases._shared.kb_store import get_kb_store  # noqa: E402
from oneops.use_cases.uc03_kb_lookup import handlers as H  # noqa: E402
from oneops.use_cases.uc03_kb_lookup.kb_embed import (  # noqa: E402
    build_cached_embed_fn,
    set_kb_embed_fn,
)
from oneops.use_cases.uc03_kb_lookup.kb_rerank import (  # noqa: E402
    LlmListwiseReranker,
    set_kb_reranker,
)

TENANT = "T001"
ROLE = "service_desk_agent"          # sees all audiences → clean topic recall

# (natural-language query, topic title-substring | None for ABSTAIN).
# Phrasings deliberately AVOID copying the article title — they read like a
# user, which is exactly what the reranker must handle.
CASES: list[tuple[str, str | None]] = [
    ("my vpn keeps dropping when I walk around the office", "VPN disconnects"),
    ("vpn won't stay connected while roaming", "roaming"),
    ("getting vpn error 809", "error 809"),
    ("my outlook email is delayed", "Outlook sync"),
    ("emails are stuck and not syncing", "Outlook sync"),
    ("the hr portal is throwing a 500 error", "HR portal 500"),
    ("payroll report keeps deadlocking", "Payroll DB deadlock"),
    ("salesforce data is lagging behind", "Salesforce sync"),
    ("wifi is down on our floor", "Wi-Fi floor outage"),
    ("mfa reset didn't take, still see old tokens", "MFA reset"),
    ("the service portal is really slow", "ITSM portal slowness"),
    ("monitoring alerts are arriving late", "Monitoring alert delay"),
    ("laptop patch install keeps failing", "Laptop patch failure"),
    ("erp export runs out of memory", "ERP export memory"),
    ("change calendar won't load", "Change calendar outage"),
    ("cmdb probe certificate has expired", "CMDB probe certificate"),
    ("how do I reset my password", "Password reset"),
    ("i forgot my password and can't log in", "Password reset"),
    ("having login issues", "Password reset"),
    ("the read replica is lagging", "replication lag"),
    ("we have duplicate CIs in the cmdb", "duplicate CI"),
    ("someone signed into my account, looks suspicious", "suspicious login"),
    ("i'm not getting ticket assignment notifications", "ticket notifications"),
    ("need to roll back after a bad deployment", "failed deployment"),
    ("my laptop won't boot after the update", "fails to boot"),
    ("database queries are running slow", "database query latency"),
    ("kubernetes pods keep restarting in a loop", "CrashLoopBackOff"),
    ("our site is down, the tls certificate expired", "TLS certificate"),
    ("crm is returning 500 errors", "CRM 500"),
    ("outbound webhooks are failing to deliver", "webhook deliveries"),
    ("kb search keeps surfacing stale articles", "knowledge search"),
    # ── ABSTAIN — off-domain ──────────────────────────────────────────────
    ("tell me a joke", None),
    ("what's the weather today", None),
    ("book me a flight to tokyo", None),
    ("who won the cricket match", None),
    # ── ABSTAIN — in-domain but no published article (content gap) ────────
    ("my mailbox quota is full", None),       # only article is draft
    ("the office coffee machine is broken", None),
]


async def _accept_ids(store, audiences, substr: str) -> set[str]:
    """Resolve a topic's accept-set from the LIVE published+visible corpus by
    case-insensitive title substring — derived, not hand-mirrored."""
    rows = await store.search(query=substr, tenant_id=TENANT,
                              audiences=audiences, limit=50)
    # store.search is FTS; fall back to a direct title scan via linked-free get
    # is unnecessary — instead match on title substring over a broad fetch.
    hits = {r["kb_id"] for r in rows
            if substr.lower() in str(r.get("title", "")).lower()}
    return hits


async def _candidates(store, embed_fn, reranker, query: str, audiences):
    """One pipeline pass → list of (kb_id, rerank_relevance) for the RRF top-8,
    ordered by rerank relevance desc. Mirrors handlers.search_kb stages."""
    per_side, _, _ = H._search_kb_config()
    qv = await embed_fn(query, tenant_id=TENANT, user_id="cal")
    import asyncio as _a
    fts, sem = await _a.gather(
        store.search(query=query, tenant_id=TENANT, audiences=audiences,
                     limit=per_side),
        store.search_semantic(query_vec=qv, tenant_id=TENANT,
                              audiences=audiences, limit=per_side) if qv else
        _noop(),
    )
    fused = H._rrf_fuse(list(fts), list(sem or []),
                        min_fused_score=0.012, top_k=8)
    for h in fused:
        h.pop("_fused_score", None)
        h.pop("_sources", None)
    verdicts = await reranker.rerank(query=query, articles=fused,
                                     tenant_id=TENANT, user_id="cal")
    if verdicts is None:
        return []
    by = {v.kb_id: v.relevance for v in verdicts}
    scored = [(h["kb_id"], by.get(h["kb_id"], 0.0)) for h in fused]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored


async def _noop():
    return []


def _bar(x: float) -> str:
    return "█" * int(round(x * 20))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--floors", default="0.33,0.5,1.0")
    args = ap.parse_args()
    floors = [float(x) for x in args.floors.split(",")]

    gw = LlmGateway(LiteLLMTransport(
        base_url=os.getenv("LLM_GATEWAY_URL", "").strip(),
        api_key=os.getenv("LLM_GATEWAY_API_KEY", "").split()[0].strip()))
    embed_fn = build_cached_embed_fn(
        gw, model=os.getenv("LLM_EMBED_MODEL", "text-embedding-3-large"),
        dimensions=int(os.getenv("LLM_EMBED_DIMENSIONS", "1536")))
    set_kb_embed_fn(embed_fn)
    reranker = LlmListwiseReranker(
        gw, model=os.getenv("ONEOPS_KB_RERANK_MODEL", "gpt-4o"))
    set_kb_reranker(reranker)
    store = get_kb_store()
    audiences = get_field_policy().kb_audiences_for(ROLE)

    # Capture once per query.
    rows = []   # (query, topic, accept_set, candidates[(id,rel)])
    for query, topic in CASES:
        accept = (await _accept_ids(store, audiences, topic)) if topic else set()
        cands = await _candidates(store, embed_fn, reranker, query, audiences)
        rows.append((query, topic, accept, cands))
        tag = "ABSTAIN" if topic is None else f"{sorted(accept)}"
        top = cands[0] if cands else ("-", 0.0)
        print(f"  {query[:46]:46} top={top[0]:>11} {top[1]:.2f}  accept={tag}")

    print("\n" + "=" * 72)
    print(f"{'floor':>6} | {'find-recall':>11} | {'top1-acc':>9} | "
          f"{'abstain-acc':>11} | {'combined':>9}")
    print("-" * 72)
    best = None
    for floor in floors:
        finds = [r for r in rows if r[1] is not None]
        absts = [r for r in rows if r[1] is None]
        recall = top1 = abst = 0
        for _, _, accept, cands in finds:
            surfaced = [(i, rel) for i, rel in cands if rel >= floor]
            if any(i in accept for i, rel in surfaced):
                recall += 1
            if surfaced and surfaced[0][0] in accept:
                top1 += 1
        for _, _, _, cands in absts:
            toprel = cands[0][1] if cands else 0.0
            if toprel < floor:
                abst += 1
        r_rec = recall / max(1, len(finds))
        r_top = top1 / max(1, len(finds))
        r_abs = abst / max(1, len(absts))
        combined = (r_rec + r_top + r_abs) / 3
        if best is None or combined > best[1]:
            best = (floor, combined)
        print(f"{floor:>6.2f} | {r_rec:>10.0%} {_bar(r_rec):<0} | "
              f"{r_top:>8.0%} | {r_abs:>10.0%} | {combined:>8.0%}")
    print("-" * 72)
    print(f"  find-queries={sum(1 for r in rows if r[1] is not None)}  "
          f"abstain-queries={sum(1 for r in rows if r[1] is None)}")
    print(f"\n  ➜ recommended UC03_RERANK_MIN_RELEVANCE = {best[0]:.2f} "
          f"(combined {best[1]:.0%})")


if __name__ == "__main__":
    asyncio.run(main())
