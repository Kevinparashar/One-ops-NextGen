#!/usr/bin/env python3
"""Devil's-play for the live streaming activity view (2026-06-02).

Adversarially probes /api/chat/stream and /api/fast/{uc}/stream for the
invariants a live "which agents + tools are running" UI depends on:

  * the stream ALWAYS terminates with exactly one `final` (no hang),
  * `turn_start` is first, `final` is last,
  * every `tool_start` has a matching `tool_done` (no orphan spinners),
  * each `tool_start` carries a non-empty human action line + tool_id,
  * graceful behaviour on missing ids, bad button input, field-reads,
  * two concurrent streams never cross-contaminate (sink keyed by req id),
  * the non-streaming /api/chat and /api/fast still work (regression).

Run:  .venv/bin/python scripts/uc_stream_devils_play.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# Repeated literals → constants (sonar S1192).
_API_CHAT_STREAM = "/api/chat/stream"
_API_FAST_UC01_SUMMARIZATION_STREAM = "/api/fast/uc01_summarization/stream"

BASE = "http://localhost:8765"
HDR = {
    "content-type": "application/json",
    "x-tenant-id": "T001", "x-user-id": "oneops",
    "x-role": "service_desk_agent",
}

_fail: list[str] = []
_pass = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"  ✓ {label}")
    else:
        _fail.append(label)
        print(f"  ✗ {label}  {('— ' + detail) if detail else ''}")


def stream(path: str, body: dict, timeout: float = 90.0) -> list[dict]:
    """POST and read the NDJSON event stream into a list of events."""
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), headers=HDR, method="POST")
    events: list[dict] = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:                       # iterates lines as they arrive
            line = raw.decode().strip()
            if line:
                events.append(json.loads(line))
    return events


def invariants(events: list[dict], label: str, *, expect_tools: bool) -> dict | None:
    check(bool(events), f"{label}: stream produced events")
    if not events:
        return None
    check(events[0].get("type") == "turn_start", f"{label}: first event is turn_start",
          str(events[0].get("type")))
    finals = [e for e in events if e.get("type") == "final"]
    check(len(finals) == 1, f"{label}: exactly one final", f"got {len(finals)}")
    check(events[-1].get("type") == "final", f"{label}: final is last")
    starts = [e for e in events if e.get("type") == "tool_start"]
    dones = [e for e in events if e.get("type") == "tool_done"]
    start_keys = {(e.get("step_id"), e.get("agent_id"), e.get("tool_id")) for e in starts}
    done_keys = {(e.get("step_id"), e.get("agent_id"), e.get("tool_id")) for e in dones}
    check(start_keys == done_keys,
          f"{label}: every tool_start has a matching tool_done",
          f"starts={len(starts)} dones={len(dones)}")
    for e in starts:
        check(bool((e.get("action") or "").strip()) and bool(e.get("tool_id")),
              f"{label}: tool_start has action + tool_id", e.get("tool_id", ""))
    if expect_tools:
        check(len(starts) >= 1, f"{label}: at least one tool ran")
    return finals[0].get("payload") if finals else None


def main() -> None:
    print("\n=== A. chat multi-step stream ===")
    ev = stream(_API_CHAT_STREAM,
                {"message": "summarize INC0001001 and find similar tickets",
                 "session_id": "dp-a"})
    p = invariants(ev, "A", expect_tools=True)
    check(p is not None and p.get("final_status") == "executed",
          "A: final_status executed", str(p and p.get("final_status")))
    check(len([e for e in ev if e["type"] == "tool_start"]) >= 2,
          "A: multi-step → 2+ tools")

    print("\n=== B. chat missing ticket (graceful) ===")
    ev = stream(_API_CHAT_STREAM,
                {"message": "summarize INC9999999", "session_id": "dp-b"})
    p = invariants(ev, "B", expect_tools=False)
    fr = (p or {}).get("final_response", "").lower()
    check(p is not None, "B: got a final (no hang/crash)")
    check("not" in fr or "no " in fr or (p or {}).get("final_status") != "executed"
          or "9999999" in fr, "B: missing id handled gracefully", fr[:80])

    print("\n=== C. chat field-read via stream ===")
    ev = stream(_API_CHAT_STREAM,
                {"message": "give me sla of INC0001001", "session_id": "dp-c"})
    p = invariants(ev, "C", expect_tools=True)
    outcomes = [s.get("output", {}).get("outcome")
                for s in (p or {}).get("step_results", [])]
    check("field_read" in outcomes, "C: field-read still routes correctly", str(outcomes))

    print("\n=== D. button bad input → clarification, NO tool events ===")
    ev = stream(_API_FAST_UC01_SUMMARIZATION_STREAM,
                {"inputs": {"ticket_id": "garbage"}, "session_id": "dp-d"})
    p = invariants(ev, "D", expect_tools=False)
    check(not [e for e in ev if e["type"] == "tool_start"],
          "D: no tool_start for rejected input")
    check((p or {}).get("final_status") == "clarification",
          "D: final is a clarification", str(p and p.get("final_status")))

    print("\n=== E. button valid stream ===")
    ev = stream(_API_FAST_UC01_SUMMARIZATION_STREAM,
                {"inputs": {"ticket_id": "PBM0003007", "service_id": "problem"},
                 "session_id": "dp-e"})
    p = invariants(ev, "E", expect_tools=True)
    check((p or {}).get("final_status") == "executed", "E: executed")

    print("\n=== F. two concurrent streams don't cross-contaminate ===")
    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(stream, _API_CHAT_STREAM,
                       {"message": "summarize INC0001002", "session_id": "dp-f1"})
        f2 = ex.submit(stream, _API_FAST_UC01_SUMMARIZATION_STREAM,
                       {"inputs": {"ticket_id": "CHG0004001", "service_id": "change"},
                        "session_id": "dp-f2"})
        e1, e2 = f1.result(), f2.result()
    rid1 = next((e["request_id"] for e in e1 if e["type"] == "turn_start"), "1")
    rid2 = next((e["request_id"] for e in e2 if e["type"] == "turn_start"), "2")
    p1 = next((e["payload"] for e in e1 if e["type"] == "final"), {})
    p2 = next((e["payload"] for e in e2 if e["type"] == "final"), {})
    check(rid1 != rid2, "F: distinct request_ids")
    check(p1.get("request_id") == rid1, "F: stream1 final matches its turn_start")
    check(p2.get("request_id") == rid2, "F: stream2 final matches its turn_start")

    print("\n=== G. regression: non-streaming endpoints still work ===")
    def post(path, body):
        req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                     headers=HDR, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    g1 = post("/api/chat", {"message": "summarize INC0001001", "session_id": "dp-g1"})
    check(g1.get("final_status") == "executed", "G: /api/chat one-shot works")
    g2 = post("/api/fast/uc01_summarization",
              {"inputs": {"ticket_id": "INC0001001", "service_id": "incident"},
               "session_id": "dp-g2"})
    check(g2.get("final_status") == "executed", "G: /api/fast one-shot works")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                # noqa: BLE001
        print(f"\nHARNESS ERROR: {exc}")
        sys.exit(2)
    print(f"\n================  {_pass} passed, {len(_fail)} failed  ================")
    if _fail:
        print("FAILURES:")
        for f in _fail:
            print("  -", f)
        sys.exit(1)
    print("ALL GREEN")
    sys.exit(0)
