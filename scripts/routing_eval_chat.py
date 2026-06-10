#!/usr/bin/env python3
"""Full-pipeline routing eval via /api/chat — the REAL entry point.

Sends each query to the live /api/chat (control_gate → decompose → route →
Stage-3 filter → rerank → execute) and scores the routed agent set. This is the
ONLY fully-faithful path — it includes the control_gate (off-domain) and the
decomposer (sets), which the offline + route()-only harnesses skipped.

Scope: chat-routable scenarios (uc01/02/03 + sets + off-domain). uc05/uc08 are
API/button-only (Stage-3 filters them from chat) and are excluded — tested on
their own path. Reuses the UNSEEN real-user dataset from routing_eval100.
Parallelized with a thread pool (each /api/chat call is blocking).

Run (server up):  .venv/bin/python scripts/routing_eval_chat.py [--workers 10]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

from routing_eval100 import DATASET as _ALL  # noqa: E402
from routing_eval100 import NONE, _s, _verdict

DATASET = [(q, e, c) for (q, e, c) in _ALL if c not in ("uc05", "uc08")]

BASE = os.getenv("ONEOPS_EVAL_BASE", "http://localhost:8765")
HDR = {"content-type": "application/json",
       "x-tenant-id": os.getenv("ONEOPS_EVAL_TENANT", "T001"),
       "x-user-id": "oneops",
       "x-role": os.getenv("ONEOPS_EVAL_ROLE", "service_desk_agent")}


def _post(message: str, sid: str) -> set[str]:
    req = urllib.request.Request(
        BASE + "/api/chat",
        data=json.dumps({"message": message, "session_id": sid}).encode(),
        headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:        # noqa: S310
        resp = json.loads(r.read().decode())
    seen: set[str] = set()
    for s in (resp.get("step_results") or []):
        a = s.get("agent_id")
        if a:
            seen.add(a)
    return seen


# Unique per-process run id → fresh session ids → never a chat-turn cache hit
# (the cache key includes session_id; reusing ids served stale results).
_RUN = os.getpid()


def _run_one(arg: tuple) -> tuple:
    i, q, exp, cat = arg
    try:
        chosen = _post(q, f"chateval-{_RUN}-{i}")
        return (q, exp, cat, chosen, None)
    except Exception as e:  # noqa: BLE001
        return (q, exp, cat, set(), str(e)[:140])


def _check_server() -> int | None:
    """Return a non-zero exit code if the server is unreachable, else None."""
    try:
        urllib.request.urlopen(  # noqa: S310
            urllib.request.Request(BASE + "/", method="GET"), timeout=5)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ server unreachable at {BASE} ({exc})")
        return 2
    return None


def _format_misroute(
    q: str, exp: object, cat: str, chosen: set, err: str | None,
) -> str:
    """One mis-route line: expected vs got (got = ERROR text / chosen set / none)."""
    if isinstance(exp, (set, list, tuple)):
        exp_l = _s("+".join(sorted(exp)))
    elif exp != NONE:
        exp_l = _s(exp)
    else:
        exp_l = "none"
    got_l = ("ERROR:" + err) if err else ("+".join(sorted(_s(a) for a in chosen)) or "none")
    return f"  ✗ {q!r}\n      want {exp_l}  got {got_l}  [{cat}]"


def _summarize(results: list) -> int:
    """Fold per-query results into overall + by-category accuracy, print the
    report, and return the exit code (0 iff every query routed correctly)."""
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
    print(f"=== FULL-PIPELINE routing via /api/chat — {n} chat-routable UNSEEN queries ===\n")
    print(f"OVERALL: {ok_total}/{n} = {ok_total/n*100:.1f}%\n")
    print("BY CATEGORY:")
    for cat in sorted(by_cat):
        ok, tot = by_cat[cat]
        print(f"  {cat:<16} {ok}/{tot} = {ok/tot*100:3.0f}%")
    if misroutes:
        print(f"\nMISROUTES ({len(misroutes)}):")
        print("\n".join(misroutes))
    return 0 if ok_total == n else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()
    rc = _check_server()
    if rc is not None:
        return rc
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(_run_one,
                              [(i, q, e, c) for i, (q, e, c) in enumerate(DATASET)]))
    return _summarize(results)


if __name__ == "__main__":
    sys.exit(main())
