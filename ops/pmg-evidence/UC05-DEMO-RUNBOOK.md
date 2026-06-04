# UC-5 PMG Demo Runbook

**Audience:** demo operator running the UC-5 walk-through for PMG validation.
**Time required:** ~10 minutes (3 min infra, 5 min demo, 2 min Q&A).

---

## 1. Start the infrastructure (3 min, one-time per session)

```bash
cd /home/kevin-parashar/AI-services/Oneops-NextGen
docker compose up -d
docker compose ps   # confirm 8 services up
```

Wait ~15 seconds. Confirm liveness:

```bash
curl -s http://localhost:3041/api/health         # Grafana
curl -s http://localhost:4301/health/liveliness  # LiteLLM
nc -z localhost 4623 && echo NATS-OK             # NATS
```

Start the app:

```bash
export $(grep -v '^#' .env | xargs)
export LLM_GATEWAY_URL=http://localhost:4301
export NATS_URL=nats://localhost:4623
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4620
export OTEL_SERVICE_NAME=oneops-uc05
export LANGGRAPH_CHECKPOINTER=memory
export UC_INVOKER_MODE=local
export WORKER_ROLE=all

nohup .venv/bin/python -m uvicorn oneops.api.app:create_app --factory \
  --host 127.0.0.1 --port 8765 --log-level warning > /tmp/uc05_api.log 2>&1 &

sleep 12
curl -s http://127.0.0.1:8765/api/health
# Expect: {"status":"ok",...}
```

---

## 2. The demo (5 min)

### Set up auth context

```bash
export H='-H x-tenant-id:T001 -H x-user-id:tech1@corp -H x-role:technician_l1'
export J='-H Content-Type:application/json'
```

### Step 1 — Show the queue (10 sec)

```bash
curl -s $H http://127.0.0.1:8765/api/uc05/queue-summary
# Expect: {"incidents":{"untriaged_count":8},"requests":{"untriaged_count":3}}
```

**Narrative:** "Operator opens the triage queue. 8 untriaged incidents and 3 untriaged requests waiting."

### Step 2 — Show the incident list (10 sec)

```bash
curl -s $H "http://127.0.0.1:8765/api/uc05/queue?service_id=incident" | head -c 600
```

**Narrative:** "Each row shows which fields are missing so the technician sees what AI will fill before clicking."

### Step 3 — Propose triage (30 sec, real LLM call)

```bash
P=$(curl -s $H $J -X POST \
  -d '{"ticket_id":"DEMO_INC_001","service_id":"incident"}' \
  http://127.0.0.1:8765/api/uc05/propose)
echo $P | python3 -m json.tool
```

**Expect:** all 16 fields populated, real LLM-derived (category=network, subcategory=vpn, assigned_to=USR00003, ci_id=CI0000001, impact=On Department, priority=High, assignment_group=GRP-NETOPS, tags=[vpn,tunnel,wi-fi], risk_class=medium, mutation_intent=recommend_only, confidence_tier=propose).

**Narrative:** "AI proposes 16 triage fields in ~2 seconds. Each carries provenance — the proposal card would show 'category: network (4 of 5 similar tickets)'."

### Step 4 — Open Tempo and find the trace (30 sec)

Open: http://localhost:3041/explore?orgId=1&left=%5B%22now-15m%22,%22now%22,%22Tempo%22,%7B%22queryType%22:%22traceqlSearch%22,%22filters%22:%5B%7B%22id%22:%22service-name%22,%22tag%22:%22service.name%22,%22operator%22:%22%3D%22,%22scope%22:%22resource%22,%22value%22:%22oneops-uc05%22%7D%5D%7D%5D

**Narrative:** "Every step is traced. The propose call shows API → tool → adapter → LLM gateway, all linked by trace ID."

### Step 5 — Approve with an edit (10 sec)

```bash
PID=$(echo $P | python3 -c 'import sys,json; print(json.load(sys.stdin)["proposal_id"])')
curl -s $H $J -X POST \
  -d "{\"proposal_id\":\"$PID\",\"choice\":\"yes\",\"final_values\":{\"subcategory\":\"vpn\"}}" \
  http://127.0.0.1:8765/api/uc05/decide | python3 -m json.tool
```

**Expect:** `outcome=applied`, `applied_fields` shows technician's edit + AI suggestions + computed `sla_due`.

### Step 6 — Show the JSON was updated (10 sec)

```bash
python3 -c "
import json
d = json.load(open('src/oneops/use_cases/uc05_triage/fixtures/demo_tickets.json'))
r = next(r for r in d['incidents'] if r['incident_id']=='DEMO_INC_001')
print('status:', r['status'])
print('category:', r['category'])
print('sla_due:', r['sla_due'])
print('triaged_by:', r['triaged_by'])
"
```

**Narrative:** "The technician clicks Yes; the row is written, the SLA clock starts, and audit captures who, when, and what."

### Step 7 — Show Grafana cost dashboard (30 sec)

Open: http://localhost:3041/?orgId=1
Login: `oneops` / `oneops`

Navigate to Dashboards → OneOps Overview. Per-tenant cost counters and trace latency p95 are populated.

---

## 3. Reset for the next demo session (10 sec)

```bash
git checkout src/oneops/use_cases/uc05_triage/fixtures/demo_tickets.json
```

This restores DEMO_INC_001's NULL state so the next demo starts fresh.

---

## 4. Q&A cheat sheet

| Question | Answer |
|---|---|
| "Does this write to the real DB?" | "Not in this demo run — the JsonFixtureStore writes to `demo_tickets.json`. Production uses a DbStore that satisfies the same Protocol. Swap is a one-line config change." |
| "How do you avoid double charging the LLM?" | "Single LLM egress via LlmGateway. Per-tenant cost ledger. PII redaction. Replay cache." |
| "What if the LLM is down?" | "Tool 3 returns safe-default impact/urgency. Tag extractor returns empty list. Tiebreak falls back to kNN. Verified by 38 devil's-play probes." |
| "What if Tempo is down?" | "Spans go in-memory; service keeps running. Spans show up next time Tempo comes back." |
| "What if NATS is down?" | "API returns 503 with retry-after; no silent loss. Verified." |
| "How do you stop a bad-actor tenant?" | "QuotaGuard: per-tenant LLM call budget. Once exceeded, gateway raises QuotaExceeded → tool falls back to safe default." |
| "Is the trace tree unified across all hops?" | "Yes. W3C traceparent injected by NATSClient adapter; Phase 2 helper parses it on the receive side." |

---

## 5. Common operator commands

```bash
# Show the live trace stream
curl -s "http://localhost:3401/api/search?tags=service.name%3Doneops-uc05&limit=10" \
  | python3 -m json.tool

# Show LiteLLM model usage
curl -s http://localhost:4301/health/liveliness

# Show NATS published-message counts (via monitor inside container)
docker exec nextgen-nats wget -qO- http://localhost:8623/varz | head -50

# Tail API log
tail -f /tmp/uc05_api.log

# Stop everything
docker compose down
pkill -f "uvicorn oneops.api"
```

---

## 6. Where the per-phase evidence lives

```
ops/pmg-evidence/phase-2-k-prime-phase-1-adapters.log
ops/pmg-evidence/phase-2-k-prime-phase-1-devils-play.log
ops/pmg-evidence/phase-2-k-prime-phase-2-observability.log
ops/pmg-evidence/phase-2-k-prime-phase-3-langgraph.log
ops/pmg-evidence/phase-2-k-prime-phase-4-nats.log
ops/pmg-evidence/phase-2-k-prime-phase-5-integrated-devils-play.log
ops/pmg-evidence/phase-2-k-prime-phase-6-live-e2e.log
ops/pmg-evidence/phase-2-k-prime-phase-7-no-regression.log
ops/pmg-evidence/phase-2-k-prime-SUMMARY.md   ← this folder's table of contents
```
