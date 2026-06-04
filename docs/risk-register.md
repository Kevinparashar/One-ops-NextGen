# Risk Register — Oneops-NextGen V1

> Status: 2026-06-04. Risks we are NOT fixing immediately (deferred, external, or
> require approval), plus security items needing manual action. Live document.

| ID | Risk | Severity | Status | Owner action |
|---|---|---|---|---|
| R-1 | **DAL not yet defined.** Production requires a system-wide Data Access Layer (Option A: existing platform service we integrate through). UC-2/5/8 currently issue raw SQL / own pools. | High (structural) | **Deferred — by decision** | Do NOT consolidate raw SQL behind ports until the DAL contract (protocol + capabilities) is confirmed. Then execute refactor-plan 3c. KG/ITOM data work waits on this. |
| R-2 | **Live secrets in untracked `.env`.** Real OpenAI key + Supabase password present locally. `.env` is gitignored (NOT committed — verified); `.env.shared-stack.bak` + `*.bak` now ignored (`cfc3832`). | High (security) | Open | **Rotate the OpenAI key and Supabase password before any real/shared deploy.** Confirm no secret ever reaches a tracked file. |
| R-3 | **`AUTHZ_JWT_SECRET` boot-time check.** Audit P0-2 was inaccurate: `_secret()` already fails fast with a clear `ConfigError`, and service tokens aren't wired into a live path yet. | Low | Reframed (future) | No change today. When service tokens are wired into the NATS path, add a boot-time presence check so a missing secret fails at startup, not first-use. |
| R-4 | **Client-facing exception text leak** (`{exc}` in HTTP detail). | Medium (security) | **RESOLVED 2026-06-04 (Batch B)** | 4 sites now log internally + return opaque `detail` (+ request_id) with `from exc`; status codes preserved. `uc02:183` reviewed + kept (400 validation message, not a leak). Devil's-advocate test `test_error_no_leak.py` locks it. |
| R-5 | **CI unit-coverage gap** — gate ran ~1% of unit tests (19/1591). | Medium | **RESOLVED 2026-06-04 (Batch A1)** | Gate now runs the full 1510-test hermetic unit lane (`-m unit` + auto-marker), green in ~102s. Follow-up: `ci-fast` is ~1:42; if too slow per-commit, add `pytest-xdist` for parallelism (deliberate dependency decision — not taken unilaterally). |
| R-6 | **No NATS idempotency** — re-delivery can double-execute writes (UC-5/8). | Medium | Open (Phase 4c) | Dedup by `request_id`. |
| R-7 | **Hard-coded timeouts** — no operator knob. | Medium | Open (Phase 4a) | Env-parameterize with current values as defaults. |
| R-8 | **Pre-existing test flakes** (time_filter, session_continuity, uc08; integration perf-load). | Low | Accepted/known | Track; do not attribute to new changes. Stabilize opportunistically. |
| R-9 | **Smoke + devils CI stages are placeholders.** | Medium | Open (Phase 6c) | Implement `scripts/smoke_routing.py`, `scripts/devils_play.py`. |
| R-10 | **No Dockerfile / k8s** — docker-compose only. | Low (current phase) | Accepted | Add at deployment time. |

## Manual verification required

- ✅ DONE (2026-06-04): `.env` / `.env.shared-stack.bak` were NEVER committed in
  history (`git log --all --full-history -- .env .env.shared-stack.bak` is empty).
  R-2 is local exposure only → action narrows to **rotate keys before deploy**.
- Spot-check all FastAPI exception handlers for raw-traceback leakage beyond R-4 sites.
- Confirm intended runtime feature-flags (focus-migration, binding) stay env-readable
  (not frozen) if/when config is centralized (Phase 3b).
