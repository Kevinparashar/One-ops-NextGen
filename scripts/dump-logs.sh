#!/usr/bin/env bash
# dump-logs.sh — snapshot the four backing services' container logs
#
# Streamed `docker logs -f` doesn't survive the sandbox here, so this
# script takes ON-DEMAND snapshots. Run it whenever you want fresh logs
# during a test session; the four files in /tmp/oneops-logs/ get rewritten.
#
# Usage:
#   ./scripts/dump-logs.sh              # last 500 lines per service
#   ./scripts/dump-logs.sh 2000         # last 2000 lines per service
#   ./scripts/dump-logs.sh since 1m     # logs from the last 1 minute
#
# Files written:
#   /tmp/oneops-logs/dragonfly.log
#   /tmp/oneops-logs/litellm.log
#   /tmp/oneops-logs/nats.log
#   /tmp/oneops-logs/otel.log
#   (and the existing /tmp/oneops-uvicorn.log is the OneOps API itself)

set -euo pipefail
mkdir -p /tmp/oneops-logs

mode="${1:-tail}"
arg="${2:-500}"

if [[ "$mode" == "since" ]]; then
  flag="--since"
  value="$arg"
else
  flag="--tail"
  value="$arg"
fi

declare -A SERVICES=(
  [dragonfly]=oneops-dragonfly
  [litellm]=ai-service-litellm
  [nats]=ai-service-nats
  [otel]=oneops-otel-collector
)

for short in dragonfly litellm nats otel; do
  container="${SERVICES[$short]}"
  out="/tmp/oneops-logs/${short}.log"
  if docker logs "$flag" "$value" "$container" > "$out" 2>&1; then
    size=$(wc -c < "$out")
    printf "  ✓ %-10s → %s  (%s bytes)\n" "$short" "$out" "$size"
  else
    printf "  ✗ %-10s → %s NOT REACHABLE\n" "$short" "$container"
  fi
done

echo ""
echo "Tail any of them live in your own terminal (not this sandbox):"
echo "  tail -F /tmp/oneops-logs/*.log /tmp/oneops-uvicorn.log"
echo ""
echo "Or watch one service in real time:"
echo "  docker logs -f oneops-dragonfly       # cache GET/SET, memory"
echo "  docker logs -f ai-service-litellm     # every LLM call + cost + tokens"
echo "  docker logs -f ai-service-nats        # NATS subjects (when ingress wires it)"
echo "  docker logs -f oneops-otel-collector  # trace + metric ingest"
