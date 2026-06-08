#!/usr/bin/env python3
"""50-query routing capability eval — UNSEEN queries, all cases.

Pure ROUTING (selection) test: does the router pick the right agent — or the
right SET of agents, or correctly refuse — for a query it has never seen?
Offline retrieve → rerank → abstain in production config (abstain floor from
env, not_when wired). No execution.

All 50 queries are NEW phrasings with NEW ticket ids (INC0009xxx / REQ0005xxx /
CHG0006xxx / PRB0003xxx / AST0002xxx / CI0005xxx) that appear in no other
dataset (routing_eval, holdout, counter_examples, team_demo) — so this measures
generalization, not memorization. Covers: single-agent (all 5), multi-agent
SETS (right set, right order), the how-to+id confusion class, and off-domain
(must abstain).

Run:  .venv/bin/python scripts/routing_eval50.py
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

A1, A2, A3 = "uc01_summarization", "uc02_similar_tickets", "uc03_kb_lookup"
A5, A8 = "uc05_triage", "uc08_fulfillment"
NONE = "NONE"

# (query, expected, category) — expected: agent id, set of ids, or NONE.
DATASET: list[tuple[str, object, str]] = [
    # ── uc01 summarize / field-read (new phrasings, new ids) ──
    ("pull up the full picture on INC0009012", A1, "uc01"),
    ("what's the present condition of CHG0006014", A1, "uc01"),
    ("break down PRB0003021 for me", A1, "uc01"),
    ("who's looking after REQ0005033", A1, "uc01"),
    ("give me the lowdown on AST0002007", A1, "uc01"),
    ("what stage is CHG0006014 at", A1, "uc01"),
    ("show everything we have on CI0005019", A1, "uc01"),
    ("what's the story with INC0009088", A1, "uc01"),
    ("severity and owner of INC0009012", A1, "uc01"),
    ("the SLA status on REQ0005033", A1, "uc01"),
    # ── uc02 similar / duplicates / precedents ──
    ("show me past cases resembling INC0009012", A2, "uc02"),
    ("has anything like CHG0006014 come up before", A2, "uc02"),
    ("other records matching the pattern of INC0009088", A2, "uc02"),
    ("run a duplicate check for REQ0005033", A2, "uc02"),
    ("find precedents for PRB0003021", A2, "uc02"),
    ("what prior tickets echo INC0009012", A2, "uc02"),
    ("anything in history that mirrors the AST0002007 issue", A2, "uc02"),
    # ── uc03 KB / how-to (incl. how-to+id confusion class) ──
    ("walk me through setting up Okta MFA", A3, "uc03"),
    ("what's the procedure to decommission a server", A3, "uc03"),
    ("is there a guide for migrating a mailbox to O365", A3, "uc03"),
    ("steps to recover a deleted SharePoint site", A3, "uc03"),
    ("documentation for configuring SSO with Azure AD", A3, "uc03"),
    ("fix for the blue screen 0x0000007B", A3, "uc03"),
    ("how to whitelist an app in the firewall", A3, "uc03"),
    ("best way to resolve the kerberos auth failure on INC0009012", A3, "uc03-howto+id"),
    ("is there a known-issue writeup for the Citrix black screen", A3, "uc03"),
    ("what's the documented fix for the error in INC0009088", A3, "uc03-howto+id"),
    # ── uc05 triage ──
    ("categorize and prioritize INC0009012", A5, "uc05"),
    ("which group should own CHG0006014", A5, "uc05"),
    ("set the right priority and assignee for INC0009088", A5, "uc05"),
    ("route PRB0003021 to the correct team", A5, "uc05"),
    ("do a triage pass on INC0009012", A5, "uc05"),
    ("assess and assign REQ0005033", A5, "uc05"),
    # ── uc08 fulfillment / catalog ──
    ("I want to request a standing desk", A8, "uc08"),
    ("provision a GitHub Enterprise seat for me", A8, "uc08"),
    ("onboard a contractor starting next Monday", A8, "uc08"),
    ("get me access to the HR analytics dashboard", A8, "uc08"),
    ("set up a new Slack workspace for the design team", A8, "uc08"),
    ("order a docking station and a headset", A8, "uc08"),
    # ── multi-agent SETS (right set) ──
    ("summarize INC0009012 and pull similar ones", {A1, A2}, "set"),
    ("give me CHG0006014 details and any docs for it", {A1, A3}, "set"),
    ("what's wrong with INC0009088 and how do I resolve it", {A1, A3}, "set"),
    ("recap REQ0005033 and check it for duplicates", {A1, A2}, "set"),
    ("explain PRB0003021 and find the runbook for it", {A1, A3}, "set"),
    ("show tickets similar to INC0009012 and any related KB", {A2, A3}, "set"),
    # ── off-domain → must abstain ──
    ("what's the score of the Lakers game", NONE, "off_domain"),
    ("draft a birthday poem for my colleague", NONE, "off_domain"),
    ("convert 100 USD to EUR", NONE, "off_domain"),
    ("who painted the Mona Lisa", NONE, "off_domain"),
    ("suggest a quick workout routine", NONE, "off_domain"),
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


def _short(a: str) -> str:
    return a.replace("uc0", "u").replace("_summarization", "").replace(
        "_similar_tickets", "").replace("_kb_lookup", "").replace(
        "_triage", "").replace("_fulfillment", "")


def _verdict(expected: object, chosen: set[str]) -> bool:
    if expected == NONE:
        return not chosen
    if isinstance(expected, (set, frozenset, list, tuple)):
        return set(expected).issubset(chosen)
    return expected in chosen


def _score_row(
    q: str, exp: object, cat: str, chosen: set,
    by_cat: dict, misroutes: list, rows: list,
) -> int:
    """Verdict one query, update by-category counts + rows + misroutes, and
    return `int(ok)`."""
    ok = _verdict(exp, chosen)
    by_cat[cat][1] += 1
    by_cat[cat][0] += int(ok)
    exp_lbl = (_short("+".join(sorted(exp))) if isinstance(exp, (set, list, tuple))
               else _short(exp) if exp != NONE else "none")
    got_lbl = "+".join(sorted(_short(a) for a in chosen)) or "none"
    rows.append((ok, q, exp_lbl, got_lbl, cat))
    if not ok:
        misroutes.append(f"  ✗ {q!r}\n      want {exp_lbl}  got {got_lbl}  [{cat}]")
    return int(ok)


def _report50(
    rows: list, by_cat: dict, misroutes: list, ok_total: int,
    n: int, top_k: int, floor: str,
) -> int:
    """Print the per-query lines + overall + by-category report; return the
    exit code (0 iff every query routed correctly)."""
    print(f"=== routing capability — {n} UNSEEN queries, top_k={top_k}, "
          f"abstain={floor or 'off'} ===\n")
    for ok, q, exp_lbl, got_lbl, _cat in rows:
        print(f"  {'✓' if ok else '✗'} {q[:50]!r:<52} want={exp_lbl:<8} got={got_lbl}")
    print("\n" + "=" * 60)
    print(f"OVERALL: {ok_total}/{n} = {ok_total/n*100:.1f}%")
    print("=" * 60)
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
    args = ap.parse_args()

    floor = os.getenv("ONEOPS_ROUTER_ABSTAIN_MIN_SCORE", "").strip()
    registry = load_registry()
    gw = _gateway()
    pool = await asyncpg.create_pool(
        _pg_url(), min_size=1, max_size=4, init=configure_hnsw_connection)
    retr = PgVectorRetriever(registry, embedder=GatewayEmbedder(gw), pool=pool)
    dis = LlmDisambiguator(gw, registry=registry,
                           abstain_min_score=(float(floor) if floor else None))

    by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    misroutes: list[str] = []
    ok_total = 0
    rows: list[tuple] = []
    try:
        for q, exp, cat in DATASET:
            cands = await retr.retrieve(q, tenant_id=args.tenant, top_k=args.top_k)
            chosen = set((await dis.disambiguate(q, cands, request_ctx={})).selected_agent_ids)
            ok_total += _score_row(q, exp, cat, chosen, by_cat, misroutes, rows)
    finally:
        await pool.close()

    return _report50(rows, by_cat, misroutes, ok_total,
                     len(DATASET), args.top_k, floor)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
