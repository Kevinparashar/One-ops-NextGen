# Multi-Turn Focus Migration Runbook

**Owner:** OneOps graph maintainers
**Status:** Phase 9 — operational hardening complete. Phase 10 (PostgresSaver
default) NOT YET shipped. Do NOT flip to Stage 4 in production until Phase 10
ships and is validated under load.

---

## What this document covers

How to move the conversation **focus** from Dragonfly (legacy authoritative
store) to LangGraph state (new authoritative store) **without downtime, with
rollback at every step, and with audit-trail proof of correctness**.

The migration is staged via five environment flags. This doc lists every flag,
every valid combination, what to monitor at each stage, and how to roll back.

---

## Why the migration

The audit (May 2026) found that the same focus value was being written by
**three independent paths** — Dragonfly via `apply_focus_writes`, the
executor's `Command(update={"focus": ...})`, and the new
`commit_focus_logic`. With Dragonfly as the only durable store, a transient
Redis error could corrupt the conversation. The new design:

- LangGraph state (Postgres checkpointer) becomes the durable focus owner.
- Dragonfly becomes cache-only.
- `aggregator_node` → `commit_focus_logic` is the sole writer.
- `candidate_focus` cannot be promoted unless execution **proves** the
  candidate entity was actually used and not-found-checked.

---

## The five flags

All five are environment variables. They are read **per call** (not
cached at import) so tests and ops can flip them mid-process.

| Flag | Default | What it controls | Introduced in |
|---|---|---|---|
| `USE_GRAPH_FOCUS` | `false` | When `true`, LangGraph state is the authoritative focus source. `load_session_node` does NOT overwrite a populated `state.focus` from Dragonfly. `uc_executor_node` stops emitting `Command(update={"focus":...})` so the aggregator alone writes focus. | Phase 3 / 8 |
| `LEGACY_DRAGONFLY_FOCUS_READ` | `true` | When `true` and graph focus is empty, `load_session_node` hydrates focus once from Dragonfly and stamps `focus_hydrated_from_legacy=true`. | Phase 3 |
| `LEGACY_DRAGONFLY_FOCUS_WRITE` | `true` | When `true`, the aggregator still calls `apply_focus_writes` on the executed path (Dragonfly stays in sync with graph state). When `false`, Dragonfly receives no focus writes. | Phase 8 |
| `COMPARE_GRAPH_AND_DRAGONFLY_FOCUS` | `false` | When `true` and both stores have focus for a session, `load_session_node` emits a `focus.divergence` warning + span attrs. Never overwrites graph focus. | Phase 3 |
| `REQUIRE_THREAD_ID` | `false` | When `true` and no thread_id can be resolved (config / state / session_id), `load_session_node` raises `RuntimeError`. Use in stress/staging/prod; leave off locally. | Phase 3 |

**Truthy aliases** (case-insensitive): `1`, `true`, `yes`, `on`, `t`, `y`.
**Falsy aliases**: `0`, `false`, `no`, `off`, `f`, `n`.
**Unknown values** fall back to the documented default and log a
`focus_migration.flag_invalid` warning (operator visibility without a
startup crash).

---

## The four stages

### Stage 1 — Legacy (default)

```
USE_GRAPH_FOCUS=false
LEGACY_DRAGONFLY_FOCUS_READ=true
LEGACY_DRAGONFLY_FOCUS_WRITE=true
COMPARE_GRAPH_AND_DRAGONFLY_FOCUS=false
REQUIRE_THREAD_ID=false           # set true in stress/staging
```

**Behavior.** Dragonfly is the boss. `load_session_node` overwrites graph
focus from Dragonfly every turn. Executor still emits
`Command(update={"focus":...})` on pivots. Aggregator still calls
`apply_focus_writes`. `commit_focus_logic` runs but its focus output is
redundant with the executor's write.

**When to use.** The byte-identical baseline — what production runs out of
the box.

### Stage 2 — Dual-write + compare

```
USE_GRAPH_FOCUS=true
LEGACY_DRAGONFLY_FOCUS_READ=true
LEGACY_DRAGONFLY_FOCUS_WRITE=true
COMPARE_GRAPH_AND_DRAGONFLY_FOCUS=true
REQUIRE_THREAD_ID=true
```

**Behavior.** Graph state is the boss. `load_session_node` reads Dragonfly
only when graph focus is empty (legacy session hydration). The aggregator's
`commit_focus_logic` decides focus; the executor stops writing focus.
`apply_focus_writes` still runs so Dragonfly stays in sync — operators can
flip back to Stage 1 at any moment.

**Monitor.** Look for `focus.divergence` warnings. Any divergence
indicates a bug in `commit_focus_logic` or the legacy writer.

**Rollback to Stage 1.** Flip `USE_GRAPH_FOCUS=false` and
`COMPARE_GRAPH_AND_DRAGONFLY_FOCUS=false`. Dragonfly is already current.

### Stage 3 — Graph primary, Dragonfly read-only

```
USE_GRAPH_FOCUS=true
LEGACY_DRAGONFLY_FOCUS_READ=true
LEGACY_DRAGONFLY_FOCUS_WRITE=false
COMPARE_GRAPH_AND_DRAGONFLY_FOCUS=true
REQUIRE_THREAD_ID=true
```

**Behavior.** Same as Stage 2 except Dragonfly receives no focus writes.
Hydration is the only legacy path remaining. `legacy_dragonfly_focus_write_skipped`
debug event fires on every executed turn so you can confirm the disable
took effect.

**Rollback to Stage 2.** Flip `LEGACY_DRAGONFLY_FOCUS_WRITE=true`. Dragonfly
will be N turns stale at the moment of flip; the next executed turn brings
it current.

### Stage 4 — Graph only

```
USE_GRAPH_FOCUS=true
LEGACY_DRAGONFLY_FOCUS_READ=false
LEGACY_DRAGONFLY_FOCUS_WRITE=false
COMPARE_GRAPH_AND_DRAGONFLY_FOCUS=false
REQUIRE_THREAD_ID=true
```

**Behavior.** Dragonfly receives no focus traffic. LangGraph state is the
single source of truth. `focus_hydrated_from_legacy` is permanently `false`.

**Prerequisite.** **PostgresSaver MUST be the default checkpointer
(`LANGGRAPH_CHECKPOINTER=postgres`).** Without it, an `InMemorySaver`
restart wipes every active conversation. Phase 10 makes Postgres the
production default. Do NOT enable Stage 4 before Phase 10 ships and runs
green in staging for at least 7 days.

**Rollback.** Stage 4 → Stage 2 requires Dragonfly to catch up. Two
options:
1. Re-enable both Dragonfly read + write, accept N turns of stale
   sessions until next executed turn re-syncs.
2. Run a one-off backfill from the checkpointer to Dragonfly.

---

## Smoke commands per stage

Use `.venv/bin/python -m pytest` (the `.venv/bin/pytest` shim points to the
wrong venv on this machine — POC copy 4).

### After enabling Stage 2 in staging

```bash
USE_GRAPH_FOCUS=true \
LEGACY_DRAGONFLY_FOCUS_READ=true \
LEGACY_DRAGONFLY_FOCUS_WRITE=true \
COMPARE_GRAPH_AND_DRAGONFLY_FOCUS=true \
REQUIRE_THREAD_ID=true \
.venv/bin/python -m pytest tests/integration/test_multi_turn_focus.py \
  -k "not TestIntegrationMultiTurnFocus and not scenario_g" -q
```
Expect: 45+ tests pass.

### Multi-turn integration scenarios (requires live Supabase + LLM)

```bash
.venv/bin/python -m pytest tests/integration/test_multi_turn_focus.py::TestIntegrationMultiTurnFocus -q
```

### Stage 4 rehearsal (requires Postgres checkpointer)

```bash
LANGGRAPH_CHECKPOINTER=postgres \
USE_GRAPH_FOCUS=true \
LEGACY_DRAGONFLY_FOCUS_READ=false \
LEGACY_DRAGONFLY_FOCUS_WRITE=false \
REQUIRE_THREAD_ID=true \
.venv/bin/python -m pytest tests/integration/test_multi_turn_focus.py::test_scenario_g_postgres_restart -q
```

---

## What to monitor (logs / spans)

Search these structured-log keys / OTel span attributes:

| Signal | Meaning | Stage relevance |
|---|---|---|
| `focus.divergence` (warn) | Dragonfly focus disagrees with graph focus on the same session. | Stage 2 / 3 |
| `oneops.focus_hydrated_from_legacy=true` | Graph focus was empty; we hydrated from Dragonfly. Should approach zero over time. | Stage 2 / 3 |
| `candidate_focus_committed` (info) | Aggregator promoted a candidate. Healthy. | All stages |
| `candidate_focus_rejected` (info) | Aggregator rejected promotion. Look at `reason` field. Spikes in `not_found` indicate routing / data issues. | All stages |
| `legacy_dragonfly_focus_write_used` (debug) | Aggregator wrote to Dragonfly. Should be empty under Stage 3 / 4. | Stage 3 verifier |
| `legacy_dragonfly_focus_write_skipped` (debug) | Aggregator skipped Dragonfly write. Should be present under Stage 3 / 4. | Stage 3 verifier |
| `focus_migration.flag_invalid` (warn) | Operator typo in an env value. Fix the typo. | Any stage |
| `oneops.focus_reject_reason` (span attr) | One of: `no_candidate`, `execution_failed`, `execution_blocked`, `not_found`, `candidate_not_executed`, `no_matching_successful_step`. Use to triage stuck conversations. | All stages |

---

## Rollback playbook

| From | To | Action |
|---|---|---|
| Stage 4 | Stage 3 | Set `LEGACY_DRAGONFLY_FOCUS_READ=true`. Restart NOT required. Next turn that finds graph focus empty hydrates from Dragonfly. |
| Stage 3 | Stage 2 | Set `LEGACY_DRAGONFLY_FOCUS_WRITE=true`. Next executed turn re-syncs Dragonfly. |
| Stage 2 | Stage 1 | Set `USE_GRAPH_FOCUS=false` + `COMPARE_GRAPH_AND_DRAGONFLY_FOCUS=false`. Dragonfly is already current (still being dual-written). Zero loss. |
| Stage 1 | (nowhere) | Already at baseline. |

**Production rollback policy.** Each flip requires a runbook entry, a
correlated `focus_migration.stage_changed` audit event (operator
responsibility), and a 5-minute soak watching `candidate_focus_rejected`
rates. Do not auto-rollback on DB errors unless an explicit migration flag
authorizes it — silent fallback to Dragonfly defeats the audit trail.

---

## What NOT to enable in production yet

- **Stage 4.** Requires Phase 10's PostgresSaver default. Phase 10 has not
  shipped. Stage 4 without Postgres = `InMemorySaver` losing every
  conversation on every restart.
- **`REQUIRE_THREAD_ID=true` for local dev.** Local invocations from
  scripts often omit `thread_id`; flipping this on locally breaks
  iteration. Restrict to stress + staging + prod.
- **Promoting flags to `Settings` (pydantic) without first auditing all
  callers.** `Settings` is process-singleton via `lru_cache`; per-test
  monkeypatch of envs would stop working. Phase 9 deliberately keeps the
  migration config OUTSIDE `Settings` for this reason.

---

## Stage label introspection

```python
from oneops.config import get_focus_migration_config
cfg = get_focus_migration_config()
print(cfg.stage)
# stage_1_legacy | stage_2_dual_write | stage_3_graph_primary | stage_4_graph_only | custom
```

Reads env on every call — no caching, no import-time freeze. Safe to log on
every request if needed for fleet-wide stage tracking.

---

## Cross-references

- Phase 8 wiring: `src/oneops/graph/nodes.py` (`aggregator_node`, `_aggregator_body`, `uc_executor_node`)
- Phase 7 logic: `src/oneops/graph/commit_focus.py` (`commit_focus_logic`, `is_not_found_result`, `derive_execution_result`)
- Phase 6 resolver: `src/oneops/conversation/subject_resolver.py`, `src/oneops/conversation/followup_classifier.py`
- Phase 3 loader: `src/oneops/graph/nodes.py` (`load_session_node`, `_resolve_thread_id`, `_env_flag`)
- Tests: `tests/integration/test_multi_turn_focus.py`
