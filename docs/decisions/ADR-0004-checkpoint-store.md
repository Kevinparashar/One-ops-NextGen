# ADR-0004 — LangGraph checkpoint store: dedicated Postgres database

**Status:** Accepted · 2026-05-20
**Decision owners:** AI Platform Architect

## Context

The LangGraph executor must be checkpointable and resumable — no in-memory-only
state on the request path. The checkpointer persists graph state per
`thread_id` so a crashed run resumes from the last good wave (docs/architecture/ARCHITECTURE.md
Flow C).

**Recorded incident — must not repeat.** A prior session (POST-MORTEM:
Supabase data loss, 2026-05-16) lost application tables when a tooling
component ran schema operations against the **shared** Supabase database that
also held application data. The lesson: anything that runs `setup()`-style
schema DDL must not point at the shared app database.

`builder.py` currently can wire `AsyncPostgresSaver` against the shared
Supabase DSN — and calls `saver.setup()`, which issues DDL. That is the exact
shape of the incident.

## Options

| Option | Durability | Resume | Blast radius of saver DDL | Restart-safe |
|---|---|---|---|---|
| **Dedicated Postgres DB for checkpoints** | Durable | Yes | Isolated — own database, own credentials | Yes |
| Shared Supabase app DB | Durable | Yes | **Shared with tenant/app tables — repeats the incident** | Yes |
| `InMemorySaver` | None | No — restart wipes every active run | None | **No** |

## Decision

A **dedicated Postgres database** for the LangGraph checkpointer, separate
from the tenant/application database — separate database, separate
credentials, separate connection pool. `AsyncPostgresSaver.setup()` runs only
against this isolated database.

`InMemorySaver` remains permitted **only** for unit tests, never for any
environment that serves real traffic.

## Why a dedicated database

1. **It structurally prevents the recorded incident.** Checkpointer DDL can
   never touch an application table because the application tables are not in
   that database.
2. Checkpoint write patterns (frequent, per-wave, large state blobs) have a
   different load profile than transactional app queries — isolating them
   protects app-DB performance.
3. Independent backup/retention: checkpoints are short-lived operational
   state; app data is long-lived. Different retention policies, cleanly.

## Consequences

- A second Postgres database (or instance) is provisioned and operated.
- `LANGGRAPH_CHECKPOINTER=postgres` becomes the production default with this
  DSN; the connection pool is process-lifetime (`min_size=2, max_size=20` —
  the Tier-1 concurrency fix from 2026-05-16 is preserved).
- A retention job prunes checkpoints past the resumability window (policy-driven).
- CI runs an isolated test database; no test ever points the saver at a
  shared DB.

## Exit plan

The checkpointer is a LangGraph interface. If LangGraph is replaced (the
AgentScript "agents are data" principle keeps that possible), the checkpoint
store is re-implemented against the new runtime's interface; the dedicated
database remains the right isolation regardless of runtime.
