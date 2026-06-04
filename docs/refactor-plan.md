# Refactor Plan — Oneops-NextGen V1

> Status: 2026-06-04. Incremental, behavior-preserving, contract-safe. Each item
> is a small reviewable change validated by: targeted unit + relevant integration +
> regression-vs-baseline + devil's-advocate + risk note, then committed. Risky
> changes are NOT batched. Rollback point: `cfc3832`.

## Guardrails (apply to every change)

1. **Preserve contracts**: HTTP routes, NATS subjects, request/response schemas,
   handler signatures, env var names, DB tables, cache key formats, entrypoints.
   See the contract catalogue in `codebase-understanding.md §3` + audit.
2. **DAL is deferred**: do NOT migrate UC-2/5/8 raw SQL behind ports, do NOT change
   the data-access shape, until the DAL contract is confirmed (Option A). Document
   in risk-register; design every change to remain DAL-compatible.
3. **No behavior change** in Phase 2; behavior changes only where fixing a clear
   bug/reliability/security issue, called out explicitly with a regression test.
4. **Validate then move on** — never batch, never hide a red test.

## Phase 1 — Baseline validation  ✅ DONE (2026-06-04)

- Run commands captured (`make ci`, `make ci-fast`, `pytest tests/unit|integration`).
- Baseline green (ci-fast) + full-suite goalposts recorded (audit "Baseline").
- Critical use-case behavior captured (request flow, per-UC contracts).

## Phase 2 — Safe cleanup (no behavior change)

| Step | Change | Files | Risk |
|---|---|---|---|
| 2a | Remove/repair dead console-script entrypoints (P2-6) | `pyproject.toml:61-64` | low |
| 2b | Type hints / docstrings only where they aid clarity; no logic edits | targeted | low |
| 2c | Name a first tranche of magic numbers as module constants WHERE value is unchanged (P2-4, non-UC-behavioral first) | per-UC | low-med |

> ruff/formatting already clean — no mass reformat needed.

## Phase 3 — Structural hardening (interface-stable)

| Step | Change | Files | Risk |
|---|---|---|---|
| 3a | Document canonical response envelope; add a non-breaking adapter/notes (do NOT change UC-3 dataclass→Pydantic yet — contract) (P2-2) | docs + thin shims | med |
| 3b | Config: introduce typed accessors for the worst untyped `os.getenv` parses (UC-8 float/int) WITHOUT renaming env vars; leave intentional runtime flags as-is (P2-1) | `config.py`, `uc08_*` | med |
| 3c | (DAL-gated, DEFERRED) raw-SQL → port consolidation (P2-5) | UC-2/5/8 | — |
| 3d | (Deferred) shared hybrid-retriever extraction (P2-3) | UC-3/5 | med |

## Phase 4 — Reliability hardening

| Step | Change | Files | Risk |
|---|---|---|---|
| 4a | Parameterize timeouts via env with current values as defaults (no behavior change at default) (P1-1) | `app.py`, `nats_invoker.py`, `nats_step_executor.py`, `graph_worker.py`, `config.py` | med |
| 4b | Streaming `except`: log root cause + preserve context (still degrade gracefully) (P1-2) | `streaming.py`, `app.py:1597` | low |
| 4c | NATS worker idempotency: dedup by `request_id` (short-TTL guard) (P1-3) | `graph_worker.py`, `agent_worker.py` | med |
| 4d | Cap payloads: bound `FastPathPostRequest.inputs`, `sr_category` (P1-5) | `app.py`, `uc08_routes.py` | low |
| 4e | UC-8 substitution depth guard (P1-6) | `uc08/core.py` | low |
| 4f | UC-5 LLM output validation/fallback parity with UC-1/3 (P2 LLM) | `uc05/adapters.py` | med |

## Phase 5 — Observability & security

| Step | Change | Files | Risk |
|---|---|---|---|
| 5a | `AUTHZ_JWT_SECRET` fail-fast (clear boot error / Settings field) (P0-2) | `authz/tokens.py`, `config.py` | low |
| 5b | Mask client-facing exception detail; log full internally (P0-3) | `app.py:2063`, `uc08_routes.py:290` | low |
| 5c | Secret rotation runbook + confirm `.env*` ignore complete (P0-4) | docs, `.gitignore` | low |
| 5d | Startup `print()` → logger (optional) | `observability/__init__.py:138-150` | low |

## Phase 6 — Testing expansion

| Step | Change | Files | Risk |
|---|---|---|---|
| 6a | **Fix CI unit-coverage gap (P0-1)**: make the gate run the full `tests/unit/` dir (not just `-m unit`), OR mark all unit tests. Prefer the directory approach (no 1500-file edit). | `scripts/ci.sh`, `Makefile` | low |
| 6b | Add focused unit tests for every Phase 4/5 change | tests/unit | low |
| 6c | Stand up real `scripts/smoke_routing.py` + `scripts/devils_play.py` (CI stages 5/6 are placeholders) | scripts | med |
| 6d | Contract tests for routes/subjects/schemas to lock the catalogue | tests | low |

## Recommended execution order (front-load value, minimize risk)

1. **Batch A (Phase 6a + 2a)**: CI unit-coverage fix + dead entrypoint cleanup. Pure
   safety/hygiene, no runtime behavior change. Highest confidence gain.
2. **Batch B (Phase 5a + 5b)**: AUTHZ fail-fast + exception masking. Small, security.
3. **Batch C (Phase 4d + 4e + 4b)**: payload caps + depth guard + streaming context.
4. **Batch D (Phase 4a)**: timeout parameterization.
5. **Batch E (Phase 4c + 4f)**: idempotency + UC-5 output validation.
6. **Batch F (Phase 3a/3b, 2b/2c)**: response-envelope docs + config typing + naming.
7. **Deferred (DAL-gated)**: 3c raw-SQL→port, 3d shared retriever — await DAL contract.

Each batch: implement → `make ci-fast` → targeted full-dir unit subset → relevant
integration → devils → update change-log + risk-register → commit.
