#!/usr/bin/env python3
"""100-query routing eval — real-user-style, UNSEEN, all scenarios.

Pure routing (selection) over all 5 agents, offline retrieve → rerank → abstain
in production config. 100 queries written in natural end-user voice (casual,
terse, lowercase, light typos, varied registers) — none appear in any other
dataset, all with fresh ids (INC0010xxx / REQ0006xxx / CHG0007xxx / PRB0004xxx /
AST0003xxx / CI0006xxx). Covers single-agent (all 5), multi-agent SETS,
how-to+id confusion, and off-domain.

NOTE: offline retrieve+rerank under-tests SETS (no decomposer) — see
scripts/multiagent_eval.py for the full-funnel set number. uc05/uc08 are
button-only in chat but are valid selection targets here (pure-selection test).

Run:  .venv/bin/python scripts/routing_eval100.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:                                              # noqa: BLE001
    pass

import asyncpg  # noqa: E402

from oneops.llm import LlmGateway  # noqa: E402
from oneops.llm.transport import LiteLLMTransport  # noqa: E402
from oneops.registry.loader import load_registry  # noqa: E402
from oneops.router.disambiguation import LlmDisambiguator  # noqa: E402
from oneops.router.retrieval import (  # noqa: E402
    GatewayEmbedder,
    PgVectorRetriever,
    configure_hnsw_connection,
)

# Repeated literals → constants (sonar S1192).
_UC03_HOWTO_ID = "uc03-howto+id"

A1, A2, A3 = "uc01_summarization", "uc02_similar_tickets", "uc03_kb_lookup"
A5, A8 = "uc05_triage", "uc08_fulfillment"
NONE = "NONE"

DATASET: list[tuple[str, object, str]] = [
    # ── uc01 summarize / field-read (real-user voice) ──
    ("whats the status on INC0010045", A1, "uc01"),
    ("can u give me a quick summary of CHG0007012", A1, "uc01"),
    ("who's handling REQ0006023 rn", A1, "uc01"),
    ("INC0010088 details pls", A1, "uc01"),
    ("hey whats going on with PRB0004011", A1, "uc01"),
    ("show me everything about CI0006007", A1, "uc01"),
    ("is INC0010045 still open", A1, "uc01"),
    ("priority of CHG0007012?", A1, "uc01"),
    ("tell me about that asset AST0003004", A1, "uc01"),
    ("what happened with INC0010088", A1, "uc01"),
    ("current state of REQ0006023", A1, "uc01"),
    ("who owns INC0010045", A1, "uc01"),
    ("give me the history on PRB0004011", A1, "uc01"),
    ("summarise CHG0007030 for me real quick", A1, "uc01"),
    ("whats the sla looking like on INC0010088", A1, "uc01"),
    ("any work notes on INC0010045", A1, "uc01"),
    ("the assignee for REQ0006040", A1, "uc01"),
    ("bring me up to speed on CHG0007012", A1, "uc01"),
    ("severity on INC0010088 please", A1, "uc01"),
    ("INC0010045", A1, "uc01"),
    # ── uc02 similar / duplicates / recurrence ──
    ("any tickets like INC0010045 before", A2, "uc02"),
    ("have we seen PRB0004011 happen previously", A2, "uc02"),
    ("find dupes of INC0010088", A2, "uc02"),
    ("similar cases to CHG0007012?", A2, "uc02"),
    ("did anyone else report something like REQ0006023", A2, "uc02"),
    ("show me past incidents matching INC0010045", A2, "uc02"),
    ("is this a repeat issue INC0010088", A2, "uc02"),
    ("other tickets with the same root cause as PRB0004011", A2, "uc02"),
    ("anything comparable to the AST0003004 problem", A2, "uc02"),
    ("pull similar resolved tickets for INC0010045", A2, "uc02"),
    ("check if INC0010088 is a known repeat", A2, "uc02"),
    ("recurring issues like INC0010045", A2, "uc02"),
    # ── uc03 KB / how-to (topic-only + how-to+id) ──
    ("how do i reset my password", A3, "uc03"),
    ("vpn wont connect any guide", A3, "uc03"),
    ("steps to set up outlook on a new phone", A3, "uc03"),
    ("my laptop is super slow how to fix", A3, "uc03"),
    ("how to request admin rights", A3, "uc03"),
    ("is there a doc on connecting to wifi", A3, "uc03"),
    ("fix for teams not loading", A3, "uc03"),
    ("how do i map a network drive", A3, "uc03"),
    ("printer keeps jamming whats the fix", A3, "uc03"),
    ("how to enable bitlocker", A3, "uc03"),
    ("guide for migrating to a new laptop", A3, "uc03"),
    ("how do i clear the teams cache", A3, "uc03"),
    ("whats the procedure for offboarding", A3, "uc03"),
    ("excel keeps freezing how do i sort it", A3, "uc03"),
    ("steps to recover deleted files from onedrive", A3, "uc03"),
    ("how do i join a distribution list", A3, "uc03"),
    ("best way to troubleshoot slow internet", A3, "uc03"),
    ("how to fix the issue in INC0010045", A3, _UC03_HOWTO_ID),
    ("any runbook for the problem in PRB0004011", A3, _UC03_HOWTO_ID),
    ("is there documentation to resolve INC0010088", A3, _UC03_HOWTO_ID),
    # ── uc05 triage ──
    ("triage INC0010045", A5, "uc05"),
    ("what priority should INC0010088 be", A5, "uc05"),
    ("which team handles CHG0007012", A5, "uc05"),
    ("categorize this ticket INC0010045", A5, "uc05"),
    ("who should i assign REQ0006023 to", A5, "uc05"),
    ("route PRB0004011 to the right group", A5, "uc05"),
    ("set priority and owner for INC0010088", A5, "uc05"),
    ("do a triage on INC0010045", A5, "uc05"),
    ("assess INC0010088 and tell me the team", A5, "uc05"),
    ("classify INC0010045", A5, "uc05"),
    # ── uc08 fulfillment / catalog ──
    ("i need a new laptop", A8, "uc08"),
    ("request access to the finance folder", A8, "uc08"),
    ("can i get adobe acrobat installed", A8, "uc08"),
    ("need a second monitor", A8, "uc08"),
    ("set me up with a new aws account", A8, "uc08"),
    ("i want to order a standing desk", A8, "uc08"),
    ("provision a github seat for me", A8, "uc08"),
    ("onboard a new hire starting monday", A8, "uc08"),
    ("get me a vpn token", A8, "uc08"),
    ("i need a headset for calls", A8, "uc08"),
    ("request a software license for figma", A8, "uc08"),
    ("set up email for the new intern", A8, "uc08"),
    # ── multi-agent SETS ──
    ("summarize INC0010045 and find similar ones", {A1, A2}, "set"),
    ("whats wrong with INC0010088 and how do i fix it", {A1, A3}, "set"),
    ("give me CHG0007012 details and any docs", {A1, A3}, "set"),
    ("recap REQ0006023 and check for dupes", {A1, A2}, "set"),
    ("explain PRB0004011 and find the runbook", {A1, A3}, "set"),
    ("show similar tickets to INC0010045 and any kb", {A2, A3}, "set"),
    ("status of INC0010088 plus past similar incidents", {A1, A2}, "set"),
    ("tell me about CHG0007012 and how to resolve it", {A1, A3}, "set"),
    ("who owns INC0010045 and is there a guide to fix it", {A1, A3}, "set"),
    ("summarize CHG0007030 and find related past changes", {A1, A2}, "set"),
    ("details on AST0003004 and comparable cases", {A1, A2}, "set"),
    ("INC0010088 overview, any dupes, and a fix doc", {A1, A2, A3}, "set"),
    # ── off-domain → must abstain ──
    ("whats the weather tomorrow", NONE, "off_domain"),
    ("tell me a fun fact", NONE, "off_domain"),
    ("how do i make pasta", NONE, "off_domain"),
    ("what's the capital of japan", NONE, "off_domain"),
    ("recommend a movie for tonight", NONE, "off_domain"),
    ("whats 15% of 240", NONE, "off_domain"),
    ("write a haiku about coffee", NONE, "off_domain"),
    ("who won the world cup", NONE, "off_domain"),
    ("translate good morning to spanish", NONE, "off_domain"),
    ("best restaurants near me", NONE, "off_domain"),
    ("how tall is mount everest", NONE, "off_domain"),
    ("play some music", NONE, "off_domain"),
]


def _pg_url() -> str:
    m = re.search(r"^POSTGRES_URL=(.+)$", (_ROOT / ".env").read_text(), re.M)
    if not m:
        raise SystemExit("POSTGRES_URL not found in .env")
    return m.group(1).strip().strip('"').strip("'")


def _gateway() -> LlmGateway:
    return LlmGateway(transport=LiteLLMTransport(
        base_url=os.environ.get("LLM_GATEWAY_URL", "http://localhost:4311"),
        api_key=(os.environ.get("LLM_GATEWAY_API_KEY")
                 or os.environ.get("LITELLM_MASTER_KEY") or "sk-1234")), redact=False)


def _s(a: str) -> str:
    return a.replace("uc0", "u").replace("_summarization", "").replace(
        "_similar_tickets", "").replace("_kb_lookup", "").replace(
        "_triage", "").replace("_fulfillment", "")


def _verdict(expected: object, chosen: set[str]) -> bool:
    if expected == NONE:
        return not chosen
    if isinstance(expected, (set, frozenset, list, tuple)):
        return set(expected).issubset(chosen)
    return expected in chosen


def _format_misroute(
    q: str, exp: object, cat: str, chosen: set, err: str | None,
) -> str:
    """One mis-route line: expected vs got (got = ERROR text, the chosen
    agent set, or 'none')."""
    exp_l = (_s("+".join(sorted(exp))) if isinstance(exp, (set, list, tuple))
             else _s(exp) if exp != NONE else "none")
    got_l = ("ERROR:" + err) if err else ("+".join(sorted(_s(a) for a in chosen)) or "none")
    return f"  ✗ {q!r}\n      want {exp_l}  got {got_l}  [{cat}]"


def _summarize(results: list, top_k: int, floor: str) -> int:
    """Fold the per-query routing results into overall + by-category accuracy,
    print the report, and return the process exit code (0 iff every query was
    routed correctly)."""
    by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    ok_total = 0
    misroutes: list[str] = []
    for q, exp, cat, chosen, err in results:
        ok = (not err) and _verdict(exp, chosen)
        by_cat[cat][1] += 1
        by_cat[cat][0] += int(ok)
        ok_total += int(ok)
        if not ok:
            misroutes.append(_format_misroute(q, exp, cat, chosen, err))

    n = len(results)
    print(f"=== 100-query routing — UNSEEN, real-user style, top_k={top_k}, "
          f"abstain={floor or 'off'} ===\n")
    print(f"OVERALL: {ok_total}/{n} = {ok_total/n*100:.1f}%\n")
    print("BY CATEGORY:")
    for cat in sorted(by_cat):
        ok, tot = by_cat[cat]
        print(f"  {cat:<16} {ok}/{tot} = {ok/tot*100:3.0f}%")
    if misroutes:
        print(f"\nMISROUTES ({len(misroutes)}):")
        print("\n".join(misroutes))
    return 0 if ok_total == n else 1


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--tenant", default=os.getenv("ONEOPS_EVAL_TENANT", "T001"))
    ap.add_argument("--concurrency", type=int, default=10,
                    help="how many queries to route in parallel")
    args = ap.parse_args()

    floor = os.getenv("ONEOPS_ROUTER_ABSTAIN_MIN_SCORE", "").strip()
    registry = load_registry()
    gw = _gateway()
    pool = await asyncpg.create_pool(
        _pg_url(), min_size=2, max_size=args.concurrency + 2,
        init=configure_hnsw_connection)
    retr = PgVectorRetriever(registry, embedder=GatewayEmbedder(gw), pool=pool)
    dis = LlmDisambiguator(gw, registry=registry,
                           abstain_min_score=(float(floor) if floor else None))

    sem = asyncio.Semaphore(args.concurrency)

    async def _run_one(q: str, exp: object, cat: str) -> tuple:
        async with sem:
            try:
                cands = await retr.retrieve(q, tenant_id=args.tenant, top_k=args.top_k)
                chosen = set((await dis.disambiguate(
                    q, cands, request_ctx={})).selected_agent_ids)
                return (q, exp, cat, chosen, None)
            except Exception as e:  # noqa: BLE001
                return (q, exp, cat, set(), str(e)[:120])

    try:
        results = await asyncio.gather(
            *[_run_one(q, exp, cat) for q, exp, cat in DATASET])
    finally:
        await pool.close()

    return _summarize(results, args.top_k, floor)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
