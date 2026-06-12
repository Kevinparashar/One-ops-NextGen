"""End-to-end health scorer for e2e_eval.json against the LIVE API.

Real-user phrasing is legitimately fuzzy, so this does NOT grade strict
route-correctness. It reports, per query: did it run end-to-end, what agent
handled it, and the final status — then aggregates execution HEALTH and the
agent distribution, and flags anything that errored or that refused while
looking in-domain (a real query getting turned away).

Runs in BATCHES (EVAL_BATCH) with a pause between them so the shared DB pool
stays healthy. Tenant T001, role service_desk_agent (executes uc01/02/03/08).
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
EVAL = Path(__file__).resolve().parent / "e2e_eval.json"


def classify(d: dict) -> tuple[str, str]:
    """Return (health, agents). health ∈ executed|refused|clarification|empty."""
    fr = (d.get("final_response") or "")
    status = (d.get("final_status") or "").lower()
    agents = sorted({s.get("agent_id") for s in (d.get("step_results") or [])
                     if isinstance(s, dict) and s.get("agent_id")})
    if isinstance(d.get("interrupt"), dict):
        agents.append("uc08_fulfillment")
        agents = sorted(set(agents))
    ag = ",".join(agents) or "(none)"
    low = fr.lower()
    if "out of my scope" in low or "within the itsm" in low:
        return "refused", ag
    if status in ("executed", "completed") or agents or isinstance(d.get("interrupt"), dict):
        return "executed", ag
    if status in ("clarification", "needs_clarification"):
        return "clarification", ag
    if not fr.strip():
        return "empty", ag
    return "clarification", ag           # non-task reply, no agent (greeting/etc.)


def call(case: dict) -> dict:
    req = urllib.request.Request(
        BASE, data=json.dumps({"message": case["query"]}).encode(),
        headers={"Content-Type": "application/json", "x-tenant-id": TENANT,
                 "x-user-id": "e2e-eval", "x-role": ROLE})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            health, ag = classify(json.load(r))
    except Exception as exc:                                      # noqa: BLE001
        health, ag = "ERROR", f"{type(exc).__name__}:{str(exc)[:24]}"
    return {**case, "health": health, "agents": ag}


def main() -> None:
    cases = json.loads(EVAL.read_text())
    results, done = [], 0
    n_batches = (len(cases) + BATCH - 1) // BATCH
    for bi in range(n_batches):
        chunk = cases[bi * BATCH:(bi + 1) * BATCH]
        print(f"\n--- BATCH {bi+1}/{n_batches} ({len(chunk)}) ---", flush=True)
        with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for r in ex.map(call, chunk):
                done += 1
                results.append(r)
                mark = {"executed": "OK ", "refused": "RF ", "clarification": "CL ",
                        "empty": "?? ", "ERROR": "XX "}.get(r["health"], "?? ")
                print(f"[{done:3}/{len(cases)}] {mark} {r['kind']:12} "
                      f"{r['health']:13} {r['agents'][:26]:26} :: {r['query'][:44]}",
                      flush=True)
        if bi < n_batches - 1:
            time.sleep(PAUSE)

    # ── aggregate ──
    health: dict[str, int] = {}
    bykind: dict[str, dict[str, int]] = {}
    agents: dict[str, int] = {}
    for r in results:
        health[r["health"]] = health.get(r["health"], 0) + 1
        bykind.setdefault(r["kind"], {}).setdefault(r["health"], 0)
        bykind[r["kind"]][r["health"]] += 1
        for a in r["agents"].split(","):
            agents[a] = agents.get(a, 0) + 1

    print("\n================ E2E EXECUTION HEALTH ================", flush=True)
    n = len(results)
    ok = health.get("executed", 0)
    print(f"  total: {n}", flush=True)
    for h in ("executed", "clarification", "refused", "empty", "ERROR"):
        if health.get(h):
            print(f"    {h:14} {health[h]:3}  ({100*health[h]//n}%)", flush=True)
    print(f"\n  end-to-end executed: {ok}/{n} = {100*ok//n}%", flush=True)

    print("\n  BY KIND (executed / total):", flush=True)
    for k in sorted(bykind):
        tot = sum(bykind[k].values())
        e = bykind[k].get("executed", 0)
        extra = " ".join(f"{h}={c}" for h, c in sorted(bykind[k].items()) if h != "executed")
        print(f"    {k:14} {e:3}/{tot:<3}  {extra}", flush=True)

    print("\n  AGENT DISTRIBUTION:", flush=True)
    for a, c in sorted(agents.items(), key=lambda x: -x[1]):
        print(f"    {a:26} {c}", flush=True)

    # flags: errors, and in-domain refusals (real query turned away)
    flags = [r for r in results if r["health"] in ("ERROR", "empty")
             or (r["health"] == "refused")]
    if flags:
        print(f"\n  FLAGGED ({len(flags)}) — errored / empty / refused:", flush=True)
        for r in flags:
            print(f"    [{r['health']:9}] {r['kind']:12} :: {r['query'][:50]}", flush=True)

    (EVAL.parent / "e2e_eval_results.json").write_text(json.dumps(results, indent=2))
    print(f"\n  wrote {EVAL.parent/'e2e_eval_results.json'}", flush=True)


if __name__ == "__main__":
    main()
