# Project Scope — Executor Scheduler Refactor (LangGraph review #1 + #3)

> Status: SCOPING ONLY (2026-06-04). No code changed. This plans the highest-risk,
> highest-leverage item from the LangGraph review: the hand-rolled `wave ⇄ run_step`
> loop. Owner sign-off required before any implementation.

## 1. The finding (what the review said)
`graph.py:137-139`, `nodes.py:1074-1109`, `nodes.py:661-662` (`wave` no-op node).
A no-op `wave` node + a `run_step → wave` back-edge form a loop. Each superstep,
`dispatch_wave` re-reads the whole `plan` + `step_results` channels, recomputes the
runnable set, and emits a fresh `list[Send]`. Verdict ❌: a breadth-first topological
scheduler re-implemented in graph topology — the reason `recursion_limit` was raised
to ~60 (`graph.py:168`); O(plan²) re-derivation; a deep chain can silently approach
the limit.

## 2. The nuance the review under-weighted (READ THIS FIRST)
The review's headline fix — *"just fan out once from `route` → `run_step` → `aggregate`"* —
**only covers static, fully-parallel plans.** The loop is **load-bearing** for two
capabilities a single fan-out cannot express:

1. **Dependency waves** — `dispatch_wave` runs a step only when all its `depends_on`
   results exist (`nodes.py:1083-1085`). Sequential/dependent plans (e.g. the data-flow
   binding: summarize → bind `root_cause` → KB) need ordered waves. The binding's
   `_previous_results` is built here from completed deps (`nodes.py:1098-1107`).
2. **Runtime-dynamic step generation** — `run_step` can append NEW steps to the `plan`
   channel (`nodes.py:799-836`, `merge_plan` reducer). The full step set is **not known
   at `route` time**; a step discovers follow-up work and `dispatch_wave` picks it up a
   later wave. A one-shot fan-out from `route` structurally cannot see steps that don't
   exist yet.

**Therefore the refactor is NOT "delete the loop."** It is: *make the iteration
idiomatic and cheaper without losing dependency-ordering or runtime generation.* Any
proposal that drops those is a regression, not a fix.

## 3. Goals / Non-goals
**Goals**
- Remove the no-op `wave` node (the "scaffolding tell") and a hop per wave.
- Reduce superstep consumption so `recursion_limit` need not be inflated; remove the
  latent "deep plan hits 60 mid-turn" failure.
- Express scheduling with idiomatic primitives (Send fan-out + conditional fan-in,
  and/or a subgraph), not a no-op-node loop.

**Non-goals (MUST be preserved — see §6 invariants)**
- Parallel, sequential, AND dependent execution semantics.
- Runtime-dynamic step generation (+ its budgets + anti-hallucination guard).
- Data-flow binding determinism (`_previous_results` as a pure projection of the
  durable `step_results` channel).
- `blocked`/deadlock surfacing, no silent failures.
- No contract change: routes, NATS subjects, schemas, `run_turn` signature, env vars.

## 4. Options (ranked)

### Option A — Idiomatic loop, no-op node removed (RECOMMENDED, lowest risk)
Delete the `wave` node. Make `run_step`'s fan-in route via a conditional edge that
either emits the next `Send` batch (the same `dispatch_wave` logic, moved onto the
`run_step → ?` edge) or goes to `aggregate`. The first batch is emitted from a
conditional edge on `route` instead of through `wave`.
- **Keeps:** dependency waves, runtime generation, all reducers/bindings unchanged.
- **Wins:** removes the no-op node + one hop/wave (≈ halves supersteps/wave); keeps the
  dynamic-generation capability intact.
- **Risk:** LOW-MED. Same scheduling logic, fewer hops. Mostly edge-rewiring.
- **Effort:** ~1–2 days incl. characterization tests.

### Option B — Static fan-out + subgraph for dependency levels / generation
Single fan-out from `route` for the common parallel case; a compiled **subgraph**
handles dependency levels; runtime-generated steps recurse into the subgraph.
- **Keeps:** semantics, if carefully built.
- **Wins:** most idiomatic; clearest separation.
- **Risk:** HIGH. Rewrites the core execution path; subgraph + dynamic Send + recursion
  interaction is subtle; biggest blast radius on the exact code the whole system runs.
- **Effort:** ~4–6 days. Its own hardening cycle.

### Option C — Leave as-is, only de-risk the recursion limit
Don't restructure; instead make `recursion_limit` provably safe (derive it from plan
size + max generation depth/width so it can't be silently hit) and document the loop.
- **Wins:** near-zero risk; kills the one *real* production failure mode (silent limit).
- **Loses:** the idiomatic-cleanliness goal; the no-op node stays.
- **Effort:** ~0.5 day.

## 5. Recommendation (phased)
1. **Phase 0 — Characterization tests FIRST** (do regardless of option). Capture current
   behavior as golden: single step, N independent (parallel, one wave), linear
   dependent chain, diamond DAG, runtime-generated step (1 + nested), deadlock/malformed
   plan, data-flow binding end-to-end, blocked-surfacing. These become the regression
   oracle the refactor must not move.
2. **Phase 1 — Option C** immediately (cheap, kills the real prod risk: derive/justify
   `recursion_limit`, log when a plan approaches it). Bank the safety win even if the
   structural refactor slips.
3. **Phase 2 — Option A** (idiomatic loop, no-op node removed), under the Phase-0 tests.
4. **Phase 3 — Option B** only if, after A, the team still wants full subgraph
   expression. Treat as a separate project with its own sign-off.

Rationale: the only finding with a *production* consequence is the recursion-limit
coupling — Phase 1 neutralizes that for ~0.5 day at near-zero risk. The structural
cleanliness (Phase 2) is real but cosmetic-adjacent; do it under a golden-test net.
Phase 3 (subgraph rewrite) is high-risk and optional.

## 6. Invariants the refactor MUST preserve (acceptance contract)
- `merge_step_results` / `merge_plan` reducers unchanged (pure, associative, dedup-by-id).
- `_previous_results` remains a pure projection of `step_results` (replay-deterministic).
- Runtime generation: budgets (`DEFAULT_MAX_GENERATION_DEPTH/_PER_STEP`) + closed-
  vocabulary anti-hallucination guard + policy/RBAC re-gating per generated step.
- `blocked`/deadlock steps surfaced in `aggregate` (never silent).
- Identical observable behavior on the Phase-0 golden scenarios.
- Zero contract change (routes/subjects/schemas/env/`run_turn` signature).

## 7. Risk + rollback
- **Risk:** this is THE code every turn runs. A subtle scheduling regression could break
  parallel/dependent/generation execution for all UCs. Mitigations: Phase-0 golden tests
  as oracle; one phase per PR; full 1510-gate + devils per phase; `cfc3832` rollback.
- **Rollback:** each phase is its own commit; revert is `git reset` to the prior tag.

## 8. Validation ladder (per phase)
smoke → Phase-0 golden tests → full unit gate (1510+) → integration (live infra) →
routing devils → regression vs baseline (1556 passed / 81 deselected; devils 98/100).
No phase advances on red.

## 9. Decisions needed from owner before Phase 2+
1. Do we want only the **safety** win (Phase 1/Option C) or the **structural** refactor
   (Phase 2/Option A) too?
2. Is **Option B** (subgraph rewrite) in scope at all, or explicitly out?
3. Acceptable `recursion_limit` policy: derived-from-plan (auto) vs a documented cap?

Related review items: **#3** (runtime step-gen → Send/subgraph) is resolved *by* Phase 2/3
and must not be done separately. **#4** (`astream`) is independent — not part of this project.
