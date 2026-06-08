#!/usr/bin/env python3
"""Routing eval harness — the routing "report card" (rigorous edition).

Runs a labelled set of (query -> expected agent) through the LIVE router via
POST /api/chat (fresh session per query), then reports overall accuracy,
per-agent accuracy, a confusion matrix, and the genuine mis-routes. Segments
CHAT-routable agents (scored) from BUTTON-ONLY capabilities (uc05/uc08).

Run (server up):  .venv/bin/python scripts/routing_eval.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict

BASE = os.getenv("ONEOPS_EVAL_BASE", "http://localhost:8765")
HDR = {
    "content-type": "application/json",
    "x-tenant-id": os.getenv("ONEOPS_EVAL_TENANT", "T001"),
    "x-user-id": os.getenv("ONEOPS_EVAL_USER", "oneops"),
    "x-role": os.getenv("ONEOPS_EVAL_ROLE", "service_desk_agent"),
}
NONE = "NONE"
A1, A2, A3 = "uc01_summarization", "uc02_similar_tickets", "uc03_kb_lookup"
A5, A8 = "uc05_triage", "uc08_fulfillment"

DATASET: list[tuple[str, object, str]] = [
    ("summarize INC0001001", A1, "chat"),
    ("summarise incident INC0001021", A1, "chat"),
    ("give me a summary of PRB0002003", A1, "chat"),
    ("what's the status of INC0001001", A1, "chat"),
    ("what is the priority of INC0001021", A1, "chat"),
    ("who is assigned to INC0001001", A1, "chat"),
    ("what's the current state of CHG0004021", A1, "chat"),
    ("details on INC0001001", A1, "chat"),
    ("tell me about INC0001021", A1, "chat"),
    ("overview of PRB0002003", A1, "chat"),
    ("what happened with INC0001001", A1, "chat"),
    ("fill me in on INC0001021", A1, "chat"),
    ("what's the deal with INC0001001", A1, "chat"),
    ("catch me up on PRB0002003", A1, "chat"),
    ("INC0001001", A1, "chat"),

    ("any similar tickets to INC0001001", A2, "chat"),
    ("find tickets like INC0001021", A2, "chat"),
    ("show me duplicates of INC0001001", A2, "chat"),
    ("has this happened before on INC0001021", A2, "chat"),
    ("are there related incidents to INC0001001", A2, "chat"),
    ("other tickets with the same issue as INC0001001", A2, "chat"),
    ("find similar cases to PRB0002003", A2, "chat"),
    ("is INC0001001 a duplicate of anything", A2, "chat"),
    ("previous occurrences of the issue in INC0001021", A2, "chat"),

    ("how do I reset my VPN token", A3, "chat"),
    ("is there a KB article for VPN error 809", A3, "chat"),
    ("documentation on mapping a network drive", A3, "chat"),
    ("how to set up MFA", A3, "chat"),
    ("knowledge base article for password reset", A3, "chat"),
    ("how do I fix Outlook error 0x800ccc0e", A3, "chat"),
    ("steps to configure Outlook on a new laptop", A3, "chat"),
    ("guide for connecting to corporate wifi", A3, "chat"),
    ("how to install the company VPN client", A3, "chat"),
    ("any runbook for an SSO login loop", A3, "chat"),
    ("how do I clear my browser cache", A3, "chat"),
    ("troubleshooting steps for a slow laptop", A3, "chat"),
    ("knowledge article about reporting phishing", A3, "chat"),

    ("any information related to INC0001001", A3, "chat"),
    ("what do we know about INC0001001", A1, "chat"),
    ("how should I resolve INC0001001", A3, "chat"),
    ("anything documented for this VPN issue", A3, "chat"),
    ("how to fix the problem reported in INC0001001", A3, "chat"),
    ("is there a fix documented for INC0001021", A3, "chat"),
    ("find me a solution for the issue in INC0001001", A3, "chat"),

    ("summarize INC0001001 and find similar tickets", {A1, A2}, "chat"),
    ("summarize INC0001001 and any related KB articles", {A1, A3}, "chat"),
    ("what's wrong with INC0001001 and how do I fix it", {A1, A3}, "chat"),
    ("give me details and similar tickets for INC0001021", {A1, A2}, "chat"),

    ("tell me a joke", NONE, "chat"),
    ("what's the weather today", NONE, "chat"),
    ("who won the cricket match last night", NONE, "chat"),
    ("write me a poem about Mondays", NONE, "chat"),
    ("what is 2 + 2", NONE, "chat"),
    ("translate hello into French", NONE, "chat"),
    ("what's the capital of France", NONE, "chat"),
    ("recommend a good restaurant nearby", NONE, "chat"),
    ("how do I cook pasta", NONE, "chat"),
    ("what time is it in Tokyo", NONE, "chat"),

    ("where should ticket INC0001001 go", A5, "button"),
    ("triage INC0001001", A5, "button"),
    ("what priority should INC0001001 be", A5, "button"),
    ("which team should handle INC0001021", A5, "button"),
    ("classify and route INC0001001", A5, "button"),
    ("re-triage INC0001021", A5, "button"),
    ("I need a new laptop", A8, "button"),
    ("request an Adobe Photoshop license", A8, "button"),
    ("onboard new employee John Smith starting March 15", A8, "button"),
    ("I need access to the finance share", A8, "button"),
    ("set up a new AWS account for me", A8, "button"),
    ("order a second monitor for my desk", A8, "button"),
]


def _post(message: str, session_id: str) -> dict:
    req = urllib.request.Request(
        BASE + "/api/chat",
        data=json.dumps({"message": message, "session_id": session_id}).encode(),
        headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def _routed_agents(resp: dict) -> list[str]:
    seen: list[str] = []
    for s in (resp.get("step_results") or []):
        aid = s.get("agent_id")
        if aid and aid not in seen:
            seen.append(aid)
    return seen


def _is_refusal(resp: dict) -> bool:
    fr = (resp.get("final_response") or "").lower()
    fs = (resp.get("final_status") or "").lower()
    if fs in {"no_match", "clarification_required"}:
        return True
    return ("out of" in fr and "scope" in fr) or "no matching" in fr or "not found" in fr


def _verdict(expected: object, routed: list[str], refusal: bool) -> tuple[bool, str]:
    actual = "+".join(routed) if routed else NONE
    if expected == NONE:
        return (refusal or not routed), actual
    if isinstance(expected, (set, frozenset, list, tuple)):
        return set(expected).issubset(set(routed)), actual
    return (expected in routed), actual


def main() -> int:
    try:
        urllib.request.urlopen(  # noqa: S310
            urllib.request.Request(BASE + "/", method="GET"), timeout=5)
    except OSError as exc:
        print(f"✗ Cannot reach the server at {BASE} ({exc}).")
        return 2

    chat_ok = chat_n = chat_err = 0
    per_agent: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    confusion: dict[tuple[str, str], int] = defaultdict(int)
    misroutes: list[str] = []
    button_lines: list[str] = []
    chat_total = sum(1 for _, _, c in DATASET if c == "chat")
    print(f"Rigorous routing eval — {chat_total} chat (+{len(DATASET) - chat_total} button) "
          f"against {BASE}\n")

    for i, (query, expected, channel) in enumerate(DATASET):
        exp_label = expected if isinstance(expected, str) else "+".join(sorted(expected))
        try:
            resp = _post(query, f"eval-{i}")
        except Exception as exc:  # noqa: BLE001
            if channel == "chat":
                chat_err += 1
                chat_n += 1
                confusion[(exp_label, "ERROR")] += 1
            misroutes.append(f"  ERROR  {query!r}  ({exc})")
            print(f"  ! {query!r} → ERROR ({exc})")
            continue
        routed = _routed_agents(resp)
        ok, actual = _verdict(expected, routed, _is_refusal(resp))
        if channel == "button":
            button_lines.append(f"  {query!r}  [{exp_label}, button-only]  chat→{actual}")
            print(f"  · {query!r}  [button-only]  chat→{actual}")
            continue
        chat_n += 1
        per_agent[exp_label][1] += 1
        per_agent[exp_label][0] += int(ok)
        confusion[(exp_label, exp_label if ok else actual)] += 1
        chat_ok += int(ok)
        print(f"  {'✓' if ok else '✗'} {query!r}  expect={exp_label}  got={actual}")
        if not ok:
            misroutes.append(f"  {query!r}\n      expected: {exp_label}   got: {actual}")

    scored = chat_n - chat_err
    acc = (chat_ok / scored * 100.0) if scored else 0.0
    print("\n" + "=" * 66)
    print(f"CHAT ROUTING ACCURACY: {chat_ok}/{scored} = {acc:.1f}%"
          + (f"   ({chat_err} errors)" if chat_err else ""))
    print("=" * 66)
    print("\nPER-AGENT ACCURACY (chat-routable):")
    for agent in sorted(per_agent):
        ok, n = per_agent[agent]
        print(f"  {agent:<26} {ok}/{n} = {(ok / n * 100.0) if n else 0:.0f}%")
    print("\nCONFUSION MATRIX  (expected → got : count); off-diagonal = mis-route")
    for e in sorted({x for x, _ in confusion}):
        rows = sorted(((a, c) for (ee, a), c in confusion.items() if ee == e), key=lambda x: -x[1])
        print(f"  {e:<26} → " + "  ".join(f"{a}:{c}{' ◄' if a != e else ''}" for a, c in rows))
    if misroutes:
        print("\nCHAT MIS-ROUTES / ERRORS:")
        for m in misroutes:
            print(m)
    if button_lines:
        print("\nBUTTON-ONLY (not chat-wired today — informational):")
        for b in button_lines:
            print(b)
    print()
    return 0 if (chat_err == 0 and chat_ok == scored) else 1


if __name__ == "__main__":
    sys.exit(main())
