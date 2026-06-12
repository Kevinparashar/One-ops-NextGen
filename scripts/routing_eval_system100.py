"""System-level routing baseline — 100 unseen real-life queries across
uc01 (summarize) / uc02 (similar, id+text) / uc03 (kb) / uc08 (fulfillment).
Excludes uc05 (button-only). Hits the LIVE API at :8000 (/api/chat) with tenant
T001. Each query carries an expected agent code and an optional acceptable
alternative for genuine semantic-boundary cases.

Run (API must be up, flag state per .env):
  .venv/bin/python scripts/routing_eval_system100.py

Baseline result: see scripts/baselines/routing_system100_2026-06-12.md
"""

import json, re, urllib.request, concurrent.futures as cf
# Fresh, unseen, real-life queries. code: 1=uc01,2=uc02,3=uc03,8=uc08. alt=acceptable second read.
Q = [
 # ---- uc01: summarize / fields / status of a SPECIFIC record ----
 ("can you pull up INC9010007 for me",1,None),
 ("what's going on with INC9010014",1,None),
 ("give me a quick rundown of SR9010043",1,None),
 ("is INC9010087 still open or closed",1,None),
 ("who's working on INC9010054",1,None),
 ("what priority did we set on INC9010007",1,None),
 ("break down INC9010014 for me",1,None),
 ("what's the sla status on INC9010058",1,None),
 ("show the full record for SR9010052",1,None),
 ("when did INC9010109 come in",1,None),
 ("what team is INC9010002 with right now",1,None),
 ("catch me up on INC9010003",1,None),
 ("what's the resolution on INC9010014 if any",1,None),
 ("details on request SR9010081 please",1,None),
 ("how urgent is INC9010087",1,None),
 ("who logged INC9010007",1,None),
 ("what's the current state of SR9010039",1,None),
 ("summarize what happened on INC9010054",1,None),
 ("is there an assignee on INC9010058 yet",1,None),
 ("what category does INC9010001 fall under",1,None),
 ("give me the gist of SR0002002",1,None),
 ("what's the latest note on INC9010003",1,None),
 ("how was INC9010014 resolved",1,None),
 ("who owns request SR9010107",1,None),
 ("what's the impact level on INC9010109",1,None),
 # ---- uc02: similar / past tickets (id + text) ----
 ("pull up tickets that look like INC9010007",2,None),
 ("anything in our history matching INC9010054",2,None),
 ("is INC9010087 something we've seen repeatedly",2,None),
 ("find me incidents close to INC9010003",2,None),
 ("are there twins of INC9010014",2,None),
 ("have we logged this kind of thing before: vpn keeps asking to reconnect",2,None),
 ("other tickets where email attachments won't open",2,None),
 ("past cases of the shared calendar not syncing",2,None),
 ("similar issues to a frozen login screen on windows",2,None),
 ("anything like a database connection pool exhausted before",2,None),
 ("find requests resembling a new monitor not being detected",2,None),
 ("other incidents about slow vpn throughput in the evenings",2,None),
 ("have we had repeated reports of teams notifications not showing",2,None),
 ("tickets that match a corrupted user profile on login",2,None),
 ("past records of certificate expiry breaking an integration",2,None),
 ("other cases where backups failed overnight",2,None),
 ("similar tickets about a printer driver crashing",2,None),
 ("anything resembling sso redirect loops on the portal",2,None),
 ("have we seen network latency spikes to the data center",2,None),
 ("find incidents like a disk array degraded on a host",2,None),
 ("other tickets about onboarding access not provisioned on day one",2,None),
 ("past cases of two factor prompts not arriving by sms",2,None),
 ("similar issues to a web app returning 500 errors after deploy",2,None),
 ("anything matching mailbox auto-archive not running",2,None),
 ("other incidents where a laptop won't wake from sleep",2,None),
 # ---- uc03: kb / how-to / knowledge ----
 ("how do I connect to the wifi on a guest laptop",3,None),
 ("where's the guide for requesting time off in the hr system",3,None),
 ("steps to set up email signatures company wide",3,None),
 ("how can I share a large file securely",3,None),
 ("what's the procedure for a lost or stolen laptop",3,None),
 ("how do I join a teams meeting from a phone",3,None),
 ("is there documentation on the vpn split tunnel setup",3,None),
 ("how to recover a forgotten bitlocker pin",3,None),
 ("what's the process to escalate a critical incident",3,None),
 ("how do I enable out of office in outlook",3,None),
 ("guide for setting up a new printer queue",3,None),
 ("how to clear the dns cache on windows",3,None),
 ("what are the password complexity requirements here",3,None),
 ("how do I request a software exception",3,8),
 ("steps to migrate files to a new laptop",3,None),
 ("how to troubleshoot a black screen on a monitor",3,None),
 ("what's the backup policy for laptops",3,None),
 ("how do I set up multi factor on a new device",3,None),
 ("instructions for connecting an external keyboard over bluetooth",3,None),
 ("how to report a security incident",3,None),
 ("what's the guide for remote desktop into my work pc",3,None),
 ("how do I whitelist a website blocked by the firewall",3,None),
 ("steps to reset the local admin password",3,None),
 ("how to check my mailbox quota usage",3,None),
 ("what's the procedure for returning equipment when leaving",3,None),
 # ---- uc08: fulfillment / obtain / provision ----
 ("I need to request a docking station",8,None),
 ("can you set me up with access to the sales drive",8,None),
 ("please order a wireless mouse for me",8,None),
 ("I'd like a license for visio",8,None),
 ("provision a test environment for my project",8,None),
 ("I need a webcam for my desk",8,None),
 ("request a temporary contractor account",8,None),
 ("can I get my disk space quota increased",8,3),
 ("set up a new shared mailbox for the finance team",8,None),
 ("I want to request a second monitor for home",8,None),
 ("please add me to the engineering distribution list",8,None),
 ("order a new laptop battery",8,None),
 ("I need elevated access to the staging servers",8,None),
 ("request a yubikey for hardware mfa",8,None),
 ("can you provision a slack workspace for our team",8,None),
 ("I'd like to request noise cancelling headphones",8,None),
 ("set up vpn access for a new remote worker",8,None),
 ("please grant me edit rights on the project sharepoint",8,None),
 ("order an ergonomic chair for my office",8,None),
 ("I need a corporate sim card for travel",8,None),
 ("request access to the analytics dashboard",8,None),
 ("can I get an upgrade to the pro version of the design tool",8,None),
 ("provision a new email alias for support",8,None),
 ("I want to request a usb-c hub",8,None),
 ("please set up a guest wifi account for a visitor",8,None),
]
CODE={1:"uc01_summarization",2:"uc02_similar_tickets",3:"uc03_kb_lookup",8:"uc08_fulfillment"}
def chat(msg):
    req=urllib.request.Request("http://localhost:8000/api/chat",
        data=json.dumps({"message":msg}).encode(),
        headers={"Content-Type":"application/json","x-tenant-id":"T001","x-user-id":"u_test","x-role":"service_desk_agent"})
    with urllib.request.urlopen(req,timeout=150) as r: return json.load(r)
def run(i,msg,exp,alt):
    try:
        d=chat(msg); sr=d.get("step_results") or []
        agents=[s.get("agent_id") for s in sr if isinstance(s,dict)]
        interrupt=bool(d.get("interrupt"))
        primary=agents[0] if agents else ("INTERRUPT" if interrupt else "none")
        want=CODE[exp]; altn=CODE.get(alt) if alt else None
        hit=(want in agents) or (altn and altn in agents)
        if not hit and interrupt and exp==8: hit=True; primary="uc08(interrupt)"
        return (i,msg,exp,primary,",".join(agents),hit,bool(alt))
    except Exception as e:
        return (i,msg,exp,"ERR:"+type(e).__name__,"",False,bool(alt))
res=[]
with cf.ThreadPoolExecutor(max_workers=3) as ex:
    for r in ex.map(lambda a: run(*a), [(i,m,e,al) for i,(m,e,al) in enumerate(Q)]): res.append(r)
res.sort()
from collections import Counter
tot=Counter(); ok=Counter(); miss=[]
for i,msg,exp,primary,agents,hit,hasalt in res:
    tot[exp]+=1
    if hit: ok[exp]+=1
    else: miss.append((i,exp,primary,agents,msg))
    print(f"{'ok ' if hit else 'XX '}[{i:3}] exp=uc0{exp} got={primary:28.28} :: {msg[:48]}")
print("="*72)
for c in (1,2,3,8): print(f"  uc0{c} {CODE[c]:22}: {ok[c]:2}/{tot[c]:2}")
print(f"  TOTAL: {sum(ok.values())}/{sum(tot.values())}")
print("MISROUTES:")
for i,exp,primary,agents,msg in miss: print(f"  [{i}] exp=uc0{exp} got={primary} ({agents}) :: {msg}")
