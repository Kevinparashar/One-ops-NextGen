# Change Log — Production-Hardening Engagement

> One entry per reviewable change. Format: date · batch · what · why · files ·
> validation · result. Behavior-preserving unless explicitly noted.

## 2026-06-04 · Phase 1 — Baseline + audit (no code change)

- **What**: Read-only repo-wide scan; produced `codebase-understanding.md`,
  `production-readiness-audit.md`, `refactor-plan.md`, `testing-strategy.md`,
  `risk-register.md`. Captured baseline.
- **Validation**: `make ci-fast` green (ruff clean, mypy 184 files, marked-unit 19✓).
  Full-suite goalposts recorded from 2026-06-02 run on identical code.
- **Result**: Baseline understood. No source modified. Rollback point `cfc3832`.

## 2026-06-04 · Batch A2 — remove dead console-script entrypoints

- **What**: Removed `[project.scripts]` (`oneops-graph`/`oneops-uc1`/`oneops-uc3`)
  from `pyproject.toml`; they pointed at non-existent `oneops.entry.*` modules.
- **Why**: Installing the package exposed three broken commands (P2-6). Service
  runs via the app factory + `WORKER_ROLE`, so these were pure dead config.
- **Files**: `pyproject.toml`.
- **Validation**: pyproject parses (tomllib), `import oneops` OK, `make ci-fast`
  green (ruff/mypy/marked-unit). No runtime path touched.
- **Result**: Committed `1e43ea9`. No behavior change.

## 2026-06-04 · Batch A1 — FINDING (re-scoped, not yet implemented)

- **What changed in understanding**: A1 was scoped as a one-line gate change. On
  inspection the gate's `-m unit` runs 19 tests, deselects 1572, and the 19 known
  "flakes" are NOT infra tests — they are cross-test global-state pollution
  (isolation bug). The gate is green partly because it skips ~19 *real* failures.
- **Implication**: Expanding the gate requires first fixing the ~19 isolation
  failures (reset polluting globals/env in fixtures), then pointing the gate at
  the full `tests/unit/` dir. Audit P0-1 updated. Pending go-ahead (scope grew).

## 2026-06-04 · Batch A1 — fix CI unit-coverage gap (P0-1) — DONE

- **What**: Made the CI gate's unit stage validate the whole hermetic unit suite
  instead of ~19 hand-marked tests, after fixing the real isolation bugs and
  reclassifying integration-class tests.
- **RCA (the "19 flakes" were three different things)**:
  - 13 × `test_time_filter_extractor` — sync tests calling
    `asyncio.get_event_loop().run_until_complete()`; once an async test closed the
    ambient loop they failed. → switched to `asyncio.run()` (self-contained loop).
  - 3 × `test_session_continuity` turn tests — run the full pipeline against a
    **live LLM**; hang offline (not isolation bugs). → marked `integration`.
  - 2 × uc08 + 1 × uc08_routes — pass isolated, fail in dir-order from a poisoned
    `get_settings()` `lru_cache`. → systemic fix below.
- **Systemic fixes**: new `tests/unit/conftest.py` — (a) auto-marks every unit-dir
  test `unit` unless it opts into integration/slow/stress (so `-m unit` = whole
  hermetic suite); (b) clears the `get_settings()` cache around each test.
- **Taxonomy**: 7 uc08 files that need a live DB (already `skipif(POSTGRES_URL)`)
  + 3 session tests marked `integration`. Partition: **1510 unit / 81 integration**.
- **Gate**: unit stage auto-runs the 1510 hermetic tests; integration stage now
  tree-wide (`-m integration tests`) so the 81 run in their lane. `ci.sh` + Makefile.
- **Files**: `tests/unit/conftest.py` (new); `tests/unit/router/test_time_filter_extractor.py`;
  `tests/unit/api/test_session_continuity.py`; 7 uc08 test files; `scripts/ci.sh`; `Makefile`.
- **Validation**: `make ci-fast` green — ruff + mypy + **1510 passed / 81 deselected
  in 102s** (was 19 passed). No product code touched.
- **Result**: P0-1 resolved. Note: `ci-fast` now ~1:42 (real coverage); if too slow
  per-commit, add `pytest-xdist` (deliberate dep decision — see risk-register R-5).

## 2026-06-04 · Rename Option A — descriptive use-case names (display only)

- **What**: Made every human-facing surface show the descriptive use-case name
  instead of the `ucNN_` wire id. The wire id (routes, NATS subjects, env vars,
  module names, registry ids, `uc_id` values) is a stable contract and is
  UNCHANGED — only what a person reads changed.
- **Scope finding**: names were already mostly descriptive (registry agent names +
  rich descriptions; frontend derived labels). Three real gaps fixed:
  - **A-1**: the fast-path session message built its label as
    `uc_id.replace("_"," ").title()` → "Run **Uc01** Summarization: …" (wire id
    shown to the user). Now uses the descriptive name → "Run Summarization: …".
  - **A-2**: new `_uc_display_name(uc_id, registry)` helper — single source of
    truth is the registry agent `name` (minus " Agent"), with a uc_id-derivation
    fallback. `/api/fast/{uc_id}/spec` now returns `display_name` (additive,
    non-breaking); frontend `ucLabel(spec)` consumes it across all 4 label sites
    (incl. the status line that printed the raw `uc01_summarization`).
  - **A-3**: two operator log strings reworded *additively* — "Triage (UC-5)
    runner" / "Similar Tickets (UC-2) runner" — the `UC-N` token is kept inside
    the string so any log-based alerting still matches.
- **Files**: `src/oneops/api/app.py`, `src/oneops/api/static/app.js`,
  `tests/unit/api/test_uc_display_name.py` (new, 10 tests).
- **Validation**: 10 new tests pass (incl. a contract test asserting `uc_id`
  unchanged + `display_name` present); ruff + mypy clean; `make ci-fast` green —
  **1520 passed / 81 deselected in 167s**.
- **Result**: Option A complete. No contract changed (asserted by test). The deep
  rename (module/route/subject/env) remains Option B — not taken.

<!-- Append new entries below this line as batches land. -->
