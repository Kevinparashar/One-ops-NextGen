#!/usr/bin/env python3
"""Counter-example regression gate — lock known routing confusions.

Runs scripts/counter_examples.json (mined hard-negatives, each a query that
LOOKS like one agent but belongs to another) through the REAL retrieve+rerank
path in production config (abstain floor from ONEOPS_ROUTER_ABSTAIN_MIN_SCORE).

The gate (exit non-zero) is the MUST_NOT_ROUTE check: a counter-example must
never land on the wrong look-alike agent. correct_route is reported but not gated
(button-only agents like uc05 aren't chat-reachable, so their positive target is
informational). This is the doc's "add counter-examples where routing fails" +
eval loop — promote a confusion to a permanent regression case so a future card
or prompt change can't silently re-break it.

Offline: Postgres + LLM gateway (no server). Run:
  .venv/bin/python scripts/counter_example_eval.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
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
    GatewayEmbedder, PgVectorRetriever, configure_hnsw_connection)


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


def _score_record(
    r: dict, q: str, chosen: set, violations: list[dict], rows: list[tuple],
) -> tuple[int, int]:
    """Score one counter-example: `(int(no must-not-route hit), int(correct
    route achieved))`. Records any forbidden-agent hit in `violations` and the
    display row in `rows`."""
    cr = r["correct_route"]
    cr_set = set(cr) if isinstance(cr, list) else {cr}
    mnr = set(r.get("must_not_route") or [])
    hit = sorted(mnr & chosen)               # must_not_route violations
    ok_clean = not hit
    ok_correct = cr_set.issubset(chosen)
    if hit:
        violations.append({"query": q, "landed_on": hit, "correct": sorted(cr_set)})
    rows.append((q, sorted(cr_set), sorted(chosen), ok_clean, ok_correct,
                 r.get("confusion", "")))
    return int(ok_clean), int(ok_correct)


def _report_counter(
    rows: list[tuple], clean: int, correct: int, n: int,
    abstain: float | None, top_k: int, violations: list[dict],
) -> int:
    """Print the per-case table + the CLEAN (gate) and correct-route lines +
    any regressions; return the exit code (0 iff no must-not-route hit)."""
    print(f"=== counter-example regression — {n} cases, abstain={abstain}, top_k={top_k} ===\n")
    for q, _cr, ch, okc, okr, conf in rows:
        mark = "✓" if okc else "✗ MUST-NOT-ROUTE HIT"
        corr = "  +correct" if okr else ""
        print(f"  {mark:<22} {q[:46]!r:<48} → {('+'.join(ch) or 'none')[:24]:<24} [{conf}]{corr}")
    print("\n" + "=" * 60)
    print(f"CLEAN (no must-not-route hit) : {clean}/{n} = {clean/n*100:.1f}%   ← the gate")
    print(f"correct_route achieved        : {correct}/{n} = {correct/n*100:.1f}%   (info; button-only not chat-reachable)")
    print("=" * 60)
    if violations:
        print("\n⚠ REGRESSIONS — a counter-example landed on a forbidden agent:")
        for v in violations:
            print(f"  ✗ {v['query']!r}  landed on {v['landed_on']}  (should be {v['correct']})")
    return 0 if not violations else 1


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--tenant", default=os.getenv("ONEOPS_EVAL_TENANT", "T001"))
    ap.add_argument("--data", default=str(_ROOT / "scripts" / "counter_examples.json"))
    args = ap.parse_args()

    data = json.loads(Path(args.data).read_text())
    records = data.get("records", [])
    floor = os.getenv("ONEOPS_ROUTER_ABSTAIN_MIN_SCORE", "").strip()
    abstain = float(floor) if floor else None

    registry = load_registry()
    gw = _gateway()
    pool = await asyncpg.create_pool(
        _pg_url(), min_size=1, max_size=4, init=configure_hnsw_connection)
    retr = PgVectorRetriever(registry, embedder=GatewayEmbedder(gw), pool=pool)
    dis = LlmDisambiguator(gw, registry=registry, abstain_min_score=abstain)

    clean = correct = 0
    violations: list[dict] = []
    rows: list[tuple] = []
    try:
        for r in records:
            q = r["query"]
            cands = await retr.retrieve(q, tenant_id=args.tenant, top_k=args.top_k)
            chosen = set((await dis.disambiguate(q, cands, request_ctx={})).selected_agent_ids)
            c_clean, c_correct = _score_record(r, q, chosen, violations, rows)
            clean += c_clean
            correct += c_correct
    finally:
        await pool.close()

    return _report_counter(rows, clean, correct, len(records),
                           abstain, args.top_k, violations)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
