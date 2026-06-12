"""Build 200 END-TO-END, REAL-USER-STYLE queries — UNSEEN, NO uc05.

Unlike the templated routing set, these are phrased the way an actual user
types: conversational, with filler ("hey", "can you", "pls"), partial context,
natural symptom reports. Grounded in real rows where an id/topic is referenced
so the response is verifiable; the natural phrasing is the point of THIS set
(end-to-end behaviour under realistic input).

Each case carries a soft `kind` (what a human would expect it to be about) for
reporting — the e2e scorer reports execution HEALTH (executed / refused /
clarification / error) and the agent that ran, not strict route-correctness,
because real phrasing is legitimately fuzzy.

uc05 excluded (API-only triage agent, not chat-routable).

Output: scripts/golden/e2e_eval.json
Run:    .venv/bin/python scripts/golden/build_e2e_eval.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import psycopg

from oneops.config import get_settings

TENANT = "T001"
OUT = Path(__file__).resolve().parent / "e2e_eval.json"


def main() -> None:
    dsn = get_settings().postgres_url
    cases: list[dict] = []
    cid = 0

    def add(query: str, kind: str, grounded_on: str = "-") -> None:
        nonlocal cid
        q = re.sub(r"\s+", " ", query).strip()
        cid += 1
        cases.append({"id": f"e{cid:03d}", "query": q, "kind": kind,
                      "grounded_on": grounded_on})

    with psycopg.connect(dsn, connect_timeout=10) as c, c.cursor() as cur:
        cur.execute("SELECT incident_id FROM itsm.incident WHERE tenant_id=%s ORDER BY incident_id", (TENANT,))
        inc = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT request_id FROM itsm.request WHERE tenant_id=%s ORDER BY request_id", (TENANT,))
        req = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT problem_id FROM itsm.problem WHERE tenant_id=%s ORDER BY problem_id", (TENANT,))
        prb = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT change_id FROM itsm.change WHERE tenant_id=%s ORDER BY change_id", (TENANT,))
        chg = [r[0] for r in cur.fetchall()]

    # ── record reads (uc01) — conversational, real ids ──
    r01 = ["hey can you tell me what's going on with {id}",
           "what's the latest on {id}?", "pull up {id} for me real quick",
           "i need a quick rundown of {id}", "whats the status of {id} right now",
           "can you catch me up on {id}", "who's working on {id}?",
           "is {id} sorted yet or still open", "give me the lowdown on {id}",
           "what happened with {id}, any idea?"]
    pool = inc[55:82] + req[20:38] + prb[12:22] + chg[12:22]
    for n, rid in enumerate(pool):
        add(r01[n % len(r01)].format(id=rid), "record_read", rid)

    # ── similar / recurring (uc02) ──
    r02 = ["have we seen something like {id} before?",
           "is {id} one of those recurring things",
           "any other tickets that look like {id}",
           "pretty sure {id} has happened before, can you check",
           "are there dupes of {id} floating around"]
    for n, rid in enumerate(inc[22:44]):
        add(r02[n % len(r02)].format(id=rid), "similar", rid)
    # NL similar — NO ticket id, describe the symptom (the uc02 NL-path fix:
    # find similar by symptom text, not by a canonical id)
    nl_similar = ["have we had other tickets about vpn dropping on wifi",
                  "any similar issues with the payroll system erroring out",
                  "is the outlook sync delay a recurring problem for us",
                  "have we seen erp login slowness like this before",
                  "are there other tickets about wifi outages on a floor",
                  "do we get a lot of mfa reset requests like this",
                  "has anyone else reported pods crashlooping recently",
                  "any past tickets about the hr portal throwing 500s",
                  "have we had repeat issues with salesforce not syncing",
                  "are duplicate CI records a recurring thing for us"]
    for q in nl_similar:
        add(q, "similar")

    # ── how-to / knowledge (uc03) — natural problem framing ──
    kb_q = ["how do i fix my vpn dropping every time i switch wifi",
            "outlook isn't syncing, whats the fix",
            "my pods keep crashlooping, how do i recover them",
            "db queries are crawling, how do i speed them up",
            "whats the deal with replication lag, how do i handle it",
            "how do i renew an expired tls cert",
            "got a suspicious login alert, what do i do",
            "how do i clean up duplicate CI records",
            "deployment failed, how do i roll back",
            "wifi keeps cutting out on my floor, any guide",
            "how do i reduce database query latency",
            "my endpoint won't boot, how do i repair it",
            "tickets aren't sending notifications, how to fix",
            "how do i improve our knowledge search results",
            "erp login is dog slow, known issue?",
            "how do i recover pods stuck in crashloopbackoff",
            "whats the procedure for a failed patch on a laptop",
            "how do i troubleshoot vpn error 809",
            "mailbox quota policy — whats the rule again",
            "how do i fix missing ticket notifications",
            "how do i diagnose wifi dead zones in the office",
            "whats the runbook for an erp export running out of memory",
            "how do i handle a salesforce sync lagging",
            "monitoring alerts are delayed, how do i fix that",
            "how do i respond to a suspicious login",
            "change calendar fell over, whats the fallback",
            "how do i renew the cmdb probe certificate",
            "best way to triage a wifi floor outage",
            "how do i mitigate a payroll db deadlock",
            "outlook sync delay — whats the checklist"]
    for q in kb_q:
        add(q, "howto")

    # ── fulfilment (uc08) — natural requests incl HR/fin/fac ──
    sr_q = ["i need a new laptop, mine's dying",
            "can i get a second monitor for my desk",
            "pls set me up with vpn access",
            "i could really use a docking station",
            "need a headset for all these calls",
            "my machine needs more RAM, can you sort that",
            "running out of disk, need an ssd upgrade",
            "i need office installed on my new machine",
            "can someone give me access to the finance shared drive",
            "i need access to the prod database for reporting",
            "set me up with an ide license please",
            "i wanna request access to the code repo",
            "need to submit an expense claim for last week",
            "how do i get my travel reimbursed for the offsite",
            "i need to raise a PO for some equipment",
            "applying for leave next month, where do i do that",
            "sign me up for the leadership training pls",
            "i need a new id badge, lost mine",
            "can i get an employment verification letter for my visa",
            "want to put in for an internal transfer",
            "need a parking spot at the office",
            "can you book me a meeting room for tomorrow 3pm",
            "i need a desk assigned, just joined the team",
            "request a company phone please",
            "i need a sim with a data plan",
            "set up a mailbox for our new hire",
            "create a distro list for my team",
            "i need a webcam for video calls",
            "can i get adobe creative cloud",
            "need antivirus installed on my laptop",
            "i need a project management tool license",
            "can i get a tablet for field work",
            "request a gpu workstation for ml training",
            "i need a static ip / firewall rule for my service",
            "set me up with a cloud sandbox environment",
            "request a digital certificate",
            "i need a mailbox quota increase",
            "can i get a keyboard and mouse set",
            "request privileged admin access for the server",
            "i need a database client license"]
    for q in sr_q:
        add(q, "fulfil")

    # ── generic incident reports (no id) — natural symptoms ──
    gen = ["my laptop is super slow since the last update",
           "i can't log into my email this morning",
           "the wifi on the 4th floor is down again",
           "my screen keeps going black randomly",
           "teams won't open at all today",
           "i think my account got locked",
           "printer by the kitchen is jammed",
           "vpn won't connect from home",
           "my mouse just stopped working",
           "the shared drive isn't showing up",
           "payroll system errored when i set up direct deposit",
           "hr portal keeps throwing a 500",
           "erp is timing out on login",
           "salesforce data looks stale, not syncing",
           "my disk is almost full and everything's lagging",
           "i keep getting mfa prompts that just fail",
           "my keyboard is typing the wrong characters",
           "can't access the internal wiki, page won't load",
           "zoom audio cuts out in every meeting",
           "my laptop fan is running constantly and it's hot",
           "outlook crashed and now won't reopen",
           "the badge reader at the main door isn't reading my card",
           "my second monitor isn't being detected",
           "getting certificate errors on the internal portal",
           "company vpn drops every few minutes today"]
    for q in gen:
        add(q, "incident")

    # ── self-service actions (deflect-first candidates) ──
    ss = ["reset my password", "i'm locked out, need a password reset",
          "i forgot my password", "reset my mfa please",
          "unlock my account", "i need to re-enroll mfa on my new phone",
          "change my password", "help me set up the authenticator app",
          "i can't log in, think i need a password reset",
          "my mfa isn't working, need to reset it"]
    for q in ss:
        add(q, "self_service")

    OUT.write_text(json.dumps(cases, indent=2))
    by: dict[str, int] = {}
    for cc in cases:
        by[cc["kind"]] = by.get(cc["kind"], 0) + 1
    print(f"wrote {len(cases)} cases -> {OUT}")
    for k in sorted(by):
        print(f"  {k:14} {by[k]}")


if __name__ == "__main__":
    main()
