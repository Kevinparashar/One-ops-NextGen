"""Score ROUTE-CORRECTNESS on routing_eval.json against the LIVE API.

Runs in BATCHES (env EVAL_BATCH, default 40) with a short pause between batches
so the shared DB pool stays healthy. Tenant T001, role service_desk_agent — a
role with execute permission for uc01/uc02/uc03/uc08, so every query actually
runs (nothing denied). uc05 is not in the set (API-only agent).

Correct iff the EXPECTED agent is among the agents that ran. kb_then_sr is
correct iff uc03_kb_lookup ran (KB surfaced first, per the 2026-06-11 decision).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("EVAL_BASE", "http://localhost:8000/api/chat")
WORKERS = int(os.environ.get("EVAL_WORKERS", "3"))
BATCH = int(os.environ.get("EVAL_BATCH", "40"))
PAUSE = float(os.environ.get("EVAL_PAUSE", "3"))
TENANT = os.environ.get("EVAL_TENANT", "T001")
ROLE = os.environ.get("EVAL_ROLE", "service_desk_agent")
EVAL = Path(__file__).resolve().parent / "routing_eval.json"

EXPECT = {"uc01": "uc01_summarization", "uc02": "uc02_similar_tickets",
          "uc03": "uc03_kb_lookup", "uc08": "uc08_fulfillment"}


def agents(d: dict) -> set[str]:
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
        headers={"Content-Type": "application/json", "x-tenant-id": TENANT,
                 "x-user-id": "routing-eval", "x-role": ROLE})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            a = agents(json.load(r))
    except Exception as exc:                                      # noqa: BLE001
        a = {f"ERR:{str(exc)[:18]}"}
    exp = case["expected"]
    if exp == "kb_then_sr":
        ok = "uc03_kb_lookup" in a
    else:
        ok = EXPECT[exp] in a
    return {**case, "actual": sorted(a), "ok": ok}


def main() -> None:
    cases = json.loads(EVAL.read_text())
    results = []
    done = 0
    n_batches = (len(cases) + BATCH - 1) // BATCH
    for bi in range(n_batches):
        chunk = cases[bi * BATCH:(bi + 1) * BATCH]
        print(f"\n--- BATCH {bi+1}/{n_batches} ({len(chunk)} queries) ---", flush=True)
        with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for r in ex.map(call, chunk):
                done += 1
                results.append(r)
                mark = "OK " if r["ok"] else "XX "
                print(f"[{done:3}/{len(cases)}] {mark} exp={r['expected']:10} "
                      f"got={','.join(r['actual'])[:34]:34} :: {r['query'][:40]}",
                      flush=True)
        if bi < n_batches - 1:
            time.sleep(PAUSE)

    # ── tally ──
    tally: dict[str, list[int]] = {}
    for r in results:
        t = tally.setdefault(r["family"], [0, 0])
        t[1] += 1
        t[0] += 1 if r["ok"] else 0
    print("\n================ ROUTE CORRECTNESS ================", flush=True)
    tot_ok = 0
    for fam in sorted(tally):
        ok, n = tally[fam]
        tot_ok += ok
        print(f"  {fam:12} {ok:3}/{n:<3} = {100*ok//n}%", flush=True)
    print(f"\n  OVERALL: {tot_ok}/{len(cases)} = {100*tot_ok//len(cases)}%", flush=True)
    err = [r for r in results if any(str(a).startswith("ERR") for a in r["actual"])]
    print(f"  errors: {len(err)}", flush=True)

    fails = [r for r in results if not r["ok"]]
    if fails:
        print(f"\n  MISROUTES ({len(fails)}):", flush=True)
        for r in fails:
            print(f"    exp={r['expected']:10} got={','.join(r['actual'])[:30]:30} "
                  f":: {r['query'][:46]}", flush=True)

    (EVAL.parent / "routing_eval_results.json").write_text(json.dumps(results, indent=2))
    print(f"\n  wrote {EVAL.parent/'routing_eval_results.json'}", flush=True)


if __name__ == "__main__":
    main()
