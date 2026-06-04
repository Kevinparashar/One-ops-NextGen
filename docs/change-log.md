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

## 2026-06-04 · Batch B — security P0s (exception-leak fix; AUTHZ finding corrected)

- **B-1 (P0-2) — NO CHANGE, audit corrected.** The finding ("`AUTHZ_JWT_SECRET`
  fails with `None`") was inaccurate: `authz/tokens.py:_secret()` already fails fast
  with a clear `ConfigError`, and `mint/verify_service_token` aren't wired into any
  live path yet (only `tests/unit/authz/test_tokens.py`). Fixing correct code would
  be churn. Future item (R-3): a boot-time presence check once service tokens are
  wired into the NATS path. Documented, not changed (per "don't fix what isn't broken").
- **B-2 (P0-3) — DONE.** Internal exception text was leaking to HTTP clients at four
  sites. Each now **logs the real cause internally** (`str(exc)[:200]`, structlog)
  and returns an **opaque `detail`**; the engine path adds `request_id=` so support
  can correlate; `raise ... from exc` added for chain preservation. HTTP status codes
  (502/500) preserved — clients may key on them.
  - `api/app.py` — `OneOpsError` + generic branches: opaque "engine failure
    (request_id=…)".
  - `api/uc08_routes.py:290/490/537` — "text extraction failed" / "catalog search
    failed" / "catalog rerank failed".
  - Reviewed + KEPT: `uc02_routes.py:183` `detail=str(e)` on a `ResolveError` — a
    400 user-facing validation message, not an internal leak (making it opaque would
    hurt UX). Noted in audit.
- **Files**: `src/oneops/api/app.py`, `src/oneops/api/uc08_routes.py`,
  `tests/unit/api/test_error_no_leak.py` (new, 2 devil's-advocate tests).
- **Validation**: new tests force a `OneOpsError` and a `RuntimeError` through
  `/api/chat` and assert the 500 body is opaque (no secret/DSN/class-name) but
  carries `request_id` — 2 pass; ruff + mypy clean; `make ci-fast` green.
- **Result**: P0-3 resolved (R-4 closed); P0-2 reframed (R-3). No contract changed.

## 2026-06-04 · Batch C-3 — streaming error paths: log internally, stay opaque (P1-2/P0-3)

- **What**: The streaming error paths were the analog of the Batch-B HTTP leak.
  Fixed both: `event_stream` (streaming.py) already logged but **leaked `str(exc)`
  to the client** in the final `error` field → now opaque (`stream failed
  (request_id=…)`). `_stream_turn` (app.py) **neither logged nor was opaque**
  (`f"stream error: {exc}"`) → now logs the cause internally + returns an opaque
  `final_response` with `request_id`. `publish_tool`'s re-raise was already correct
  (untouched).
- **Files**: `src/oneops/api/streaming.py`, `src/oneops/api/app.py`,
  `tests/unit/api/test_stream_error_no_leak.py` (new).
- **Test note (honest)**: the hermetic `event_stream` test fully covers the
  streaming.py fix. The chat-door `_stream_turn` is a closure whose only test path
  is a full TestClient streaming lifespan — which **hangs when multiple app-building
  modules share a process** (test-infra issue, not a product bug; tracked R-11). So
  the app.py change is covered-by-pattern (identical to the tested helper + the
  tested Batch-B non-stream handler), not by a direct endpoint test. Documented, not
  hidden.
- **Validation**: cross-module combo that previously hung now passes (8/8, EXIT 0);
  ruff + mypy clean. Pre-commit full-gate is the regression.

## 2026-06-04 · Review finding #6 — executor HIL interrupt() is NOT production-wired

- **What (investigation, no code change yet)**: A read-only agent confirmed the
  executor's `interrupt()` (nodes.py:750) is **test-only / dead in production**:
  no `Command(resume=...)` exists in `src/` (only tests); `thread_id` is a fresh
  `request_id` per request (app.py:421,2056,2099) so a paused thread can never be
  resumed; default checkpointer is in-memory (app.py:635). The real write-action
  UCs use their OWN approval: UC-8 = DB `approval`/`blocked` state with
  `langgraph_interrupt_id=None` (executor.py:270-290), **no `/approve` endpoint**,
  dead `record_approval_decision` (db.py:523, zero callers); UC-5 = `/propose`+
  `/decide` (graph.py:15, "NO interrupt()").
- **Implication**: production approval works (via the UCs), but the executor
  interrupt path is dead code with **stale docstrings** (uc08 contracts.py:360-363,
  __init__.py) claiming a LangGraph-interrupt + `/api/uc08/approve` flow that does
  not exist. Review finding #2 (interrupt placement) is therefore **moot for prod**.
- **Follow-ups (low-risk, tracked R-12)**: correct the stale docstrings; later decide
  to either wire the interrupt properly (stable thread_id + resume endpoint + PG
  checkpointer) or remove the dead interrupt/`record_approval_decision`.

## 2026-06-04 · Batch C-6 — turn timeouts env-tunable (P1-1)

- **What**: The hard-coded turn-timeout literals (in-process `run_turn` 60s; NATS
  inner 60s / outer 65s; `GraphWorker` 90s) are now typed `Settings` fields
  (`turn_timeout_seconds`, `turn_nats_outer_timeout_seconds`,
  `graph_worker_timeout_seconds`) read at the call sites — NOT new `os.getenv`
  scatter (extends the existing typed config, per audit P2-1).
- **Defaults equal the old literals → zero behavior change** unless an operator sets
  `TURN_TIMEOUT_SECONDS` / `TURN_NATS_OUTER_TIMEOUT_SECONDS` /
  `GRAPH_WORKER_TIMEOUT_SECONDS`. GraphWorker keeps an explicit-arg override.
- **Files**: `src/oneops/config.py`, `src/oneops/api/app.py`,
  `src/oneops/workers/graph_worker.py`, `tests/unit/test_turn_timeout_settings.py` (new).
- **Validation**: smoke 5/5; 6/6 timeout tests (defaults match old literals; outer ≥
  inner; env overrides flow through; worker uses settings + explicit-arg wins);
  ruff + mypy clean.

## 2026-06-04 · LangGraph review #5 — per-node RetryPolicy on LLM decision nodes

- **What**: Added `RetryPolicy(max_attempts=3, retry_on=UpstreamError)` to the two
  LLM-bearing DECISION nodes — `route` and `control_gate` — via `add_node(...,
  retry_policy=...)` (the typed kwarg; `retry=` works at runtime but fails mypy).
- **Why scoped this way**: both nodes are read-only/idempotent (classify + plan, no
  writes), so re-running on a transient blip is safe. `retry_on=UpstreamError` retries
  only transient infra faults (LLM upstream/timeout/rate-limit, cache, NATS); logic
  errors fail fast (no wasted retries), and gateway-exhausted `LLMGatewayError` is NOT
  retried again (the gateway already did its own internal retries — no amplification).
  The action-capable `run_step` node deliberately gets NO retry (avoid double-executing
  writes).
- **Files**: `src/oneops/executor/graph.py`,
  `tests/unit/executor/test_node_retry_policy.py` (new).
- **Validation**: smoke 5/5; 5/5 retry tests (UpstreamError retried→succeeds; ValueError
  + ConfigError NOT retried; attempts capped then re-raised; real graph wires retry on
  route+control_gate and NOT on run_step); ruff + mypy clean. Satisfies rule §2.8.

<!-- Append new entries below this line as batches land. -->
