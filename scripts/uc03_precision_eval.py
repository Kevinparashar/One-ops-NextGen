"""UC-3 KB precision/recall baseline — 25 in-corpus (paraphrased from real
T001 KB topics → expect an ARTICLE) + 25 out-of-corpus (plausible enterprise
asks with no KB article → expect NO_MATCH / control-gate OOS, NOT a junk article).
Validates the confidence-banded no-match override (UC03_FORCE_RENDER_MIN_SCORE):
borderline gate-passers the LLM calls irrelevant are suppressed without hurting
in-corpus recall. Hits the live /api/chat at :8000, tenant T001.

Run:  .venv/bin/python scripts/uc03_precision_eval.py
Baseline: scripts/baselines/uc03_precision_2026-06-12.md
"""

import json, re, urllib.request, concurrent.futures as cf
# IN-CORPUS (paraphrased from real T001 KB topics) → expect an ARTICLE (recall).
IN = [
 "how do I fix vpn error 809",
 "my outlook emails are arriving late, what should I check",
 "the hr portal is throwing a 500 error, how do I respond",
 "how to mitigate a payroll database deadlock",
 "salesforce data sync is lagging, how do I troubleshoot",
 "wifi is out on a whole floor, what's the triage",
 "how do I reset mfa for a user",
 "the itsm portal is slow, what are the quick checks",
 "monitoring alerts are delayed, what's the playbook",
 "laptop patch install keeps failing, how do I remediate",
 "erp export is running out of memory, how to respond",
 "how do I renew the cmdb probe certificate",
 "how do I reset a password",
 "what's the m365 mailbox quota policy",
 "how to handle database replication lag",
 "how do I clean up duplicate ci records",
 "pods are stuck in crashloopbackoff, how do I recover",
 "how to replace an expired tls certificate",
 "how do I respond to a suspicious login",
 "ticket notifications are missing, how do I fix it",
 "how to recover from a failed deployment",
 "how do I repair an endpoint that won't boot",
 "how to reduce database query latency",
 "vpn keeps disconnecting when I roam between networks",
 "how do I recover failed webhook deliveries",
]
# OUT-OF-CORPUS (plausible enterprise asks, NOT in the KB) → expect NO-MATCH
# (or control-gate OOS). FAIL = surfaced a (wrong) article.
OUT = [
 "how do I request time off in the hr system",
 "what's the guide for booking a conference room",
 "how do I set up my voicemail greeting",
 "where do I submit an expense report",
 "how do I enroll in the company 401k",
 "what's the procedure for parking permit registration",
 "how do I update my emergency contact details in the portal",
 "guide for the cafeteria meal plan signup",
 "how do I book a desk through hot-desking",
 "how do I order new business cards",
 "steps to nominate a colleague for an award",
 "how do I access the employee discount portal",
 "what's the process for a sabbatical request",
 "how do I set up direct deposit for payroll",
 "guide for gym membership reimbursement",
 "how do I report a workplace safety hazard",
 "what's the travel booking tool guide",
 "how do I schedule my performance review",
 "where's the org chart for the marketing team",
 "what's the holiday schedule for this year",
 "guide for ordering office snacks",
 "how do I set up a charitable payroll deduction",
 "what's the process for a name change after marriage",
 "how do I update my tax withholding elections",
 "where do I find the relocation assistance policy",
]
def chat(msg):
    req=urllib.request.Request("http://localhost:8000/api/chat",
        data=json.dumps({"message":msg}).encode(),
        headers={"Content-Type":"application/json","x-tenant-id":"T001","x-user-id":"u_test","x-role":"service_desk_agent"})
    with urllib.request.urlopen(req,timeout=120) as r: return json.load(r)
def classify(d):
    fr=d.get("final_response"); txt=(fr.get("display_text") if isinstance(fr,dict) else fr) or ""
    low=txt.lower()
    if "out of my scope" in low or "within the itsm" in low: return "OOS", txt
    if "no matching" in low or "no published" in low or "try rephrasing" in low or "no knowledge-base article" in low: return "NO_MATCH", txt
    if re.search(r'KB\d{5,}', txt) or "source: kb" in low: return "ARTICLE", txt
    return "OTHER", txt
def run(kind, i, msg):
    try:
        cls,txt = classify(chat(msg))
        if kind=="IN": ok = (cls=="ARTICLE")
        else: ok = (cls in ("NO_MATCH","OOS"))   # didn't show junk
        return (kind,i,msg,cls,ok,txt[:60])
    except Exception as e:
        return (kind,i,msg,"ERR:"+type(e).__name__,False,"")
tasks=[("IN",i,m) for i,m in enumerate(IN)]+[("OUT",i,m) for i,m in enumerate(OUT)]
res=[]
with cf.ThreadPoolExecutor(max_workers=3) as ex:
    for r in ex.map(lambda a: run(*a), tasks): res.append(r)
res.sort(key=lambda r:(r[0],r[1]))
inok=sum(1 for r in res if r[0]=="IN" and r[4]); into=sum(1 for r in res if r[0]=="IN")
outok=sum(1 for r in res if r[0]=="OUT" and r[4]); outo=sum(1 for r in res if r[0]=="OUT")
for kind,i,msg,cls,ok,head in res:
    print(f"{'ok ' if ok else 'XX '}[{kind} {i:2}] {cls:9} :: {msg[:46]}")
print("="*70)
print(f"  IN-CORPUS  (want ARTICLE):   {inok}/{into}  recall")
print(f"  OUT-CORPUS (want NO_MATCH):  {outok}/{outo}  precision (no junk shown)")
print(f"  TOTAL: {inok+outok}/{into+outo}")
print("FAILS:")
for kind,i,msg,cls,ok,head in res:
    if not ok: print(f"  [{kind} {i}] {cls} :: {msg} -> {head}")
