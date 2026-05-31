#!/usr/bin/env bash
# Common helpers for OneOps synthetic probes.
#
# Each probe (ops/probes/uc01.sh, uc03.sh, uc05.sh, uc08.sh) runs a
# golden-path query against the live API and prints one line per
# invocation to stdout in a parse-friendly format:
#
#   ts=<iso>  uc=<id>  status=<HTTP>  latency_ms=<int>  note=<short>
#
# Production posture:
#   - No external deps beyond curl + date + bash. Runs in any container.
#   - Always exits 0 (probes are observability, not gates). The status
#     field carries the verdict. Aggregation lives in Prometheus + the
#     ops/pmg-evidence/ log files.
#   - Per-UC scripts override BASE / TENANT / USER / ROLE via env if
#     needed; sensible defaults match the local dev setup.

BASE="${ONEOPS_API_BASE:-http://127.0.0.1:8765}"
TENANT="${ONEOPS_TENANT:-T001}"
USER="${ONEOPS_USER:-USR00001}"
ROLE="${ONEOPS_ROLE:-service_desk_agent}"

probe_emit() {
  local uc="$1" status="$2" lat="$3" note="$4"
  printf 'ts=%s  uc=%s  status=%s  latency_ms=%d  note=%s\n' \
    "$(date -u +%FT%TZ)" "$uc" "$status" "$lat" "$note"
}

# Run a single curl POST against $BASE$path with a JSON body. Echoes one
# parse-friendly probe line. Always returns 0.
probe_post() {
  local uc="$1" path="$2" body="$3" note="$4"
  local started ended status lat
  started=$(date +%s%3N)
  status=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 30 \
    -H "x-tenant-id: $TENANT" -H "x-user-id: $USER" -H "x-role: $ROLE" \
    -H "content-type: application/json" \
    -X POST "$BASE$path" -d "$body" 2>/dev/null || echo "000")
  ended=$(date +%s%3N)
  lat=$((ended - started))
  probe_emit "$uc" "$status" "$lat" "$note"
}
