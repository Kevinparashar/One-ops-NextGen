#!/usr/bin/env python3
"""Routing consistency + coverage devil's-play (2026-06-02).

Two checks:
  1. CONSISTENCY — run key queries 3x each in FRESH sessions (different
     session_id → turn-cache miss → real pipeline each time). The outcome
     (final_status + tool + match/no-match) MUST be identical every run.
     This is the regression test for the temperature=0 boundary fix +
     the deterministic KB-id / fallback fixes.
  2. COVERAGE — 50+ distinct queries across every edge case; assert no
     crash and the expected class of outcome.

Run:  .venv/bin/python scripts/routing_consistency_devils.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "http://localhost:8765"
HDR = {"content-type": "application/json", "x-tenant-id": "T001",
       "x-user-id": "oneops", "x-role": "service_desk_agent"}
_n = [0]
_fail: list[str] = []


def _post(message: str, session_id: str) -> dict:
    req = urllib.request.Request(
        BASE + "/api/chat",
        data=json.dumps({"message": message, "session_id": session_id}).encode(),
        headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def _shape(d: dict) -> tuple[str, str, str]:
    """A coarse outcome signature: (final_status, tool, match-class)."""
    steps = d.get("step_results") or []
    tool = steps[0].get("tool_id") if steps else ""
    fr = (d.get("final_response") or "").lower()
    if "out of" in fr and "scope" in fr:
        cls = "out_of_scope"
    elif "no matching" in fr or "no knowledge-base article" in fr or "not found" in fr:
        cls = "no_match"
    else:
        cls = "answered"
    return (d.get("final_status", ""), tool or "", cls)


def check(cond: bool, label: str, detail: str = "") -> None:
    _n[0] += 1
    if cond:
        print(f"  ✓ {label}")
    else:
        _fail.append(label)
        print(f"  ✗ {label}  {('— ' + detail) if detail else ''}")


def consistency() -> None:
    print("\n=== CONSISTENCY: same query, 3 fresh sessions → identical outcome ===")
    queries = [
        "VPN error 809",
        "how to set vpn password",
        "database connection fails",
        "how to fix VPN disconnects on Wi-Fi handoff",
        "SAP ERP login is slow",
        "vpn error 809 for INC0001021",
        "tell me a joke",
        "how to reset my chair",
        "mfa reset propagation",
        "reduce database query latency",
    ]
    for qi, q in enumerate(queries):
        # 3 fresh sessions, run concurrently
        with ThreadPoolExecutor(max_workers=3) as ex:
            res = list(ex.map(lambda i: _post(q, f"cons-{qi}-{i}"), range(3)))
        shapes = [_shape(r) for r in res]
        same = len(set(shapes)) == 1
        check(same, f"consistent: {q!r}",
              f"got {set(shapes)}")
        print(f"      → {shapes[0]}")


def coverage() -> None:
    print("\n=== COVERAGE: 50+ distinct queries, all edge cases ===")
    # (query, expected_class | None=just-no-crash)
    cases = [
        # KB hits (articles exist)
        ("VPN error 809", "answered"),
        ("how to fix VPN disconnects on Wi-Fi handoff", "answered"),
        ("SAP ERP login is slow", "answered"),
        ("database replication lag", "answered"),
        ("reduce database query latency", "answered"),
        ("MFA reset propagation guide", "answered"),
        ("VPN keeps dropping when switching networks", "answered"),
        # KB content-gap → graceful no_match (no such article)
        ("how to set vpn password", None),
        ("database connection fails", "no_match"),
        # summaries (UC-1)
        ("summarize INC0001001", "answered"),
        ("summarize REQ0002001", "answered"),
        ("summarize CHG0004001", "answered"),
        ("summarize PBM0003001", "answered"),
        ("summarize AST0001001", "answered"),
        ("INC0001001", "answered"),
        # missing / invalid
        ("summarize INC9999999", "no_match"),
        ("summarize", None),
        ("   ", None),
        ("a", None),
        # out of scope
        ("tell me a joke", "out_of_scope"),
        ("what is the weather today", "out_of_scope"),
        ("who won the world cup", "out_of_scope"),
        # similar / triage / multi
        ("find tickets similar to INC0001001", "answered"),
        ("find tickets similar to INC0001002", "answered"),
        ("summarize INC0001001 and find similar tickets", "answered"),
        ("summarize INC0001001 and any KB about VPN", "answered"),
        # id-in-text KB (must not break)
        ("vpn error 809 for INC0001021", None),
        ("VPN ERROR 809", "answered"),
        ("vpn error 809???", "answered"),
        # typos / casing
        ("databse conection fials", None),
        ("sap erp login slow", "answered"),
        # KB by ticket / article id
        ("KB0005001", None),
        ("any KB linked to INC0001001", None),
        # cross-service summaries
        ("summarize CI0000001", "answered"),
        ("summarize SR0002001", "answered"),
        # field-read shapes (standalone — no focus)
        ("priority of INC0001001", "answered"),
        ("who is INC0001001 assigned to", "answered"),
        ("status of CHG0004001", "answered"),
        # longer / natural phrasings
        ("my vpn keeps disconnecting every time I move between floors", "answered"),
        ("the SAP system is really slow at login during peak hours", "answered"),
        ("how do I troubleshoot a slow database", "answered"),
        ("is there any guidance on MFA enrollment", "answered"),
        # more KB topics
        ("password reset steps", None),
        ("email delivery delayed", None),
        ("printer not working", None),
        ("how to request a new laptop", None),
        ("wifi handoff drops", "answered"),
        ("troubleshoot VPN error", "answered"),
        ("known error for VPN", None),
        ("workaround for ERP latency", None),
        ("INC0001002", "answered"),
        ("summarize the incident INC0001003", "answered"),
    ]
    def run(case):
        q, exp = case
        try:
            d = _post(q, f"cov-{abs(hash(q))%100000}")
        except Exception as exc:                            # noqa: BLE001
            return (q, exp, None, str(exc)[:60])
        return (q, exp, _shape(d), "")
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(run, cases))
    for q, exp, shape, err in results:
        if shape is None:
            check(False, f"no-crash: {q!r}", err)
            continue
        status, tool, cls = shape
        check(status not in ("", "failed") or cls == "no_match",
              f"valid outcome: {q!r}", f"{shape}")
        if exp is not None:
            check(cls == exp, f"expected {exp}: {q!r}", f"got {cls} {shape}")


if __name__ == "__main__":
    try:
        consistency()
        coverage()
    except Exception as exc:                                # noqa: BLE001
        print(f"\nHARNESS ERROR: {exc}")
        sys.exit(2)
    total = _n[0]
    print(f"\n================  {total - len(_fail)}/{total} checks passed  ================")
    if _fail:
        print(f"{len(_fail)} FAILED:")
        for f in _fail:
            print("  -", f)
        sys.exit(1)
    print("ALL GREEN")
