#!/usr/bin/env bash
# UC-8 Fulfillment synthetic probe — exercises the create-sr + match
# read-only paths. Does NOT fire /fulfill (which would persist tasks
# and run a real workflow). For the full button flow, use the E2E
# test suite.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/_common.sh"
# (a) Match-only probe — preview catalog match for a known provisioning ask.
probe_post "uc08" "/api/uc08/match" \
  '{"sr_title":"VPN access for contractor","sr_description":"VPN access for contractor Tom Nguyen, expires in 30 days"}' \
  "match-vpn-access"
