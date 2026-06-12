"""Build a labeled golden routing set FROM REAL records — not hand-invented.

Honesty note: this is "real records -> templated queries", NOT "real user query
logs" (we have none). Every query is GROUNDED in a real row — a real incident
id, a real KB topic, a real catalog item — so the EXPECTED answer is verifiable
and the label is DERIVED from the data. The intent templates are the one
synthetic part, stated plainly.

Labels (the routing contract per our UCs):
  uc01        — read THIS record's own fields (summary / field-read). Grounded
                on real incident/request/problem/change ids.
  uc02        — other records like this one (similar / recurring). Grounded on
                real incident ids.
  uc03        — authored knowledge / how-to / break-fix. Grounded on KB topics
                with NO catalog counterpart (pure troubleshooting).
  uc08        — fulfilment: obtain/provision a resource. Grounded on catalog
                items with NO self-service KB (hardware/license/new access).
  kb_then_sr  — AMBIGUOUS self-serviceable action: could self-serve via a KB
                procedure OR raise an SR. DERIVED from a catalog item whose
                action ALSO has a KB article sharing a SELF-SERVICE ACTION token
                (reset/enroll/setup/...) — e.g. password reset, MFA reset. NOT a
                shared noun alone (that catches policy KBs / different actions).
                Expected behaviour (decided 2026-06-11): only when the system is
                UNSURE KB-vs-SR — KB FIRST, then OFFER the SR.
  uc05        — operator triage on a real incident (needs an operator role).
  off_domain  — a clearly non-IT ask; must be refused.

Output: scripts/golden/golden_real.json
Run:    .venv/bin/python scripts/golden/build_golden_real.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import psycopg

from oneops.config import get_settings

TENANT = "T001"
OUT = Path(__file__).resolve().parent / "golden_real.json"

# stopwords for token overlap (generic catalog/KB scaffolding words)
_STOP = {"request", "access", "new", "and", "or", "the", "a", "of", "for", "to",
         "license", "set", "service", "it", "general", "report", "an", "my",
         "i", "need", "v1", "v2", "m365", "old", "tips", "guide", "checklist",
         "procedure", "playbook", "runbook", "first", "response", "quick"}

# KB titles that START with one of these are instruction/how-to phrasings, so
# "how do i <title>" reads naturally; otherwise fall back to "is there a guide".
# These are linguistic action verbs (a derivation primitive), not a rule map.
_HOWTO_VERBS = {"fix", "troubleshoot", "resolve", "handle", "repair", "recover",
                "diagnose", "reduce", "improve", "replace", "respond", "clean",
                "remediate", "mitigate", "detect", "renew", "restore", "remove"}

# A catalog action is SELF-SERVICEABLE only when its KB twin shares one of these
# user-performable action tokens — not merely a shared noun. Keeps password/MFA
# reset; drops mailbox-quota (policy) and vpn-license (troubleshooting).
_SELFSERVE_ACTIONS = {"reset", "enroll", "enrollment", "reenroll", "setup",
                      "configure", "enable", "unlock", "join", "change",
                      "install", "re-enrollment"}


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower())
            if t not in _STOP and len(t) > 2}


def main() -> None:
    dsn = get_settings().postgres_url
    cases: list[dict] = []
    cid = 0

    def add(query: str, expected: str, grounded_on: str, family: str,
            note: str = "") -> None:
        nonlocal cid
        q = re.sub(r"\s+", " ", query).strip()
        cid += 1
        cases.append({"id": f"g{cid:03d}", "query": q, "expected": expected,
                      "grounded_on": grounded_on, "family": family, "note": note})

    with psycopg.connect(dsn) as c, c.cursor() as cur:
        cur.execute("SELECT incident_id FROM itsm.incident WHERE tenant_id=%s "
                    "ORDER BY incident_id", (TENANT,))
        incidents = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT request_id FROM itsm.request WHERE tenant_id=%s "
                    "ORDER BY request_id LIMIT 30", (TENANT,))
        requests = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT problem_id FROM itsm.problem WHERE tenant_id=%s "
                    "ORDER BY problem_id LIMIT 10", (TENANT,))
        problems = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT change_id FROM itsm.change WHERE tenant_id=%s "
                    "ORDER BY change_id LIMIT 10", (TENANT,))
        changes = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT kb_id, title FROM itsm.kb_knowledge WHERE tenant_id=%s "
                    "ORDER BY kb_id", (TENANT,))
        kbs = cur.fetchall()
        cur.execute("SELECT catalog_item_id, name FROM itsm.catalog_item "
                    "WHERE tenant_id=%s ORDER BY catalog_item_id", (TENANT,))
        catalog = cur.fetchall()

    kb_token_sets = [(_tokens(t), kb_id, t) for kb_id, t in kbs]

    def kb_selfserve_twin(name: str) -> tuple[str, str] | None:
        nt = _tokens(name)
        for kt, kb_id, ktitle in kb_token_sets:
            shared = nt & kt
            if len(shared) >= 2 and (shared & _SELFSERVE_ACTIONS):
                return kb_id, ktitle
        return None

    # ── uc01: read this record's own fields ────────────────────────────
    rec_pool = (incidents[:10] + requests[:6] + problems[:4] + changes[:4])
    uc01_tmpl = ["summarize {id}", "what is the priority of {id}",
                 "who is assigned to {id}", "what is the status of {id}",
                 "give me the full details of {id}", "tell me about {id}"]
    for n, rid in enumerate(rec_pool):
        add(uc01_tmpl[n % len(uc01_tmpl)].format(id=rid), "uc01", rid, "uc01")

    # ── uc02: other records like this one ──────────────────────────────
    for n, inc_id in enumerate(incidents[:12]):
        tmpl = ("any tickets similar to {id}" if n % 2 == 0
                else "is {id} a recurring problem")
        add(tmpl.format(id=inc_id), "uc02", inc_id, "uc02")

    # ── uc03: pure knowledge/how-to (KB with no catalog twin) ──────────
    cat_token_sets = [_tokens(n) for _, n in catalog]
    uc03_added = 0
    for kb_id, title in kbs:
        if uc03_added >= 20:
            break
        kt = _tokens(title)
        if any(len(kt & ct) >= 2 for ct in cat_token_sets):
            continue                                  # belongs to kb_then_sr/uc08
        clean = re.sub(r"\s*\[\d+\]\s*$", "", title).strip().lower()
        first = clean.split()[0] if clean else ""
        q = f"how do i {clean}" if first in _HOWTO_VERBS \
            else f"is there a guide for {clean}"
        add(q, "uc03", kb_id, "uc03")
        uc03_added += 1

    # ── uc08 (pure SR) + kb_then_sr (ambiguous) from catalog ───────────
    sr_tmpl = ["request {n}", "i need {n}", "order {n}", "can i get {n}"]
    seen_names: set[str] = set()
    sr_added = 0
    for cat_id, name in catalog:
        key = name.strip().lower()
        if key in seen_names:                          # dedupe duplicate items
            continue
        seen_names.add(key)
        twin = kb_selfserve_twin(name)
        if twin is not None:
            # the self-service action + its subject → natural ambiguous query
            nt = _tokens(name)
            verb = next(iter(nt & _SELFSERVE_ACTIONS))
            subj = next((t for t in nt if t not in _SELFSERVE_ACTIONS), "")
            v = "set up" if verb in ("enroll", "enrollment", "setup") else verb
            add(f"{v} my {subj}".strip(), "kb_then_sr", f"{cat_id}|{twin[0]}",
                "kb_then_sr", note=f"catalog {cat_id} ~ KB {twin[0]} ({twin[1]})")
            add(f"i need to {v} my {subj}".strip(), "kb_then_sr",
                f"{cat_id}|{twin[0]}", "kb_then_sr")
        elif sr_added < 24:
            add(sr_tmpl[sr_added % len(sr_tmpl)].format(n=key), "uc08", cat_id,
                "uc08")
            sr_added += 1

    # ── uc05: operator triage on real incidents ────────────────────────
    for n, inc_id in enumerate(incidents[12:20]):
        tmpl = ("triage {id}" if n % 2 == 0
                else "what category and priority should {id} be")
        add(tmpl.format(id=inc_id), "uc05", inc_id, "uc05",
            note="operator role required")

    # ── off_domain sanity anchors (must refuse) ────────────────────────
    for q in ["what's the weather tomorrow", "recommend a good restaurant",
              "what's my leave balance", "book me a flight", "who won the match",
              "how do i file my personal taxes"]:
        add(q, "off_domain", "-", "off_domain")

    OUT.write_text(json.dumps(cases, indent=2))
    fam: dict[str, int] = {}
    for cc in cases:
        fam[cc["family"]] = fam.get(cc["family"], 0) + 1
    print(f"wrote {len(cases)} cases -> {OUT}")
    for k in sorted(fam):
        print(f"  {k:12} {fam[k]}")


if __name__ == "__main__":
    main()
