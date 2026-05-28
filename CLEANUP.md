# CLEANUP.md — POC-5-MW Phase 1 Inventory & Prune

**Date:** 2026-05-20
**Scope:** Phase 1 of the production rebuild — inventory the repo, remove dead
code / abandoned scaffolding / stale artifacts, preserve all durable assets.

---

## Honest framing first

This repo is **not** a 1000-agent system needing a heavy prune. It is an
actively-developed POC with **3 use cases built** (UC-1 summarization, UC-3 KB
lookup, UC-99 conversational). It is already clean: there is almost no dead
*code*. The genuine cleanup is empty scaffolding directories and stale run
artifacts.

The large architectural "removal" implied by the brief — the old routing layer —
is **deliberately deferred**, not done in Phase 1. See "Deferred, not removed"
below. Deleting a stress-passing production path before its replacement exists
would violate the 5-year-horizon discipline.

---

## Removed

| Path | Type | Why |
|---|---|---|
| `src/oneops/entry/` | empty directory | Empty package. `[project.scripts]` references `oneops.entry.graph_service` / `oneops.entry.uc_service` — **modules that never existed**. Broken scaffolding; the rebuild's Phase 3 recreates real service entrypoints. |
| `src/oneops/proto/` (`proto/oneops/`) | empty directory tree | Codec scaffolding placeholder. No `.proto` files were ever generated. ADR-0001 picks the codec; the rebuild creates this fresh with real schemas. |
| `tests/stress/results/` | 38 stale `.md` files | Run artifacts from stress runs dated 2026-05-14 → 2026-05-16. Not tests, not code — historical output. Belongs in CI artifact storage, not the repo. |
| `.pytest_cache/` | build artifact | Regenerated on every `pytest` run. Already in `.gitignore`. |
| `src/oneops.egg-info/` | build artifact | Regenerated on `pip install -e .`. Already in `.gitignore`. Also carried the stale broken `entry_points.txt` referencing the non-existent `entry` modules. |

**LOC impact:** 0 lines of Python source removed — every removed item was an
empty directory or a non-`.py` artifact. `src/` Python LOC unchanged at **20,874**.
**Dependency impact:** 0 — no vendored libraries were found; `pyproject.toml`
dependency set unchanged.

---

## Kept — looked suspicious, verified live

| Path | Why it stays | Call-site evidence |
|---|---|---|
| `tests/stress/_verdict_guard.py` | Load-bearing test infra — the optimism-bias safeguard every smoke routes through. | 8 referencing test files (`grep -rl _verdict_guard tests`). |
| `tests/stress/_port_poc3_probes.py` | Probe corpus ported from POC3; consumed by the stress probe set. | 1 referencing file. |
| `tests/stress/_soak_test.py`, `_tier1_edge_probes.py`, `_rerun_failed.py`, `_targeted_rerun.py` | **0 references each** — not wired into the pytest suite. **Retained anyway** as operable ops/perf harnesses (soak, tier-1 concurrency, targeted reruns). The brief says "delete if not wired in", but these encode real load/soak capability the brief's own Phase 4 requires. Deleting them to rebuild equivalents is waste. **Action:** folded into the Phase 4 load/chaos suite rather than discarded. *(Conflict surfaced — see below.)* |
| `showcase/` | Standalone demo UI (`app.py`, `serve.py`, `chat.html`). Not on the request path; not imported by `src/`. Imports the current graph API, so it still runs. **Retained** as a manual demo surface; will be re-pointed at the new engine in Phase 3, or retired then. |
| `src/oneops/graph/nodes.py::_legacy_planner_node` | Marked "future cleanup: delete entirely" but still reachable via `PLANNER_MODE=legacy`. **Retained** until the new executor is wired and proven, then removed. |
| `src/oneops/caching/` | Looked like an empty stub (`__init__.py` only). Verified: imported by 10+ modules (UC-1/UC-3/UC-99 handlers, routing, safety, quality). Live. |
| `data/`, `registries/`, `contracts/`, `ops/`, migrations, `.env.example`, `docs/policies/updated_policy_v2.md` | Durable assets — explicitly preserved per the brief's "Keep" list. |

---

## Deferred, not removed (architectural replacement, not Phase-1 cleanup)

The brief's "Remove old routing prototypes" instruction cannot be executed in
Phase 1 without breaking a working system. These are **not dead code** — they
are the *current* production and in-progress paths:

| Component | Status | Disposition |
|---|---|---|
| `ROUTING_MODE=legacy` path | Production default; stress-passing; byte-identical baseline | **Kept as flag-protected fallback** until the new router+executor is validated in an integration environment. Removed in the migration plan's final phase. |
| `ROUTING_MODE=three_stage` (`src/oneops/routing/`) | In-progress current approach (decomposer → rewriter → verifier → shortlist → rerank) | **Replaced** by the new router (glossary → vector retrieval → ABAC/condition filter → LLM disambiguation). Removal sequenced in the migration plan, after the replacement passes parity. |
| `docs/design/routing-layer-architectural-review.md` (Option C+E) | Approved 2026-05-18 | **Superseded** by the production build brief. The C+E lesson (deterministic intent decision over retrieval scoring) is preserved: the new router's per-agent declarative *activation condition* is the deterministic decision layer C+E asked for. Doc retained for traceability with a superseded header. |

Rationale: removing routing code is an architectural migration with rollback
criteria, not a Phase-1 prune. It belongs in the migration plan, gated behind
parity validation — not in a cleanup pass.

---

## Conflicts surfaced

Per the brief's instruction to surface conflicts rather than silently compromise:

1. **"Delete if not wired in" vs. ops harnesses.** Four stress harnesses have 0
   suite references. Strict reading says delete. Engineering judgment says a
   soak/concurrency harness is ops tooling, not dead code, and the brief's
   Phase 4 explicitly requires load/chaos tests. **Resolution:** retained and
   folded into the Phase 4 suite. Flag if you want them gone instead.

2. **The repo is near-greenfield, not a 1000-UC catalog to prune.** Phase 1
   cleanup is therefore small. The real work is the build, not the prune.

---

## Before / after

| Metric | Before | After | Delta |
|---|---|---|---|
| `src/` Python LOC | 20,874 | 20,874 | 0 (no source removed) |
| `tests/` Python LOC | 15,911 | 15,911 | 0 |
| Empty scaffolding dirs | 2 (`entry/`, `proto/`) | 0 | −2 |
| Stale run artifacts | 38 files | 0 | −38 |
| Build-artifact dirs in tree | 2 (`.pytest_cache/`, `egg-info/`) | 0 | −2 (regenerate on demand) |
| `pyproject.toml` dependencies | unchanged | unchanged | 0 |
| Broken `[project.scripts]` refs | 3 | 3 (still present) | **see note** |

**Note on `[project.scripts]`:** the three console-script entries
(`oneops-graph`, `oneops-uc1`, `oneops-uc3`) still point at the now-removed
`oneops.entry.*` modules. They are left in `pyproject.toml` deliberately — Phase 3
rebuilds the service entrypoints under `oneops.entry.*`, at which point these
references become valid again. Removing and re-adding them would be churn.
Tracked as the first task of Phase 3.

---

## Definition-of-done for Phase 1

- [x] Inventory complete — import-graph scan + entrypoint trace.
- [x] Dead scaffolding and stale artifacts removed.
- [x] Durable assets verified present and untouched.
- [x] Suspicious-but-live files verified with call-site evidence.
- [x] Architectural replacements deferred to the migration plan, not force-deleted.
- [x] Conflicts surfaced rather than silently compromised.
