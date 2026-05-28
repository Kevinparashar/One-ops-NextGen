# ADR-0002 — Vector store for routing retrieval: pgvector

**Status:** Accepted · 2026-05-20
**Decision owners:** AI Platform Architect

## Context

Routing stage 2 (semantic retrieval) embeds every agent's capability
description and, per query, retrieves the top-K candidates by vector
similarity. The store must:

- Hold the agent-capability index — **~1000 vectors** at full catalog scale.
- Support per-tenant filtering of the candidate set.
- Be operable for 5 years without a dedicated vector-DB team.

The repo already runs Postgres (Supabase) with the `pgvector` extension; KB
and UC-capability embeddings are already seeded there.

## Options

| Option | Scale headroom | Ops burden | Tenant filter | Already in stack |
|---|---|---|---|---|
| **pgvector** | Millions of vectors (HNSW) — vastly above 1000 | None new — it's Postgres | SQL `WHERE tenant_id` alongside kNN | Yes |
| Qdrant | Tens of millions+ | A new service to deploy, monitor, back up, secure | Native payload filters | No |
| Pinecone / hosted | High | Vendor lock-in, per-vector cost, data-residency questions | Native | No |

## Decision

**pgvector**, in the existing Postgres. The agent-capability index uses an
HNSW index; retrieval is a single SQL statement combining kNN with a
tenant-allowed-agent filter.

## Why pgvector over Qdrant

The routing index is **tiny** — 1000 vectors. Qdrant's advantage is scale into
the tens of millions; that advantage is irrelevant here and is bought with a
whole new system to run, monitor, secure, and back up. The 5-year-horizon
tiebreaker favors **one fewer moving part**. pgvector with HNSW handles
millions of rows comfortably — three orders of magnitude of headroom.

## Consequences

- Routing retrieval joins naturally with relational tenant/registry data — no
  cross-store consistency problem.
- Backup/DR for the vector index is just Postgres backup/DR.
- Embedding writes happen on agent registration; the index is small and
  rebuildable from the registry at any time.

## Exit plan / revisit trigger

This ADR covers the **routing index only**. A *separate* concern — per-tenant
document/tool corpora that could grow to 50M+ vectors — may justify a
dedicated vector DB later. That is a different index with its own ADR; it does
not change this decision. Revisit when any single tenant's document index
exceeds ~5M vectors or pgvector p99 retrieval latency exceeds the routing
budget.
