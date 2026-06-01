#!/usr/bin/env bash
#
# tail-tracing-logs.sh
#
# Open live follow-mode tails on every infrastructure component so that
# when a query runs in the system, the operator watches the activity
# stream past in real time.
#
# Two modes:
#
#   bash ops/pmg-evidence/tail-tracing-logs.sh           # live to stdout (one combined stream)
#   bash ops/pmg-evidence/tail-tracing-logs.sh --append  # live AND append to evidence files
#
# In --append mode, each component's live output is also appended to its
# matching file under ops/pmg-evidence/Tracing logs/ so the evidence
# directory grows in real time as queries run. A line prefix
#   [HH:MM:SS] [component]
# is added to every appended line so the timeline is unambiguous.
#
# Press Ctrl+C to stop. Read-only against the application.
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && cd .. && pwd)"
cd "$ROOT"

EVIDIR="ops/pmg-evidence/Tracing logs"
APPEND=false
[[ "${1:-}" == "--append" ]] && APPEND=true && mkdir -p "$EVIDIR"

APP_LOG="/tmp/uc08_prod_server.log"

emit() {
  local component="$1" outfile="$2"
  while IFS= read -r line; do
    local stamped="[$(date -u +%H:%M:%SZ)] [$component] $line"
    echo "$stamped"
    if [[ "$APPEND" == "true" && -n "$outfile" ]]; then
      echo "$stamped" >> "$EVIDIR/$outfile"
    fi
  done
}

echo "════════ Live tracing tails — Ctrl+C to stop ════════"
echo "  append-to-evidence: $APPEND"
echo "  evidence dir      : $EVIDIR"
echo

# Trap so child processes die with the parent
trap 'kill 0' SIGINT SIGTERM EXIT

# 01. Application log (the most important one — every chat + button click)
if [[ -f "$APP_LOG" ]]; then
  tail -F -n 0 "$APP_LOG" 2>/dev/null \
    | emit "app   " "08-application.log" &
fi

# 02. OTel collector
docker logs -f --since 0s nextgen-otel-collector 2>&1 \
  | emit "otel  " "01-otel-collector.log" &

# 03. LiteLLM proxy
docker logs -f --since 0s nextgen-litellm 2>&1 \
  | emit "litellm" "03-litellm-proxy.log" &

# 04. NATS server
docker logs -f --since 0s nextgen-nats 2>&1 \
  | emit "nats  " "02-nats-server.log" &

# 05. Dragonfly cache — periodically dump hits/misses since DBSIZE doesn't log
while :; do
  hits=$(docker exec nextgen-dragonfly redis-cli INFO stats 2>/dev/null | awk -F: '/keyspace_hits/ {print $2+0}')
  misses=$(docker exec nextgen-dragonfly redis-cli INFO stats 2>/dev/null | awk -F: '/keyspace_misses/ {print $2+0}')
  size=$(docker exec nextgen-dragonfly redis-cli DBSIZE 2>/dev/null)
  echo "hits=$hits misses=$misses keys=$size" \
    | emit "cache " "04-dragonfly-cache.log"
  sleep 5
done &

# 06. Tempo + Prometheus (sample what each is ingesting)
docker logs -f --since 0s nextgen-tempo 2>&1 \
  | emit "tempo " "06-tempo-server.log" &

docker logs -f --since 0s nextgen-prometheus 2>&1 \
  | emit "prom  " "05-prometheus-server.log" &

# 07. Grafana (mostly idle — captures dashboard reads + alert evaluations)
docker logs -f --since 0s nextgen-grafana 2>&1 \
  | emit "grafana" "07-grafana-server.log" &

# Wait forever (until Ctrl+C trips the trap)
wait
