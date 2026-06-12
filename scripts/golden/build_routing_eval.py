"""Build a 200-query ROUTE-CORRECTNESS set — UNSEEN, real-grounded, NO uc05.

Unlike the gate set (binary scope), this scores WHICH AGENT the router picks.
Each query is grounded in a real row and labeled with the agent that OWNS that
outcome:

  uc01        — read THIS record's own fields (real inc/req/prb/chg ids)
  uc02        — other records like this one (similar/recurring; real inc ids)
  uc03        — authored knowledge / how-to (real KB titles, as NL questions)
  uc08        — fulfilment: obtain/provision a resource (real catalog items)
  kb_then_sr  — ambiguous self-service action (password/MFA reset/unlock):
                expected to surface KB FIRST (uc03) per the 2026-06-11 decision

uc05 is EXCLUDED on purpose: uc05_triage is API-only
(`__uc05_api_only_never_matches__`) and never eligible for conversational
routing — scoring it from chat would be wrong.

UNSEEN: record slices and phrasings are distinct from both golden_real.json and
gate_eval.json. Queries are grounded in real rows; the intent phrasings are the
one synthetic part (we have no real user logs), stated plainly. Test stimuli,
not business rules.

Output: scripts/golden/routing_eval.json
Run:    .venv/bin/python scripts/golden/build_routing_eval.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import psycopg

from oneops.config import get_settings

TENANT = "T001"
OUT = Path(__file__).resolve().parent / "routing_eval.json"


def main() -> None:
    dsn = get_settings().postgres_url
    cases: list[dict] = []
    cid = 0

    def add(query: str, expected: str, grounded_on: str, note: str = "") -> None:
        nonlocal cid
        q = re.sub(r"\s+", " ", query).strip()
        cid += 1
        cases.append({"id": f"r{cid:03d}", "query": q, "expected": expected,
                      "grounded_on": grounded_on, "family": expected, "note": note})

    with psycopg.connect(dsn, connect_timeout=10) as c, c.cursor() as cur:
        cur.execute("SELECT incident_id FROM itsm.incident WHERE tenant_id=%s "
                    "ORDER BY incident_id", (TENANT,))
        inc = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT request_id FROM itsm.request WHERE tenant_id=%s "
                    "ORDER BY request_id", (TENANT,))
        req = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT problem_id FROM itsm.problem WHERE tenant_id=%s "
                    "ORDER BY problem_id", (TENANT,))
        prb = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT change_id FROM itsm.change WHERE tenant_id=%s "
                    "ORDER BY change_id", (TENANT,))
        chg = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT kb_id,title FROM itsm.kb_knowledge WHERE tenant_id=%s "
                    "ORDER BY kb_id", (TENANT,))
        kb = cur.fetchall()
        cur.execute("SELECT catalog_item_id,name FROM itsm.catalog_item "
                    "WHERE tenant_id=%s ORDER BY catalog_item_id", (TENANT,))
        cat = cur.fetchall()

    # ── uc01: read this record (FRESH slice + FRESH phrasings) — ~55 ──
    p01 = ["what's the current status of {id}", "summarise {id} for me",
           "where do things stand on {id}", "give me the details on {id}",
           "what's the priority on {id}", "who's handling {id}",
           "break down {id} for me", "fill me in on {id}",
           "what's the impact of {id}", "read me {id}"]
    rec = inc[60:90] + req[40:55] + prb[30:38] + chg[30:38]
    for n, rid in enumerate(rec):
        add(p01[n % len(p01)].format(id=rid), "uc01", rid)

    # ── uc02: other records like this one — ~30 ──
    p02 = ["any other tickets like {id}", "is {id} a recurring issue",
           "have we seen something like {id} before",
           "show me incidents similar to {id}",
           "is {id} part of a recurring pattern", "what else looks like {id}"]
    for n, rid in enumerate(inc[20:50]):
        add(p02[n % len(p02)].format(id=rid), "uc02", rid)

    # ── uc03: authored knowledge / how-to from REAL KB titles — ~45 ──
    p03 = ["how do i {t}", "what's the procedure to {t}",
           "is there a runbook for {t}", "guide me on how to {t}",
           "steps to {t}", "what's the recommended way to {t}"]
    used = 0
    prev = ""
    for kid, title in kb[16:]:                    # slice unused by gate set
        if used >= 45:
            break
        clean = re.sub(r"\s*\[\d+\]\s*$", "", title).strip().lower()
        if clean == prev:
            continue                              # skip duplicate titles
        prev = clean
        add(p03[used % len(p03)].format(t=clean), "uc03", kid)
        used += 1

    # ── uc08: fulfilment from REAL catalog (incl HR/FN/FC) — ~55 ──
    p08 = ["i need {n}", "can i request {n}", "please raise a request for {n}",
           "i'd like to get {n}", "set me up with {n}", "request {n}"]
    seen: set[str] = set()
    n08 = 0
    for cat_id, name in cat:
        if n08 >= 55:
            break
        key = name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        # password/MFA reset are ambiguous self-service -> handled below, skip here
        if any(w in key for w in ("password reset", "mfa enrollment")):
            continue
        add(p08[n08 % len(p08)].format(n=key), "uc08", cat_id, note=name)
        n08 += 1

    # ── kb_then_sr: ambiguous self-service -> expect KB first (uc03) — ~15 ──
    selfserve = ["reset my password", "i forgot my password and need to reset it",
                 "how do i reset my password", "reset my mfa",
                 "i need to re-enroll my mfa", "set up mfa on my new phone",
                 "unlock my account", "my account is locked, how do i unlock it",
                 "change my password", "i can't log in, need a password reset",
                 "help me reset my multi-factor authentication",
                 "re-enroll mfa after getting a new device",
                 "i'm locked out and need to reset my password",
                 "how do i change my expired password",
                 "set up authenticator app for mfa"]
    for q in selfserve:
        add(q, "kb_then_sr", "-")

    OUT.write_text(json.dumps(cases, indent=2))
    fam: dict[str, int] = {}
    for cc in cases:
        fam[cc["family"]] = fam.get(cc["family"], 0) + 1
    print(f"wrote {len(cases)} cases -> {OUT}")
    for k in sorted(fam):
        print(f"  {k:12} {fam[k]}")


if __name__ == "__main__":
    main()
