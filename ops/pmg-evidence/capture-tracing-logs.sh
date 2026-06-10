#!/usr/bin/env bash
#
# capture-tracing-logs.sh
#
# Drives a single end-to-end UC-8 button-mode turn and captures the state
# of every infrastructure component the turn touched, into one file per
# component under ops/pmg-evidence/tracing-logs/.
#
# Read-only against the application code, dashboards, and containers.
# The only writes are:
#   - one new SR row in itsm.request (the demo turn)
#   - the eight evidence files in ops/pmg-evidence/tracing-logs/
#
# Run from the project root:
#   bash ops/pmg-evidence/capture-tracing-logs.sh
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && cd .. && pwd)"
cd "$ROOT"

EVIDIR="ops/pmg-evidence/tracing-logs"
mkdir -p "$EVIDIR"

TS="$(date -u +%FT%TZ)"
echo "════════ End-to-end tracing capture — $TS ════════"
echo "Evidence directory: $EVIDIR"
echo

# Pre-flight: confirm the server is up
if ! curl -sS -o /dev/null -w '' --max-time 3 http://127.0.0.1:8765/openapi.json; then
  echo "ERROR: API server at :8765 is not reachable. Start it first." >&2
  exit 1
fi

# Record application log position so we slice this run only
APP_LOG="/tmp/uc08_prod_server.log"
APP_BEFORE=0
[[ -f "$APP_LOG" ]] && APP_BEFORE=$(wc -l < "$APP_LOG")

BASE="http://127.0.0.1:8765"
TENANT="${ONEOPS_TENANT:-T001}"
USER="${ONEOPS_USER:-USR00001}"
ROLE="${ONEOPS_ROLE:-service_desk_agent}"
HEADERS=(-H "x-tenant-id: $TENANT" -H "x-user-id: $USER" -H "x-role: $ROLE"
         -H "content-type: application/json")

# Default demo prompt — overridable
PROMPT="${ONEOPS_CAPTURE_PROMPT:-Onboard our new senior dev Maria starting Monday in engineering}"

echo "── Driving UC-8 turn ──"
echo "  prompt: $PROMPT"

CREATE=$(curl -sS "${HEADERS[@]}" -X POST "$BASE/api/uc08/create-sr" \
  -d "$(python3 -c "import json,sys; print(json.dumps({'user_text': sys.argv[1]}))" "$PROMPT")")
SR_ID=$(echo "$CREATE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('request_id',''))")
TITLE=$(echo "$CREATE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('title',''))")
DESC=$(echo "$CREATE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('description',''))")
JV=$(echo "$CREATE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('judge_verdict','-'))")
echo "  SR=$SR_ID  judge=$JV"

MATCH=$(curl -sS "${HEADERS[@]}" -X POST "$BASE/api/uc08/match" \
  -d "$(python3 -c "import json,sys; print(json.dumps({'sr_title': sys.argv[1], 'sr_description': sys.argv[2]}))" "$TITLE" "$DESC")")
CAT=$(echo "$MATCH" | python3 -c "import sys,json; d=json.load(sys.stdin); print((d.get('auto_pick') or {}).get('catalog_item_id') or '')")
echo "  match=$CAT"

if [[ -n "$CAT" ]]; then
  FUL=$(curl -sS "${HEADERS[@]}" -X POST "$BASE/api/uc08/fulfill" \
    -d "$(python3 -c "import json,sys; print(json.dumps({'request_id': sys.argv[1], 'catalog_item_id': sys.argv[2], 'variables': {}}))" "$SR_ID" "$CAT")")
  RIT=$(echo "$FUL" | python3 -c "import sys,json; print(json.load(sys.stdin).get('ritm_id',''))")
  echo "  fulfill=$RIT"
fi

echo
echo "── Waiting 18 seconds for spans to flush ──"
sleep 18

echo
echo "── Capturing per-component evidence ──"

# 01. OTel collector container log
docker logs --tail 300 nextgen-otel-collector > "$EVIDIR/01-otel-collector.log" 2>&1
echo "  ✓ 01-otel-collector.log"

# 02. NATS — varz, connz, subscriptions
{
  echo "════════ NATS state captured $TS ════════"
  echo
  echo "── varz (server health and counts) ──"
  docker exec nextgen-nats wget -qO- http://localhost:8222/varz 2>/dev/null | python3 -m json.tool 2>/dev/null
  echo
  echo "── live subscriptions ──"
  docker exec nextgen-nats wget -qO- 'http://localhost:8222/subsz?subs=1' 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
subs = d.get('subscriptions_list', [])
oneops = [s for s in subs if 'oneops' in str(s).lower()]
print(f'total_subscriptions: {d.get(\"num_subscriptions\", 0)}')
print(f'oneops_subscriptions: {len(oneops)}')
print()
for s in oneops:
    if isinstance(s, dict):
        print(f'  subject={s.get(\"subject\",\"-\"):42s}  queue={s.get(\"qgroup\",\"-\"):28s}  msgs={s.get(\"msgs\",0)}')
" 2>/dev/null
} > "$EVIDIR/02-nats-subscriptions.json"
echo "  ✓ 02-nats-subscriptions.json"

# 03. LiteLLM proxy log
docker logs --tail 300 nextgen-litellm > "$EVIDIR/03-litellm-proxy.log" 2>&1
echo "  ✓ 03-litellm-proxy.log"

# 04. Dragonfly cache
{
  echo "════════ Dragonfly cache state captured $TS ════════"
  echo
  echo "── DBSIZE ──"
  docker exec nextgen-dragonfly redis-cli DBSIZE
  echo
  echo "── INFO stats ──"
  docker exec nextgen-dragonfly redis-cli INFO stats
  echo
  echo "── recent keys (first 30) ──"
  docker exec nextgen-dragonfly redis-cli --no-raw KEYS 'oneops:*' | head -30
} > "$EVIDIR/04-dragonfly-cache.log" 2>&1
echo "  ✓ 04-dragonfly-cache.log"

# 05. Prometheus metric snapshot — the metrics this turn produced
{
  echo "════════ Prometheus metric snapshot $TS ════════"
  for q in \
    ai_uc08_create_sr_total \
    ai_uc08_match_total \
    ai_uc08_fulfill_total \
    ai_uc08_judge_verdict_total \
    ai_uc08_agent_events_total \
    ai_llm_tokens_total \
    ai_llm_cost_usd_micros_total \
    ai_agent_runs_total
  do
    echo
    echo "── $q ──"
    curl -sS "http://localhost:9391/api/v1/query?query=$q" 2>/dev/null | python3 -m json.tool 2>/dev/null
  done
} > "$EVIDIR/05-prometheus-metrics.json"
echo "  ✓ 05-prometheus-metrics.json"

# 06. Tempo distributed traces
{
  echo "════════ Tempo trace search $TS ════════"
  echo "── UC-8 traces produced in the last 5 minutes ──"
  curl -sS "http://localhost:3401/api/search?tags=service.name%3Doneops&start=$(($(date +%s)-300))&end=$(date +%s)&limit=50" 2>/dev/null | python3 -m json.tool 2>/dev/null
} > "$EVIDIR/06-tempo-traces.json"
echo "  ✓ 06-tempo-traces.json"

# 07. Grafana state
{
  echo "════════ Grafana state $TS ════════"
  echo
  echo "── provisioned datasources ──"
  curl -sS -u oneops:oneops "http://localhost:3041/api/datasources" 2>/dev/null | python3 -m json.tool 2>/dev/null
  echo
  echo "── provisioned alert rules ──"
  curl -sS -u oneops:oneops "http://localhost:3041/api/v1/provisioning/alert-rules" 2>/dev/null | python3 -m json.tool 2>/dev/null
  echo
  echo "── current alert states ──"
  curl -sS -u oneops:oneops "http://localhost:3041/api/prometheus/grafana/api/v1/alerts" 2>/dev/null | python3 -m json.tool 2>/dev/null
} > "$EVIDIR/07-grafana-state.json"
echo "  ✓ 07-grafana-state.json"

# 08. Application log slice
APP_AFTER=$([[ -f "$APP_LOG" ]] && wc -l < "$APP_LOG" || echo 0)
if [[ "$APP_AFTER" -gt "$APP_BEFORE" ]]; then
  sed -n "${APP_BEFORE},${APP_AFTER}p" "$APP_LOG" > "$EVIDIR/08-application.log"
else
  echo "(application log unchanged)" > "$EVIDIR/08-application.log"
fi
echo "  ✓ 08-application.log"

echo
echo "════════ Capture complete ════════"
ls -la "$EVIDIR/"
