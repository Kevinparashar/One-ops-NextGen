#!/usr/bin/env python3
"""Held-out A/B for the not_when wiring — UNSEEN queries, controlled comparison.

The not_when clauses were derived from hard negatives mined off routing_eval's
dataset, so testing on those would be circular. This harness uses a SEPARATE
held-out set (new phrasings, new ticket ids never used elsewhere) and reranks
each query TWICE with everything identical except the one change under test:

  OLD  = reranker catalog with description only      (pre-wiring behaviour)
  NEW  = reranker catalog with description + use_when + not_when  (this change)

Same retrieval feeds both, same model, same prompt — so any difference is the
not_when wiring alone. Reports per-query OLD vs NEW choice and overall accuracy.

Offline: Postgres + LLM gateway only (no server, no restart).
Run:  .venv/bin/python scripts/routing_holdout_eval.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
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
NONE = "NONE"

# HELD-OUT — none of these phrasings or ids appear in routing_eval.DATASET.
# New ids (INC0005005, REQ0002010, CHG0004010, PRB0002010) ensure the model
# can't pattern-match on a seen id. Focus: the confusion classes the wiring
# targets (how-to+id → uc03; summarize → uc01; similar → uc02) + clean + chit-chat.
HOLDOUT: list[tuple[str, str]] = [
    # how-to / resolution carrying a record id  → KB (the main fix)
    ("what's the fix for the error in INC0005005", A3),
    ("is there a documented workaround for the issue in INC0005005", A3),
    ("how do I resolve the problem behind PRB0002010", A3),
    ("any runbook that applies to REQ0002010", A3),
    ("steps to get the service in INC0005005 working again", A3),
    ("what should I try to clear the fault on CHG0004010", A3),
    # topic-only how-to (no id) → KB
    ("procedure for rotating an expired API key", A3),
    ("what's the process to enable BitLocker on a laptop", A3),
    # summarize / field-read (new phrasings) → entity summary
    ("give me the rundown on REQ0002010", A1),
    ("where does CHG0004010 stand right now", A1),
    ("who's the current owner of INC0005005", A1),
    ("recap PRB0002010 for me", A1),
    ("what's the latest state of CHG0004010", A1),
    # similar / recurrence (new phrasings) → similar tickets
    ("anything comparable to INC0005005 in the past", A2),
    ("have we seen the same problem as INC0005005 before", A2),
    ("pull up tickets that look like REQ0002010", A2),
    # off-domain → none
    ("what's a good lunch spot downtown", NONE),
    ("explain quantum computing in one sentence", NONE),
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


def _desc_only_catalog(registry) -> str:
    """Reproduce the PRE-WIRING catalog (id + description only)."""
    ids = sorted(registry.agents.list_ids())
    lines = ["## Agent catalog (stable; cached in system prompt)"]
    for aid in ids:
        a = registry.agents.get_optional(aid)
        desc = (a.description or "").strip() if a else ""
        if desc:
            lines.append(f"\n### {aid}\n{desc}")
    return "\n".join(lines)


def _ok(expected: str, chosen: list[str]) -> bool:
    if expected == NONE:
        return not chosen
    return expected in chosen


def _format_holdout_row(
    q: str, exp: str, co: list, cn: list, oo: bool, on: bool,
) -> str:
    """One A/B comparison row: OLD vs NEW verdict marks, agent sets, and a
    FIXED/BROKE flip tag."""
    mo = "✓" if oo else "✗"
    mn = "✓" if on else "✗"
    if not oo and on:
        flip = "  ← FIXED"
    elif oo and not on:
        flip = "  ← BROKE"
    else:
        flip = ""
    exp_lbl = (exp.replace('uc0', 'u').replace('_summarization', '')
               .replace('_similar_tickets', '').replace('_kb_lookup', '')
               .replace('NONE', 'none'))
    return (f"{q[:50]!r:<52} {exp_lbl:<6} "
            f"{mo} {('+'.join(co) or 'none')[:20]:<20} "
            f"{mn} {('+'.join(cn) or 'none')[:20]:<20}{flip}")


def _report_holdout(
    rows: list, old_ok: int, new_ok: int, n: int, top_k: int,
    abstain: float, flips_fixed: list[str], flips_broke: list[str],
) -> None:
    """Print the OLD-vs-NEW A/B table, the two accuracy lines, and the
    fixed/regressed flip lists."""
    print(f"=== held-out A/B — {n} UNSEEN queries, top_k={top_k} "
          f"(OLD=desc-only, NEW=+not_when +abstain@{abstain}) ===\n")
    print(f"{'query':<52} {'want':<6} {'OLD':<22} {'NEW':<22}")
    print("-" * 104)
    for q, exp, co, cn, oo, on in rows:
        print(_format_holdout_row(q, exp, co, cn, oo, on))
    print("\n" + "=" * 60)
    print(f"OLD (description-only catalog): {old_ok}/{n} = {old_ok/n*100:.1f}%")
    print(f"NEW (not_when wired):          {new_ok}/{n} = {new_ok/n*100:.1f}%   "
          f"({'+' if new_ok>=old_ok else ''}{new_ok-old_ok})")
    print("=" * 60)
    if flips_fixed:
        print(f"\nFIXED by the wiring ({len(flips_fixed)}):")
        for q in flips_fixed:
            print(f"  ✓ {q!r}")
    if flips_broke:
        print(f"\n⚠ REGRESSED by the wiring ({len(flips_broke)}):")
        for q in flips_broke:
            print(f"  ✗ {q!r}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--tenant", default=os.getenv("ONEOPS_EVAL_TENANT", "T001"))
    ap.add_argument("--abstain", type=float, default=0.32,
                    help="abstain floor for the NEW reranker (0 = off); "
                         "below this top retrieval score → refuse-and-clarify")
    args = ap.parse_args()

    registry = load_registry()
    gw = _gateway()
    pool = await asyncpg.create_pool(
        _pg_url(), min_size=1, max_size=4, init=configure_hnsw_connection)
    retr = PgVectorRetriever(registry, embedder=GatewayEmbedder(gw), pool=pool)

    # NEW = the change under test: not_when wired in + abstain gate on.
    new = LlmDisambiguator(
        gw, registry=registry,
        abstain_min_score=(args.abstain if args.abstain > 0 else None))
    old = LlmDisambiguator(gw, registry=registry)          # baseline
    old._catalog_block = _desc_only_catalog(registry)      # force pre-wiring catalog

    old_ok = new_ok = 0
    flips_fixed: list[str] = []
    flips_broke: list[str] = []
    rows: list[tuple] = []
    try:
        for q, exp in HOLDOUT:
            cands = await retr.retrieve(q, tenant_id=args.tenant, top_k=args.top_k)
            ro = await old.disambiguate(q, cands, request_ctx={})
            rn = await new.disambiguate(q, cands, request_ctx={})
            co, cn = list(ro.selected_agent_ids), list(rn.selected_agent_ids)
            oo, on = _ok(exp, co), _ok(exp, cn)
            old_ok += int(oo)
            new_ok += int(on)
            if not oo and on:
                flips_fixed.append(q)
            if oo and not on:
                flips_broke.append(q)
            rows.append((q, exp, co, cn, oo, on))
    finally:
        await pool.close()

    _report_holdout(rows, old_ok, new_ok, len(HOLDOUT),
                    args.top_k, args.abstain, flips_fixed, flips_broke)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
