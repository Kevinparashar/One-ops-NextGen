"""Live golden scorer — prints each case the instant it resolves (flushed),
so progress is visible in real time. Same route-correctness logic as
score_golden_real.py; running tally per family at the end.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sys
import urllib.request
from pathlib import Path

BASE = os.environ.get("EVAL_BASE", "http://localhost:8000/api/chat")
GOLDEN = Path(__file__).resolve().parent / "golden_real.json"
EXPECT = {"uc01": "uc01_summarization", "uc02": "uc02_similar_tickets",
          "uc03": "uc03_kb_lookup", "uc05": "uc05_triage",
          "uc08": "uc08_fulfillment"}


def routed(d: dict) -> set[str]:
    fr = (d.get("final_response") or "").lower()
    if "out of my scope" in fr or "within the itsm" in fr:
        return {"refused"}
    ag = {s.get("agent_id") for s in (d.get("step_results") or [])
          if isinstance(s, dict) and s.get("agent_id")}
    if isinstance(d.get("interrupt"), dict):
        ag.add("uc08_fulfillment")
    return ag or {"(none)"}


def call(case: dict) -> dict:
    req = urllib.request.Request(
        BASE, data=json.dumps({"message": case["query"]}).encode(),
        headers={"Content-Type": "application/json", "x-tenant-id": "T001",
                 "x-user-id": "golden-live", "x-role": "service_desk_agent"})
    try:
        with urllib.request.urlopen(req, timeout=150) as r:
            a = routed(json.load(r))
    except Exception as exc:                                     # noqa: BLE001
        a = {f"ERR:{str(exc)[:20]}"}
    exp = case["expected"]
    ok = (a == {"refused"} if exp == "off_domain"
          else "uc03_kb_lookup" in a if exp == "kb_then_sr"
          else EXPECT[exp] in a)
    return {**case, "actual": sorted(a), "ok": ok}


def main() -> None:
    cases = json.loads(GOLDEN.read_text())
    done = 0
    tally: dict[str, list[int]] = {}
    results = []
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for r in ex.map(call, cases):
            done += 1
            results.append(r)
            t = tally.setdefault(r["family"], [0, 0])
            t[1] += 1
            t[0] += 1 if r["ok"] else 0
            mark = "OK " if r["ok"] else "XX "
            print(f"[{done:3}/{len(cases)}] {mark} {r['family']:11} "
                  f"exp={r['expected']:11} got={','.join(r['actual'])[:34]:34} "
                  f":: {r['query'][:40]}", flush=True)
    print("\n=== tally ===", flush=True)
    tot_ok = 0
    for fam in sorted(tally):
        ok, n = tally[fam]
        tot_ok += ok
        print(f"  {fam:12} {ok}/{n} = {100*ok//n}%", flush=True)
    print(f"\nOVERALL: {tot_ok}/{len(cases)} = {100*tot_ok//len(cases)}%", flush=True)
    (GOLDEN.parent / "golden_live_results.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    sys.exit(main())
