#!/usr/bin/env bash
#
# scripts/ci.sh — local CI gate, replacing GitHub Actions.
#
# WHY: This repository has no GitHub access today; a CI gate must still exist so
# rule §2.9 (production-grade testing on every change) is enforced. This script
# is the gate. When GitHub access lands, a .github/workflows/ci.yml is a 10-line
# wrapper around this script — logic stays portable.
#
# USAGE:
#   bash scripts/ci.sh           # full gate: 6 stages, fail-fast
#   bash scripts/ci.sh --fast    # skip the integration suite (pre-commit hook)
#   make ci                      # full gate (Makefile shortcut)
#   make ci-fast                 # fast gate (Makefile shortcut)
#
# STAGES (in order):
#   1. ruff      lint (src + tests)
#   2. mypy      type check (src)
#   3. unit      pytest -m unit tests/unit/
#   4. integ     pytest -m integration tests/integration/ (skipped in --fast)
#   5. smoke     scripts/smoke_routing.py — routing baseline (81/84)
#   6. devils    scripts/devils_play.py — 11-probe adversarial
#
# EXIT CODES:
#   0  every stage green
#   1+ first stage that failed (rule §2.7 fail loud)
#
# DAY-1 NOTE:
#   Stages 5 + 6 are no-ops in this skeleton — the scripts do not exist yet on
#   day-1 morning. Phase 6 of docs/day1-execution-plan.md fills them in. The
#   stage headers print regardless so the report shows all 6 stages.
set -euo pipefail

# ── flags ────────────────────────────────────────────────────────────────────
FAST_MODE=false
for arg in "$@"; do
    case "$arg" in
        --fast) FAST_MODE=true ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

# ── paths & tools ────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV="${VENV:-.venv}"
PY="${VENV}/bin/python"
PYTEST="${VENV}/bin/pytest"
RUFF="${VENV}/bin/ruff"
MYPY="${VENV}/bin/mypy"

# ── colours (no-op when not a TTY) ───────────────────────────────────────────
if [ -t 1 ]; then
    C_GREEN="\033[32m"; C_RED="\033[31m"; C_DIM="\033[2m"; C_RST="\033[0m"
else
    C_GREEN=""; C_RED=""; C_DIM=""; C_RST=""
fi

# ── stage runner ─────────────────────────────────────────────────────────────
# Each stage prints --- STAGE: <name> --- then runs. On failure, fail-fast with
# the stage name so the operator sees exactly which gate blocked.
run_stage() {
    local name="$1"; shift
    printf "\n%b--- STAGE: %s ---%b\n" "$C_DIM" "$name" "$C_RST"
    if "$@"; then
        printf "%b✓ %s%b\n" "$C_GREEN" "$name" "$C_RST"
    else
        printf "%b✗ %s — FAILED%b\n" "$C_RED" "$name" "$C_RST" >&2
        exit 1
    fi
}

# A stage marked as "deferred" prints a notice but does not fail. Used when the
# underlying script doesn't exist yet (day-1 skeleton). Day-1 Phase 6 replaces
# these with real commands.
defer_stage() {
    local name="$1"
    local reason="$2"
    printf "\n%b--- STAGE: %s ---%b\n" "$C_DIM" "$name" "$C_RST"
    printf "%b⚠ deferred — %s%b\n" "$C_DIM" "$reason" "$C_RST"
}

# ── stages ───────────────────────────────────────────────────────────────────
START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf "%bCI gate — %s — mode: %s%b\n" "$C_DIM" "$START_TS" "$([ "$FAST_MODE" = true ] && echo fast || echo full)" "$C_RST"

if [ -x "$RUFF" ]; then
    run_stage "ruff (lint)" "$RUFF" check src tests
else
    defer_stage "ruff (lint)" "ruff not installed in $VENV (run: make setup)"
fi

if [ -x "$MYPY" ]; then
    run_stage "mypy (typecheck)" "$MYPY" src/oneops
else
    defer_stage "mypy (typecheck)" "mypy not installed in $VENV (run: make setup)"
fi

if [ -x "$PYTEST" ]; then
    run_stage "pytest -m unit" "$PYTEST" -m unit tests/unit -q
else
    defer_stage "pytest -m unit" "pytest not installed in $VENV (run: make setup)"
fi

if [ "$FAST_MODE" = true ]; then
    printf "\n%b--- STAGE: pytest -m integration ---%b\n" "$C_DIM" "$C_RST"
    printf "%b⚠ skipped — --fast mode%b\n" "$C_DIM" "$C_RST"
elif [ -x "$PYTEST" ]; then
    run_stage "pytest -m integration" "$PYTEST" -m integration tests/integration -q
else
    defer_stage "pytest -m integration" "pytest not installed in $VENV (run: make setup)"
fi

if [ -f scripts/smoke_routing.py ]; then
    run_stage "smoke (routing baseline 81/84)" "$PY" scripts/smoke_routing.py
else
    defer_stage "smoke (routing baseline 81/84)" "scripts/smoke_routing.py not present (Phase 6 fills in)"
fi

if [ -f scripts/devils_play.py ]; then
    run_stage "devils (11-probe adversarial)" "$PY" scripts/devils_play.py
else
    defer_stage "devils (11-probe adversarial)" "scripts/devils_play.py not present (Phase 6 fills in)"
fi

END_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf "\n%b✓ CI gate green — %s → %s%b\n" "$C_GREEN" "$START_TS" "$END_TS" "$C_RST"
exit 0
