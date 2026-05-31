#!/usr/bin/env bash
# Continuous-loop driver for all OneOps synthetic probes.
#
# Production posture:
#   - Runs every UC probe once per cycle, sleeps `INTERVAL` (default 60s)
#     between cycles. Cron-friendly: replace this loop with a per-minute
#     cron line per probe and you get the same effect.
#   - Output goes to stdout. Caller pipes it to the evidence file:
#       ops/probes/run-all-loop.sh | tee -a ops/pmg-evidence/probes.log
#   - Stops on SIGINT cleanly.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
INTERVAL="${PROBE_INTERVAL_S:-60}"
CYCLES="${PROBE_CYCLES:-0}"   # 0 = forever
i=0
while :; do
  i=$((i+1))
  "$DIR/uc01.sh"
  "$DIR/uc03.sh"
  "$DIR/uc05.sh"
  "$DIR/uc08.sh"
  [[ "$CYCLES" -gt 0 && "$i" -ge "$CYCLES" ]] && break
  sleep "$INTERVAL"
done
