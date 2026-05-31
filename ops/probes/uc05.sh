#!/usr/bin/env bash
# UC-5 Triage synthetic probe — proposes a triage on a known incident.
# Uses /api/uc05/propose directly (button-mode parity).
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/_common.sh"
probe_post "uc05" "/api/uc05/propose" \
  '{"ticket_id":"INC0001003","service_id":"incident"}' \
  "triage-propose-known-incident"
