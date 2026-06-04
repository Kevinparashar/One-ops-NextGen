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
| R-7 | **Hard-coded timeouts** — no operator knob. | Medium | **RESOLVED 2026-06-04 (Batch C-6)** | Typed Settings fields (`turn_timeout_seconds`/`turn_nats_outer_timeout_seconds`/`graph_worker_timeout_seconds`); defaults = old literals (zero behavior change); env-overridable. Test-locked. |
| R-8 | **Pre-existing test flakes** (time_filter, session_continuity, uc08; integration perf-load). | Low | Accepted/known | Track; do not attribute to new changes. Stabilize opportunistically. |
| R-9 | **Smoke + devils CI stages are placeholders.** | Medium | Open (Phase 6c) | Implement `scripts/smoke_routing.py`, `scripts/devils_play.py`. |
| R-10 | **No Dockerfile / k8s** — docker-compose only. | Low (current phase) | Accepted | Add at deployment time. |

| R-11 | **Test-infra hang**: multiple app-building test modules sharing a process can hang on TestClient streaming-lifespan teardown. | Low (tests only) | Open | Smoke uses a module-scoped app to avoid it; the C-3 stream endpoint test was made hermetic. Proper fix: a shared session-scoped app fixture for api/ tests. |
| R-12 | **Executor `interrupt()` HIL is dead in production** — no `Command(resume)` in src/, per-request thread_id, in-memory checkpointer. UC approval is separate (UC-8 DB-blocked, UC-5 propose/decide). | Medium (correctness/clarity) | **(a) DONE 2026-06-04** / (b) open | (a) ✅ Stale docstrings corrected (uc08 `contracts.py` ApprovalState + Approval, `__init__.py` module doc + layout, `db.py` `record_approval_decision` flagged NOT-WIRED). (b) DOWNGRADED to dormant (reachability checked 2026-06-04): the only ACTIVE agents are uc01/02/03/05/08; the catalog action agents (ticket_action_agent etc.) are INACTIVE/unreachable, and the active action-tier UCs (uc05, uc08) use their OWN approval (UC-8 DB-state, UC-5 propose/decide), NOT the chat executor `interrupt()`. So the executor interrupt path is UNREACHED in prod — not an urgent bug. Owner decision (low priority): when an action agent is ever chat-routed through the executor, wire resume (Command(resume) endpoint + stable thread_id + checkpointer) before relying on it. From LangGraph review #6 + #2. |

## LangGraph review (2026-06-04) — findings tracked
Overall: idiomatic + in the right direction; reducers/checkpointer-guard/thread_id are strong. Open items:
- **#1 (high, P2)** `wave ⇄ run_step` is a hand-rolled scheduler (no-op `wave` node + back-edge re-derives runnable set every superstep → forces `recursion_limit=60`). Idiomatic: single fan-out from `route` for parallel plans, or a subgraph per dependency level. High leverage, high blast radius — dedicated effort.
- **#2 (moot for prod, see R-12)** interrupt() placement after before-hooks.
- **#3 (P2)** runtime step-gen via plan-channel mutation → should be dynamic `Send`/subgraph (coupled to #1).
- **#4 (P3)** streaming via side event-sink, not `astream(stream_mode="custom")`.
- **#5 (P1)** ✅ RESOLVED 2026-06-04 — `RetryPolicy(max_attempts=3, retry_on=UpstreamError)` on `route`+`control_gate` (idempotent); `run_step` deliberately excluded. Test-locked.
Keep-list (do NOT change): reducers (state.py:18-66), checkpointer shared-DB guard (graph.py:201-254), thread_id resume wiring, `ToolNode` deliberately unused.

## LangGraph re-review (2026-06-04, fresh against current code) — outcomes
Label: "Directionally good but needs hardening." Confirmed our fixes are now correct
+ idiomatic (RetryPolicy scoping, recursion floor, golden tests — "do not touch").
- **NOTABLE DISAGREEMENT (wave loop):** review #1 called the `wave ⇄ run_step` loop a
  ❌ hand-rolled-scheduler anti-pattern; the fresh review calls it ✅ idiomatic ("the
  canonical dynamic-dispatch pattern… do NOT touch it"). Two skeptical reviewers,
  opposite verdicts → it's a judgment call, not a clear defect. Implication: do NOT
  rush the Phase-2 scheduler rewrite; a human LangGraph expert is the tiebreaker if
  certainty is needed. (Affects scheduler-refactor-scope.md priority — leans "leave it".)
- **R-13 (C2) — FIXED 2026-06-04:** checkpointer docstrings claimed cross-turn state
  carry, but prod uses per-request thread_id → checkpointer carries nothing across
  turns; cross-turn memory = session store. Docstrings corrected (graph.py run_turn,
  nodes.py update_focus). Doc-only.
- **R-14 (C9) — FIXED 2026-06-04:** `langgraph>=0.2.50` pin vs installed 1.2.2 (4 majors
  apart) → pinned `langgraph>=1.2,<2` + `langgraph-checkpoint-postgres>=3,<4` (both
  match installed). Removes clean-install break risk.
- **R-15 (C8/C11) — FIXED 2026-06-04:** test used deprecated `retry=` alias → `retry_policy=`;
  collapsed a redundant double `InMemorySaver` construction.
- **C1 (interrupt dead-in-prod):** reconfirmed (= R-12). Owner decision, dormant.

| R-16 | **Code smell (not dead code): `target_labels` param ignored.** `_resolve_linked_field_read` (`uc01_summarization/tools.py:591`) takes a keyword-only `target_labels: list[str]` that live callers pass but the body never reads. Possible latent bug (intended filtering not applied) — vulture flags it 100% but it's an interface param, NOT removable dead code. | Low | Open (review, do not delete) | Investigate whether the linked-field-read should filter by `target_labels`; either use it or drop it from signature + call sites. Flagged during the 2026-06-04 intra-.py dead-code sweep. |

## Intra-`.py` dead-code status (2026-06-04 sweep)
ruff `F` rules are enabled and the gate is green → **zero unused imports / unused local
assignments in `src/`** (enforced every commit). vulture @≥80% yields only 3 hits, all
**false positives** (2 = required structlog processor signature `logger`/`method_name`
at `observability/__init__.py:229`; 1 = the R-16 interface param). **0 orphaned modules,
0 removable dead functions** beyond `record_approval_decision` (removed). Conclusion:
no additional removable intra-file dead code.

## Manual verification required

- ✅ DONE (2026-06-04): `.env` / `.env.shared-stack.bak` were NEVER committed in
  history (`git log --all --full-history -- .env .env.shared-stack.bak` is empty).
  R-2 is local exposure only → action narrows to **rotate keys before deploy**.
- Spot-check all FastAPI exception handlers for raw-traceback leakage beyond R-4 sites.
- Confirm intended runtime feature-flags (focus-migration, binding) stay env-readable
  (not frozen) if/when config is centralized (Phase 3b).
