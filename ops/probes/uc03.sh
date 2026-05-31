#!/usr/bin/env bash
# UC-3 KB Lookup synthetic probe — golden path.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/_common.sh"
probe_post "uc03" "/api/chat" \
  '{"message":"how do I reset MFA on a new device","session_id":"probe-uc03"}' \
  "kb-lookup-mfa-reset"
