#!/usr/bin/env bash
#
# verify-all.sh — master PMG-evidence verifier.
#
# WHY: PMG needs a single command that walks every Day-1 phase, confirms each
# evidence artefact exists, and writes a green/red REPORT.md. This is the
# operator gate that distinguishes "code written" from "code verified" per
# PROJECT-BRIEFING §2.7 (no silent failures) and §2.9 (production-grade testing).
#
# USAGE:
#   bash ops/pmg-evidence/verify-all.sh    # or: make pmg-verify
#
# OUTPUT:
#   ops/pmg-evidence/REPORT.md             generated, one row per phase
#   stdout                                 per-phase status with colour codes
#
# EXIT CODES:
#   0  every phase verified green
#   1  one or more phases red (REPORT.md still produced for inspection)
#
# IDIOMS:
#   - set -euo pipefail: rule §2.7 fail loud
#   - REPORT.md is the single artefact a PMG reviewer opens first
set -euo pipefail

# ── paths ────────────────────────────────────────────────────────────────────
EVIDENCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORT="${EVIDENCE_DIR}/REPORT.md"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ── colour helpers (no-op when not a TTY) ────────────────────────────────────
if [ -t 1 ]; then
    C_GREEN="\033[32m"; C_RED="\033[31m"; C_DIM="\033[2m"; C_RST="\033[0m"
else
    C_GREEN=""; C_RED=""; C_DIM=""; C_RST=""
fi

# ── tracking ─────────────────────────────────────────────────────────────────
declare -a PHASE_NAMES
declare -a PHASE_STATUSES
declare -a PHASE_ARTEFACTS
declare -a PHASE_NOTES
RED_COUNT=0

# Each phase calls record_phase to add its row to the report.
#   $1 = phase number
#   $2 = phase name
#   $3 = status (green|red)
#   $4 = primary evidence artefact path (or "—" if N/A)
#   $5 = note (one short line)
record_phase() {
    PHASE_NAMES+=("Phase $1 — $2")
    PHASE_STATUSES+=("$3")
    PHASE_ARTEFACTS+=("$4")
    PHASE_NOTES+=("$5")
    if [ "$3" = "red" ]; then
        RED_COUNT=$((RED_COUNT + 1))
        printf "%b✗ Phase %s — %s%b — %s\n" "$C_RED" "$1" "$2" "$C_RST" "$5"
    else
        printf "%b✓ Phase %s — %s%b — %s\n" "$C_GREEN" "$1" "$2" "$C_RST" "$5"
    fi
}

# A path is "green" if it is a non-empty file/dir; "red" otherwise.
check_path() {
    if [ -s "$1" ] || [ -d "$1" ]; then echo "green"; else echo "red"; fi
}

# ── phases ───────────────────────────────────────────────────────────────────
printf "%b── PMG evidence verification — %s ──%b\n" "$C_DIM" "$TS" "$C_RST"
printf "\n"

# Phase 1 — Scaffolding
P1_README="$EVIDENCE_DIR/README.md"
P1_LOG="$EVIDENCE_DIR/phase-1-scaffolding.log"
P1_STATUS=$(check_path "$P1_README")
[ "$(check_path "$P1_LOG")" = "red" ] && P1_STATUS="red"
record_phase 1 "Scaffolding" "$P1_STATUS" "$P1_LOG" \
    "evidence dir + README + verify script + ci.sh skeleton + Makefile targets"

# Phase 2 — UC-5 Triage handler
P2_LOG="$EVIDENCE_DIR/phase-2-uc05-routing.log"
P2_STATUS=$(check_path "$P2_LOG")
record_phase 2 "UC-5 Triage handler" "$P2_STATUS" "$P2_LOG" \
    "5-incident routing + Tempo trace IDs + structured TriageDecision payloads"

# Phase 3 — Lifecycle state machine
P3_LOG="$EVIDENCE_DIR/phase-3-lifecycle.log"
P3_STATUS=$(check_path "$P3_LOG")
record_phase 3 "Lifecycle state machine" "$P3_STATUS" "$P3_LOG" \
    "boot validation + active-route trace + deprecated-refusal trace"

# Phase 4 — UC-8 Fulfillment handler
P4_LOG="$EVIDENCE_DIR/phase-4-uc08-fulfillment.log"
P4_STATUS=$(check_path "$P4_LOG")
record_phase 4 "UC-8 Fulfillment handler" "$P4_STATUS" "$P4_LOG" \
    "onboarding-template wave→interrupt→resume Tempo trace tree"

# Phase 5 — SLO + cost + probes
P5_LOG="$EVIDENCE_DIR/phase-5-slo-alert.log"
P5_DASH="$EVIDENCE_DIR/dashboards/per-tenant-cost.json"
P5_STATUS=$(check_path "$P5_LOG")
[ "$(check_path "$P5_DASH")" = "red" ] && P5_STATUS="red"
record_phase 5 "SLO + cost + probes" "$P5_STATUS" "$P5_LOG" \
    "alert rules + cost dashboard JSON + forced-breach alert log"

# Phase 6 — Local CI gate
P6_GREEN="$EVIDENCE_DIR/phase-6-ci-gate-green.log"
P6_BLOCK="$EVIDENCE_DIR/phase-6-ci-gate-blocks.log"
P6_STATUS=$(check_path "$P6_GREEN")
[ "$(check_path "$P6_BLOCK")" = "red" ] && P6_STATUS="red"
record_phase 6 "Local CI gate" "$P6_STATUS" "$P6_GREEN" \
    "make ci green on clean tree + blocked on broken tree"

# Phase 7 — Demo runbook + decision package
P7_RUNBOOK="../../docs/pmg-demo-runbook.md"
P7_DECPKG="../../docs/manager-decision-package.md"
P7_STATUS="green"
[ "$(check_path "$EVIDENCE_DIR/$P7_RUNBOOK")" = "red" ] && P7_STATUS="red"
[ "$(check_path "$EVIDENCE_DIR/$P7_DECPKG")" = "red" ] && P7_STATUS="red"
record_phase 7 "Demo runbook + decision package" "$P7_STATUS" "docs/pmg-demo-runbook.md" \
    "PMG meeting script + manager 10-question decision package"

# ── write REPORT.md ──────────────────────────────────────────────────────────
{
    echo "# PMG evidence report"
    echo ""
    echo "**Generated:** \`$TS\`"
    echo "**Verifier:** \`ops/pmg-evidence/verify-all.sh\`"
    echo "**Result:** $([ "$RED_COUNT" -eq 0 ] && echo "✅ all phases green" || echo "❌ $RED_COUNT phase(s) red")"
    echo ""
    echo "## Per-phase status"
    echo ""
    echo "| # | Phase | Status | Evidence | Notes |"
    echo "|---|---|---|---|---|"
    for i in "${!PHASE_NAMES[@]}"; do
        local_idx=$((i + 1))
        local_status="${PHASE_STATUSES[$i]}"
        if [ "$local_status" = "green" ]; then mark="✅"; else mark="❌"; fi
        echo "| $local_idx | ${PHASE_NAMES[$i]} | $mark $local_status | \`${PHASE_ARTEFACTS[$i]}\` | ${PHASE_NOTES[$i]} |"
    done
    echo ""
    echo "## How to read this"
    echo ""
    echo "- A green row means the named evidence file exists and is non-empty."
    echo "- A red row means the evidence file is missing or empty — the phase has not been verified working."
    echo "- For a red row, fix the underlying step before claiming Day-1 complete (rule §2.7 no silent failures)."
    echo "- Each row links to a log file in this directory; deep-trace JSON lives under \`traces/\`."
    echo ""
    echo "## Source of truth"
    echo ""
    echo "- Plan: [\`docs/day1-execution-plan.md\`](../../docs/day1-execution-plan.md)"
    echo "- Production maturity plan: [\`docs/production-maturity-plan.md\`](../../docs/production-maturity-plan.md)"
    echo "- Demo runbook: [\`docs/pmg-demo-runbook.md\`](../../docs/pmg-demo-runbook.md)"
    echo "- Decision package: [\`docs/manager-decision-package.md\`](../../docs/manager-decision-package.md)"
} > "$REPORT"

printf "\n"
printf "%bREPORT.md%b → %s\n" "$C_DIM" "$C_RST" "$REPORT"
printf "\n"

if [ "$RED_COUNT" -gt 0 ]; then
    printf "%b✗ %d phase(s) red — fix before claiming Day-1 done.%b\n" "$C_RED" "$RED_COUNT" "$C_RST"
    exit 1
fi

printf "%b✓ all phases green%b\n" "$C_GREEN" "$C_RST"
exit 0
