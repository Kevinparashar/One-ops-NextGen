# Production-Readiness Audit — Oneops-NextGen V1

> Status: 2026-06-04. Read-only audit across the dimensions in the engagement
> brief. Severity = impact × likelihood in production. Every item cites file:line.
> Nothing here is fixed yet; remediation order is in `docs/refactor-plan.md`.

## Baseline (captured before any change)

- `make ci-fast` (ruff + mypy + `pytest -m unit`): **green** — ruff clean, mypy clean
  (184 files), marked-unit 19 passed (2026-06-04).
- Full suite (run by directory, 2026-06-02, identical code): unit **1572 passed / 19
  failed** (all pre-existing flakes: time_filter ×13, session_continuity ×3, uc08 ×3);
  integration **77 passed / 4 failed** (3 perf-load budget + 1 uc08 LLM-flake,
  passes on retry); routing devils 98/100 (2 pre-existing summarize edges).
- These are the regression goalposts: changes must not move them adversely.

## Severity-ranked findings

### P0 — correctness / reliability / security (fix first; low blast radius)

| # | Finding | Evidence | Why it matters |
|---|---|---|---|
| P0-1 ✅RESOLVED (A1) | **CI gate unit stage runs ~1% of tests AND hides ~19 failing tests.** Fixed 2026-06-04: event-loop pollution → `asyncio.run`; settings-cache leak → autouse `cache_clear`; integration-class tests reclassified. Gate now runs the full 1510-test hermetic lane (green, 102s). See change-log Batch A1. `pytest -m unit` selects 19, deselects 1572. Crucially, the 19 known "flakes" are NOT infra-dependent — `test_time_filter_extractor` uses a stub gateway/cache, `test_session_continuity` forces local mode + in-process client — they fail only in the full-suite run = **cross-test global-state pollution** (isolation bug, same class as the earlier UC-3 fix). So the gate is green partly *because* it skips them. | `scripts/ci.sh` stage 3; live run 2026-06-04; `tests/unit/router/test_time_filter_extractor.py:18`, `tests/unit/api/test_session_continuity.py:34` | False confidence + masks real failures. Expanding the gate naively turns it red. Correct fix = fix the 19 isolation failures FIRST (reset polluting globals/env in fixtures), THEN expand the gate to the full `tests/unit/` dir. Not a one-liner. |
| P0-2 ⚠️CORRECTED (not a defect) | Original finding ("`AUTHZ_JWT_SECRET` fails with `None`") was **inaccurate**. `authz/tokens.py:_secret()` already fails fast with a clear `ConfigError` ("AUTHZ_JWT_SECRET is not set… there is no default") — the desired behavior. Also, `mint/verify_service_token` are exported but **not yet wired into any live request/NATS path** (only `tests/unit/authz/test_tokens.py` uses them). | `authz/tokens.py:43-50` | No change made — fixing correct code would be churn. FUTURE (not now): add a boot-time presence check once service tokens are wired into the NATS path, so a missing secret fails at startup rather than first-use. Tracked R-3. |
| P0-3 ✅RESOLVED (Batch B) | **Internal exception text leaked to HTTP clients.** `detail=f"engine failure: {exc}"`, `f"text-extract failure: {exc}"`, `f"search failure: {exc}"`, `f"rerank failure: {exc}"`. | `api/app.py:2092`, `api/uc08_routes.py:290,490,537` | Fixed 2026-06-04: each now logs the real cause internally (`str(exc)[:200]`) and returns an opaque `detail` (app.py adds `request_id=` for support correlation); `from exc` added. Status codes unchanged. `uc02_routes.py:183` (`detail=str(e)` on a `ResolveError`) reviewed + KEPT — it's a 400 user-facing validation message, not an internal leak. |
| P0-4 | **Secret hygiene.** Local (untracked, gitignored) `.env` holds a real OpenAI key + Supabase password; `.env.shared-stack.bak` held 7 secret lines (now gitignored, commit `cfc3832`). | `.env`, `.gitignore` | `.env` is NOT committed (verified). Residual risk = local exposure; **rotate before any real deploy.** Tracked in risk-register. |

### P1 — reliability hardening

| # | Finding | Evidence | Why it matters |
|---|---|---|---|
| P1-1 | **Hard-coded timeouts, not operator-tunable.** chat `nats_invoke(timeout_s=60.0)` / `wait_for(..., 65.0)`, `run_turn(..., 60.0)`, NatsStepExecutor `60.0`, GraphWorker `90.0`. | `api/app.py` (~2000-2021), `api/nats_invoker.py:45`, `executor/nats_step_executor.py:26`, `workers/graph_worker.py:46` | Latency tuning requires a code change; no env knob. (Contrast: NATS retry/breaker ARE env-tunable.) |
| P1-2 | **Broad `except Exception` loses root cause on stream paths.** | `api/streaming.py:57`, `api/app.py:1597` | Stream failures collapse to a generic JSON error with no logged cause → hard to diagnose in prod. |
| P1-3 | **No idempotency on NATS worker handlers.** `_handle` runs the turn with no dedup by `request_id`. | `workers/graph_worker.py:74`, `workers/agent_worker.py:64` | NATS re-delivery (ack race / reconnect) → turn executes twice; writes (UC-5/8) could double-apply. |
| P1-4 | **Silent cache-write failures at the app layer.** `except Exception: pass`. | `api/app.py:1525`, `api/app.py:1897`, `api/uc02_routes.py:284` | App-level swallow hides degradation (the adapter layer DOES emit metrics; the app wrapper does not). At least log/count. |
| P1-5 | **Unbounded request payloads.** `FastPathPostRequest.inputs: dict[str,Any]` (no size cap); `MatchRequest.sr_category` unbounded. | `api/app.py:~297`, `api/uc08_routes.py:98` | Large/nested inputs → memory + slow SQL/embeddings; DoS surface. (chat `message` IS capped at 4000.) |
| P1-6 | **UC-8 template substitution recurses without a depth guard.** | `uc08_fulfillment/core.py:96-111` | Deeply nested template vars → unbounded recursion. |

### P2 — maintainability / structure (sequence carefully; some DAL-gated)

| # | Finding | Evidence | Why it matters |
|---|---|---|---|
| P2-1 | **Config scatter.** ~114 `os.getenv` across ~32 files; UC-8 alone ~13. Worst: untyped float/int parses with string fallbacks. | `config.py` (truth) vs `uc08_fulfillment/catalog_search.py:20-47`, `uc03_kb_lookup/handlers.py:123-136`, `executor/graph.py:190-206` | No central audit of knobs; inconsistent parsing/defaults. NOTE: some reads are intentional runtime flags — do NOT blindly centralize those. |
| P2-2 | **Inconsistent response shapes across UCs.** UC-3 uses `@dataclass`; others Pydantic. Outcome vocab differs (`found/not_found` vs `status` vs none). | `uc03_kb_lookup/handlers.py:43-56` vs `uc0{1,2,5,8}` contracts | Frontend/clients must special-case each UC. Document a canonical envelope; **do not break existing shapes without approval** (contract). |
| P2-3 | **Duplicated hybrid retrieval (FTS+vector RRF).** Same K=60 / fuse logic in two places. | `uc03_kb_lookup/handlers.py:~260-306`, `uc05_triage/retrieval/similarity_search.py:88-138` | Drift risk; consolidate into a shared retriever (medium-risk refactor). |
| P2-4 | **Magic numbers/thresholds in code, not config.** sem floors, dup/res thresholds, fuse scores. | `uc02/core.py:56-82,187-200`, `uc03/handlers.py:231-235`, `uc05/check_duplicates.py:46-82` | No tuning/A-B without code change; name them as constants/config. |
| P2-5 | **DAL gap (DEFERRED).** UC-2/5/8 issue raw SQL / own pools instead of going through a data-access port. | `uc02/core.py`, `uc05/retrieval/similarity_search.py`, `uc08/db.py` | Production mandates a single DAL boundary. **Deferred until DAL contract confirmed** — document only, do not migrate now. |
| P2-6 | **Dead/legacy entry points.** Console scripts point at `oneops.entry.*` modules that don't exist. | `pyproject.toml:61-64` | Confusing; `pip install` exposes broken `oneops-graph`/`oneops-uc1`/`oneops-uc3`. |
| P2-7 | **`raise`-without-`from` debt (B904 ignored).** | `pyproject.toml:109` | Loses exception chains in some `except` blocks; sweep gradually. |

### Dimension notes (where the codebase is already strong)

- **Architecture**: clean layering (api → router → executor → handlers → adapters);
  agents-as-data registries; LangGraph-native reducers/Send/checkpointer.
- **Resilience (NATS/LLM/cache)**: bounded retries, circuit breaker, single-flight,
  graceful degradation — all env-tunable, all observable.
- **Observability**: structlog + OTel, trace correlation, PII-safe text attrs.
- **Errors**: rich typed hierarchy with `.code`; mostly disciplined `from exc`.
- **Tenant isolation**: structural — `tenant_id` is the leading predicate/key everywhere.
- **Clients**: Postgres/Dragonfly/NATS are reused singletons (no per-call re-init).

## Devil's-advocate exposure matrix (current behavior)

| Scenario | Current handling | Gap |
|---|---|---|
| Empty/oversized input | chat capped (4000); fast-path/uc08 partly uncapped | P1-5 |
| Missing config | pydantic fails fast EXCEPT `AUTHZ_JWT_SECRET` | P0-2 |
| Dependency timeout | timeouts present but hard-coded | P1-1 |
| Failed/malformed LLM output | UC-1/3 fall back; UC-5 assumes schema | P2 (UC-5 validation) |
| Queue duplicate delivery | no dedup | P1-3 |
| Cache miss/stale | graceful; app-level write errors swallowed | P1-4 |
| DB/network failure | propagates (no UC-level retry — acceptable) | monitor |
| Secret in logs | guarded (safe_attrs) | OK |
| Breaking public contract | routes/subjects/schemas catalogued | preserve (refactor-plan) |
