"""Golden routing eval — 40 varied real-phrasing queries, 5+ per UC kind plus
the hard axis-A/B, multi-intent, catalog-vs-broken boundary, and off-domain
edges. Asserts each query's routed agent(s) match expectation. Production
validation for any router/disambiguator change.

Run: EVAL_TENANT=T001 EVAL_ROLE=service_desk_agent python scripts/golden_routing_eval.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("EVAL_BASE", "http://localhost:8001/api/chat")
HDR = {"Content-Type": "application/json",
       "x-tenant-id": os.environ.get("EVAL_TENANT", "T001"),
       "x-user-id": "golden-eval",
       "x-role": os.environ.get("EVAL_ROLE", "service_desk_agent")}

# (id, query, expected): "uc01" exact; "uc01+uc03" multi; "none" = must not route
CASES = [
    # uc01 — understand the record (summary / field-read)
    ("s1", "what is going on with INC0001009", "uc01"),
    ("s2", "give me the details of INC0001003", "uc01"),
    ("s3", "who is INC0001009 assigned to", "uc01"),
    ("s4", "what is the priority and status of INC0001022", "uc01"),
    ("s5", "tell me about INC0001045", "uc01"),
    # uc02 — similar / duplicate tickets
    ("m1", "do we have similar tickets to INC0001009", "uc02"),
    ("m2", "any other tickets like INC0001010", "uc02"),
    ("m3", "find duplicates of INC0001022", "uc02"),
    ("m4", "tickets resembling the office wifi outage", "uc02"),
    ("m5", "show me incidents similar to INC0001003", "uc02"),
    # uc03 — KB / how-to / docs
    ("k1", "how do I fix vpn disconnects", "uc03"),
    ("k2", "is there a runbook for wifi channel overlap", "uc03"),
    ("k3", "anything documented about MFA reset", "uc03"),
    ("k4", "what is the procedure to reset a password", "uc03"),
    ("k5", "any docs for INC0001009", "uc03"),
    # uc08 — catalog / provision (acquire something new)
    ("c1", "I need a software license", "uc08"),
    ("c2", "I need to request a new laptop", "uc08"),
    ("c3", "set up VPN access for me", "uc08"),
    ("c4", "get me access to the finance share", "uc08"),
    ("c5", "onboard a new hire", "uc08"),
    # hard axis A vs B (the subtle one)
    ("h1", "what do we know about INC0001009", "uc01"),
    ("h2", "what info is available for INC0001009", "uc03"),
    ("h3", "details of INC0001009", "uc01"),
    ("h4", "anything written up on INC0001022", "uc03"),
    ("h5", "describe INC0001003", "uc01"),
    # multi-intent
    ("x1", "summarize INC0001009 and any docs for it", "uc01+uc03"),
    ("x2", "give me details of INC0001009 and similar tickets", "uc01+uc02"),
    ("x3", "what is going on with INC0001022 and how do I fix it", "uc01+uc03"),
    ("x4", "summarize INC0001003 and any runbooks", "uc01+uc03"),
    ("x5", "details of INC0001010 plus related KB", "uc01+uc03"),
    # boundary: broken / how-to must NOT go to catalog
    ("b1", "my vpn is broken and not working", "uc03"),
    ("b2", "excel keeps crashing on my laptop", "uc03"),
    ("b3", "how do I install MS Project myself", "uc03"),
    ("b4", "the printer is offline", "uc03"),
    ("b5", "what is the process to request software", "uc03"),
    # off-domain: must NOT route
    ("o1", "tell me a joke", "none"),
    ("o2", "what is the weather today", "none"),
    ("o3", "who won the cricket match", "none"),
    ("o4", "thanks bye", "none"),
    ("o5", "can you write me a poem", "none"),
]


def _route(q):
    body = json.dumps({"message": q, "session_id": f"ge-{int(time.time()*1000)}"}).encode()
    d = json.loads(urllib.request.urlopen(
        urllib.request.Request(BASE, data=body, headers=HDR), timeout=120).read())
    agents = sorted({s.get("agent_id") for s in (d.get("step_results") or [])})
    return agents


def _ok(agents, expect):
    if expect == "none":
        return len(agents) == 0
    got = {a.split("_")[0] for a in agents}
    return got == set(expect.split("+"))


def main():
    passed = 0
    for cid, q, expect in CASES:
        try:
            agents = _route(q)
            ok = _ok(agents, expect)
        except Exception as e:                       # noqa: BLE001
            agents, ok = [f"ERR:{str(e)[:30]}"], False
        passed += ok
        got = ",".join(a.split("_")[0] for a in agents) or "(none)"
        print(f"  [{'PASS' if ok else 'FAIL'}] {cid:3s} got={got:14s} "
              f"want={expect:10s} <- {q[:46]}", flush=True)
    print(f"\n=== {passed}/{len(CASES)} passed ===", flush=True)
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
