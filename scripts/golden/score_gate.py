"""Score CONTROL-GATE quality on gate_eval.json against the LIVE API.

The gate's decision is binary: REFUSE (out_of_scope) vs LET THROUGH (routed).
We detect a refusal the same way the boundary renders it ("out of my scope" /
"within the itsm"). Dual metric (the only honest way to read a scope gate):

  in_domain  -> correct iff NOT refused      (over-refusal is the failure)
  off_domain -> correct iff refused          (leak is the failure)
  boundary   -> reported, not scored

Prints each case as it resolves; ends with a confusion matrix + the two rates
that matter, plus every failure listed so they can be triaged.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import urllib.request
from pathlib import Path

BASE = os.environ.get("EVAL_BASE", "http://localhost:8000/api/chat")
WORKERS = int(os.environ.get("EVAL_WORKERS", "3"))   # pool-safe (shared pool)
EVAL = Path(__file__).resolve().parent / "gate_eval.json"


def gate_decision(d: dict) -> tuple[str, str]:
    """Return (decision, routed_agents) where decision in {refused, passed}."""
    fr = (d.get("final_response") or "").lower()
    if "out of my scope" in fr or "within the itsm" in fr:
        return "refused", "-"
    ag = sorted({s.get("agent_id") for s in (d.get("step_results") or [])
                 if isinstance(s, dict) and s.get("agent_id")})
    if isinstance(d.get("interrupt"), dict):
        ag.append("uc08_fulfillment")
    return "passed", (",".join(sorted(set(ag)))[:30] or "(boundary/clarify)")


def call(case: dict) -> dict:
    req = urllib.request.Request(
        BASE, data=json.dumps({"message": case["query"]}).encode(),
        headers={"Content-Type": "application/json", "x-tenant-id": "T001",
                 "x-user-id": "gate-eval", "x-role": "service_desk_agent"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            decision, routed = gate_decision(json.load(r))
    except Exception as exc:                                      # noqa: BLE001
        decision, routed = f"ERR:{str(exc)[:18]}", "-"
    label = case["label"]
    if label == "in_domain":
        ok = decision == "passed"
    elif label == "off_domain":
        ok = decision == "refused"
    else:                                                         # boundary
        ok = None
    return {**case, "decision": decision, "routed": routed, "ok": ok}


def main() -> None:
    cases = json.loads(EVAL.read_text())
    done = 0
    results = []
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r in ex.map(call, cases):
            done += 1
            results.append(r)
            mark = {True: "OK ", False: "XX ", None: "·· "}[r["ok"]]
            print(f"[{done:3}/{len(cases)}] {mark} {r['label']:10} "
                  f"{r['decision']:8} {r['routed']:18} :: {r['query'][:42]}",
                  flush=True)

    # ── confusion matrix + dual metric ──
    idn = [r for r in results if r["label"] == "in_domain"]
    off = [r for r in results if r["label"] == "off_domain"]
    bnd = [r for r in results if r["label"] == "boundary"]
    id_pass = sum(1 for r in idn if r["decision"] == "passed")
    id_ref = sum(1 for r in idn if r["decision"] == "refused")
    off_ref = sum(1 for r in off if r["decision"] == "refused")
    off_pass = sum(1 for r in off if r["decision"] == "passed")
    err = [r for r in results if str(r["decision"]).startswith("ERR")]

    def pct(a, b):
        return f"{100*a//b}%" if b else "n/a"

    print("\n================ CONTROL-GATE QUALITY ================", flush=True)
    print(f"  in_domain  : {len(idn)}   off_domain: {len(off)}   "
          f"boundary: {len(bnd)}   errors: {len(err)}", flush=True)
    print("\n  CONFUSION (gate decision):", flush=True)
    print(f"                    passed     refused", flush=True)
    print(f"    in_domain  :  {id_pass:5}      {id_ref:5}   "
          f"<- refused here = OVER-REFUSAL", flush=True)
    print(f"    off_domain :  {off_pass:5}      {off_ref:5}   "
          f"<- passed here = LEAK", flush=True)
    print("\n  THE TWO RATES THAT MATTER:", flush=True)
    print(f"    in-domain pass-through (not over-refused): "
          f"{id_pass}/{len(idn)} = {pct(id_pass,len(idn))}", flush=True)
    print(f"    off-domain refusal (junk kept out)       : "
          f"{off_ref}/{len(off)} = {pct(off_ref,len(off))}", flush=True)
    print(f"    over-refusal rate : {pct(id_ref,len(idn))}    "
          f"leak rate : {pct(off_pass,len(off))}", flush=True)

    if bnd:
        b_ref = sum(1 for r in bnd if r["decision"] == "refused")
        print(f"\n  BOUNDARY (not scored): {b_ref}/{len(bnd)} refused, "
              f"{len(bnd)-b_ref} passed", flush=True)
        for r in bnd:
            print(f"    {r['decision']:8} :: {r['query']}", flush=True)

    fails = [r for r in results if r["ok"] is False]
    if fails:
        print(f"\n  FAILURES ({len(fails)}):", flush=True)
        for r in fails:
            kind = "OVER-REFUSAL" if r["label"] == "in_domain" else "LEAK"
            print(f"    [{kind:12}] {r['family']:16} {r['decision']:8} "
                  f":: {r['query']}", flush=True)

    (EVAL.parent / "gate_eval_results.json").write_text(json.dumps(results, indent=2))
    print(f"\n  wrote {EVAL.parent/'gate_eval_results.json'}", flush=True)


if __name__ == "__main__":
    main()
