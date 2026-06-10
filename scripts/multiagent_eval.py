#!/usr/bin/env python3
"""Multi-agent routing eval — FULL funnel (decompose → route), via /api/chat.

The offline retrieve+rerank eval under-tests SETS because it skips the
decomposer that splits "summarize X and find docs" into two sub-queries. This
hits the LIVE /api/chat (full funnel, incl. decompose) and checks the routed
SET of agents. UNSEEN queries, new ids — all chat-routable combos (uc01/02/03;
uc05/uc08 are button-only so excluded from sets).

A query passes if the expected agent set is a SUBSET of the routed agents.

Run (server up):  .venv/bin/python scripts/multiagent_eval.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = os.getenv("ONEOPS_EVAL_BASE", "http://localhost:8765")
HDR = {"content-type": "application/json", "x-tenant-id": "T001",
       "x-user-id": "oneops", "x-role": "service_desk_agent"}
A1, A2, A3 = "uc01_summarization", "uc02_similar_tickets", "uc03_kb_lookup"

# (query, expected set) — UNSEEN phrasings + new ids; all combos of the
# chat-routable agents. First 6 are the originals from routing_eval50; the rest
# are new, to grow the multi-agent sample.
DATASET: list[tuple[str, set[str]]] = [
    ("summarize INC0009012 and pull similar ones", {A1, A2}),
    ("give me CHG0006014 details and any docs for it", {A1, A3}),
    ("what's wrong with INC0009088 and how do I resolve it", {A1, A3}),
    ("recap REQ0005033 and check it for duplicates", {A1, A2}),
    ("explain PRB0003021 and find the runbook for it", {A1, A3}),
    ("show tickets similar to INC0009012 and any related KB", {A2, A3}),
    # ── new, to grow the sample ──
    ("give me the status of INC0009100 and any past similar incidents", {A1, A2}),
    ("what does CHG0006200 say and is there a guide for it", {A1, A3}),
    ("summarize PRB0003100 and surface duplicates", {A1, A2}),
    ("tell me about REQ0005200 and any documentation on it", {A1, A3}),
    ("details on AST0002100 plus comparable cases", {A1, A2}),
    ("what happened in INC0009101 and find the KB article for it", {A1, A3}),
    ("show me tickets like CHG0006201 and the runbook for it", {A2, A3}),
    ("current state of PRB0003101 and related past problems", {A1, A2}),
    ("describe INC0009103 and documentation to resolve it", {A1, A3}),
    ("overview of INC0009102, any duplicates, and a fix doc", {A1, A2, A3}),
]


def _post(message: str, sid: str) -> dict:
    req = urllib.request.Request(
        BASE + "/api/chat",
        data=json.dumps({"message": message, "session_id": sid}).encode(),
        headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:        # noqa: S310
        return json.loads(r.read().decode())


def _routed(resp: dict) -> list[str]:
    seen: list[str] = []
    for s in (resp.get("step_results") or []):
        a = s.get("agent_id")
        if a and a not in seen:
            seen.append(a)
    return seen


def _s(a: str) -> str:
    return a.replace("uc0", "u").replace("_summarization", "").replace(
        "_similar_tickets", "").replace("_kb_lookup", "")


def main() -> int:
    try:
        urllib.request.urlopen(  # noqa: S310
            urllib.request.Request(BASE + "/", method="GET"), timeout=5)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ server unreachable at {BASE} ({exc})")
        return 2

    n = len(DATASET)
    ok = 0
    misses: list[str] = []
    print(f"=== multi-agent routing (FULL funnel via /api/chat) — {n} unseen sets ===\n")
    for i, (q, exp) in enumerate(DATASET):
        try:
            routed = set(_routed(_post(q, f"ma-{i}")))
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {q[:48]!r} ERROR {exc}")
            misses.append(f"  ERROR {q!r}")
            continue
        good = exp.issubset(routed)
        ok += int(good)
        exp_l = "+".join(sorted(_s(a) for a in exp))
        got_l = "+".join(sorted(_s(a) for a in routed)) or "none"
        print(f"  {'✓' if good else '✗'} {q[:50]!r:<52} want={exp_l:<9} got={got_l}")
        if not good:
            misses.append(f"  ✗ {q!r}  want {exp_l}  got {got_l}")
    print("\n" + "=" * 60)
    print(f"MULTI-AGENT (full funnel): {ok}/{n} = {ok/n*100:.1f}%")
    print("=" * 60)
    if misses:
        print("\nMISSES:")
        print("\n".join(misses))
    return 0 if ok == n else 1


if __name__ == "__main__":
    sys.exit(main())
