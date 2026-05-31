#!/usr/bin/env bash
# UC-1 Summarization synthetic probe — golden path.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/_common.sh"
probe_post "uc01" "/api/chat" \
  '{"message":"summarize INC0001003","session_id":"probe-uc01"}' \
  "summarize-known-incident"
