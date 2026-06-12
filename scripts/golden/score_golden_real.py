"""Score the real-grounded golden set by ROUTE CORRECTNESS — not non-refusal.

For each labeled case we send the query to /api/chat, extract the agent(s) that
actually handled it, and check it matches the expected UC. This is the honest
metric: a query that routes to the WRONG agent FAILS here (the earlier
"did-it-refuse" metric would have passed it).

kb_then_sr is scored on the agenda behaviour (decided 2026-06-11): when the
system is unsure KB-vs-SR it must deliver KB self-service FIRST. So a kb_then_sr
case PASSES only if the knowledge agent (uc03) actually handled it; a straight-
to-catalog (uc08-only) route FAILS — which is exactly the gap we want measured.

Run:  .venv/bin/python scripts/golden/score_golden_real.py
Env:  EVAL_BASE (default http://localhost:8000/api/chat), EVAL_WORKERS (default 3)
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import urllib.request
from pathlib import Path

BASE = os.environ.get("EVAL_BASE", "http://localhost:8000/api/chat")
WORKERS = int(os.environ.get("EVAL_WORKERS", "3"))
GOLDEN = Path(__file__).resolve().parent / "golden_real.json"

EXPECT_AGENT = {
    "uc01": "uc01_summarization", "uc02": "uc02_similar_tickets",
    "uc03": "uc03_kb_lookup", "uc05": "uc05_triage", "uc08": "uc08_fulfillment",
}


def _routed(d: dict) -> set[str]:
    """The agent(s) that actually handled the turn (best-effort, robust to the
    interrupt path where step_results is empty)."""
    fr = (d.get("final_response") or "").lower()
    if "out of my scope" in fr or "within the itsm" in fr:
        return {"refused"}
    ag = {s.get("agent_id") for s in (d.get("step_results") or [])
          if isinstance(s, dict) and s.get("agent_id")}
    intr = d.get("interrupt")
    if intr and isinstance(intr, dict):
        # a user_selection / catalog interrupt is produced by the fulfilment agent
        ag.add("uc08_fulfillment")
    return ag or {"(none)"}


def _call(case: dict) -> dict:
    role = "service_desk_agent"
    req = urllib.request.Request(
        BASE, data=json.dumps({"message": case["query"]}).encode(),
        headers={"Content-Type": "application/json", "x-tenant-id": "T001",
                 "x-user-id": "golden-real", "x-role": role})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.load(r)
        actual = _routed(d)
    except Exception as exc:                                     # noqa: BLE001
        actual = {f"ERR:{str(exc)[:24]}"}

    exp = case["expected"]
    if exp == "off_domain":
        ok = actual == {"refused"}
    elif exp == "kb_then_sr":
        ok = "uc03_kb_lookup" in actual            # KB self-service delivered
    else:
        ok = EXPECT_AGENT[exp] in actual
    return {**case, "actual": sorted(actual), "ok": ok}


def main() -> None:
    cases = json.loads(GOLDEN.read_text())
    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(_call, cases):
            results.append(r)

    fams = sorted({c["family"] for c in cases})
    print(f"=== Golden route-correctness ({len(results)} real-grounded cases) ===\n")
    print(f"{'family':12} {'pass':>8}  rate")
    overall_ok = 0
    for fam in fams:
        rows = [r for r in results if r["family"] == fam]
        n_ok = sum(1 for r in rows if r["ok"])
        overall_ok += n_ok
        print(f"{fam:12} {n_ok:>4}/{len(rows):<3}  {100*n_ok/len(rows):.0f}%")
    print(f"\nOVERALL: {overall_ok}/{len(results)} = {100*overall_ok/len(results):.0f}%")

    print("\n=== misroutes (expected -> actual) ===")
    for r in results:
        if not r["ok"]:
            print(f"  [{r['family']:11}] exp={r['expected']:11} "
                  f"got={','.join(r['actual']):28} :: {r['query']!r}")

    out = GOLDEN.parent / "golden_real_results.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nfull results -> {out}")


if __name__ == "__main__":
    main()
