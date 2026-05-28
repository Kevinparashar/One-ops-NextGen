# MIGRATION.md — POC-5-MW → Production AI Engine

**Status:** Plan. No build step starts until the phase before it meets its
exit criteria.
**Principle:** every phase leaves the system working. Every phase is
reversible. The current `ROUTING_MODE=legacy` path stays as a flag-protected
fallback until the new engine passes parity — it is removed only in the final
phase.

---

## Starting state (after Phase 1 cleanup)

- 3 use cases built: UC-1 summarization, UC-3 KB lookup, UC-99 conversational.
- LangGraph graph with `legacy` (production default, stress-passing) and
  `three_stage` routing modes.
- Adapters present: Dragonfly, Postgres (Supabase), NATS client, LLM gateway.
- OTel instrumentation present (`docs/observability/architecture_map.md`).
- Not present: API Gateway, separate Ingress/AuthZ/Router/Session services,
  declarative agent registry with conditions/hooks/determinism dial, protobuf
  codec, policy engine as data, idempotency keys, per-tenant cost accounting.

The migration is **additive then subtractive**: build the new engine behind
flags alongside the old one, prove parity, then remove the old one.

---

## Phase ordering and dependencies

```
P0 cleanup ✓ ─▶ P1 registry ─▶ P2 codec ─▶ P3 session ─▶ P4 authz ─┐
                                                                    ▼
P11 cutover ◀─ P10 load/chaos ◀─ P9 otel ◀─ P8 gateway ◀─ P7 tools ◀┤
                                                                    │
                              P6 executor ◀─ P5 router ◀────────────┘
```

Each phase is gated. `Pn+1` does not start until `Pn` exit criteria pass.

---

## P1 — Registry layer

**Build:** declarative agent registry, tool registry, schema registry. Each
agent record carries id, version, description, **activation_condition**,
tool_refs, policy_refs, abac_tags, **determinism_level**, **hooks**,
**depends_on**, **excludes**, owner (ARCHITECTURE.md §4). CRUD APIs; ABAC tags;
versioning. Migrate the existing 3 UCs into registry records.

**Exit criteria:** all 3 current UCs represented as registry records and
loadable; CRUD APIs contract-tested; synthetic load at 10K registry entries
within latency budget; the existing graph still runs unchanged off a
compatibility shim that reads the new registry.

**Rollback:** the registry is additive — the old hardcoded handler map
(`invoker.base._handlers`) stays live in parallel. Revert = stop reading the
registry; nothing else changes.

---

## P2 — Codec + schemas

**Build:** protobuf base envelope + per-message schemas (ADR-0001);
`proto/oneops/v1/`; generated clients; `protoc` step in CI. Schema-registry
records track versions + deprecation windows.

**Exit criteria:** envelope round-trips N and N−1 versions in a contract test;
CI fails on stale generated code; no message type on a service boundary still
uses ad-hoc JSON.

**Rollback:** wire format is negotiated per boundary; a boundary can fall back
to JSON via a content-type header while protobuf rolls out boundary by boundary.

---

## P3 — Session + conversation store

**Build:** Postgres append-only event log per session; Dragonfly hot-window
cache (tenant-scoped keys); replay logic; retention driven by
`updated_policy_v2.md`.

**Exit criteria:** integration test (testcontainers — real Postgres + real
Dragonfly) proves append, hot-read, cold-replay, and policy-driven retention;
two-tenant isolation test proves disjoint keyspaces.

**Rollback:** new session store runs behind a flag; the current
`adapters/session_store.py` stays live. Revert = flip the flag.

---

## P4 — AuthZ service

**Build:** RBAC role resolution + ABAC attribute evaluator as a service;
Dragonfly-cached decisions; sub-millisecond p99 on cache hits.

**Exit criteria:** property test — every ABAC deny is honored; p99 latency on
cache hit within budget; signed service-JWT verification proven on an internal
boundary.

**Rollback:** AuthZ is called behind a flag; flag-off path uses the current
tool-boundary `audience` checks. Revert = flip the flag. (Note: flag-off is a
*weaker* security posture — only acceptable in non-prod.)

---

## P5 — Router

**Build:** the four-stage funnel (ARCHITECTURE.md §3) — glossary
normalization, pgvector retrieval (ADR-0002), condition + ABAC filter, LLM
disambiguation via the gateway. Emits a plan DAG with dependencies and
exclusions resolved.

**Exit criteria:**
- Property test: any valid query yields a valid plan DAG or an explicit
  `no_confident_match`; never a silent wrong route.
- ABAC denies are honored at stage 3.
- **Parity gate:** on a frozen probe corpus, the new router's plan for the 3
  current UCs matches the `legacy`/`three_stage` outcome, or differs only in
  documented, reviewed ways.

**Rollback:** new router behind `ROUTING_MODE=engine`; `legacy` remains the
default. Revert = leave the default at `legacy`.

---

## P6 — LangGraph executor

**Build:** plan-DAG executor — parallel for independent nodes (Send fan-out),
sequential where `depends_on` declares; lifecycle hooks gating each node;
determinism-dial respected; checkpointing to the **dedicated** Postgres DB
(ADR-0004); Moveworks plan-evaluate-execute loop per node.

**Exit criteria:**
- Independent nodes observably run in parallel (overlapping OTel spans).
- Dependent nodes chain; cross-step injection works.
- Failure-mode tests: a node failure, a hook abort, and a mid-run crash each
  resume correctly from checkpoint (ARCHITECTURE.md Flow C).
- Idempotency: a re-delivered wave does not double-execute a side effect.

**Rollback:** new executor behind a flag; the current graph executor stays.
Revert = flip the flag. Checkpoints are in their own DB — reverting the
executor cannot harm app data.

---

## P7 — Tool runners

**Build:** each tool a sandboxed, timeout-enforced, idempotent FaaS handler;
referenced by id+version from the registry; activation-condition gated;
large outputs stored as named variables with a preview.

**Exit criteria:** a tool exceeding the timeout is killed and surfaces a typed
error; a re-invoked tool with the same idempotency key does not repeat its
side effect; oversized output never enters the next prompt.

**Rollback:** tools are versioned — a bad tool version is reverted by
re-pointing the registry reference at the prior version. No redeploy.

---

## P8 — LLM Gateway integration

**Build:** route **every** model call through the gateway — quota, fallback,
cost accounting **per tenant per model**, prompt redaction. Zero direct
provider-SDK calls anywhere else (enforced by a CI grep gate).

**Exit criteria:** CI fails if any module imports a provider SDK directly;
per-tenant/per-model cost counter increments on every call and is queryable;
prompt redaction verified against a PII fixture.

**Rollback:** the gateway is the single egress by construction — there is no
"old path" to revert to. Mitigation if the gateway itself fails: its own
circuit breaker + provider fallback (built into the gateway, not bypassed).

---

## P9 — OTel wiring

**Build:** trace id minted at the API Gateway, propagated through NATS
headers, every node, every tool, every gateway call. Metrics per
ARCHITECTURE.md §7. Structured, redacted, trace-correlated logs.

**Exit criteria:** one real exemplar trace traverses API Gateway → Ingress →
Router → Executor → tool → LLM Gateway and is viewable end-to-end with no
broken span links; PII redaction confirmed in the trace viewer.

**Rollback:** observability is non-functional-path — it can be disabled
(`OTEL_EXPORTER_OTLP_ENDPOINT` unset) without affecting business logic.

---

## P10 — Policy engine

**Build:** `updated_policy_v2.md` + policy data loaded into the embedded
data-driven evaluator (ADR-0003); agents and tools query it; canned responses
wired for compliance touchpoints.

**Exit criteria:** integration test proves a policy change takes effect via
cache invalidation with **no redeploy**; every policy decision is traced; a
compliance-touchpoint UC returns the canned response, not an LLM draft.

**Rollback:** policy data is versioned — revert to the prior policy version
(a data deploy). The evaluator code reverts via the flag if needed.

---

## P11 — Load + chaos, then cutover

**Build/run:** sustained multi-tenant load at the full registry scale; chaos
drills — kill a NATS node, kill a tool runner, kill Dragonfly — assert the
system degrades, does not crash. Fold in the retained soak/tier-1 harnesses.

**Exit criteria:**
- p99 latency at 10× current load measured (not guessed).
- Each chaos drill: degraded, traced, recovered — no crash, no data loss.
- New engine at parity with `legacy` on the full probe corpus.

**Cutover:** flip `ROUTING_MODE`/executor flags to the new engine **per
tenant**, canary first. After a soak window with no regression, flip the
default. **Only then** remove `legacy`, `three_stage`, `_legacy_planner_node`,
and the compatibility shims — in a dedicated removal PR with its own
CLEANUP-style report.

**Rollback:** until the removal PR, every prior phase's flag still works —
cutover is reversible by flipping flags back to `legacy`. After the removal
PR, rollback is a revert of that PR.

---

## Cross-cutting, every phase

- Idempotency keys minted at Ingress and threaded through (P3 onward).
- Per-tenant cost accounting live from P8.
- PII classified in schemas from P2; redaction enforced from P9.
- Every phase ships behind a per-tenant feature flag.
- Definition of Done (brief §"Definition of Done") applies to every phase:
  dead code gone, real tests, OTel spans verified, ABAC at every new
  boundary, two-tenant proof, failure modes tested, docs updated in the
  same PR, no untracked TODO.

---

## Reversibility summary

| Phase | Revert mechanism |
|---|---|
| P1 registry | Old `_handlers` map runs in parallel; stop reading registry |
| P2 codec | Per-boundary content-type fallback to JSON |
| P3 session | Flag — old `session_store.py` stays live |
| P4 authz | Flag — old `audience` checks stay (non-prod only) |
| P5 router | `ROUTING_MODE` default stays `legacy` |
| P6 executor | Flag — old graph executor stays; checkpoints isolated |
| P7 tools | Registry reference re-points to prior tool version |
| P8 gateway | Gateway's own breaker + provider fallback (no bypass) |
| P9 otel | Disable exporter — no business-logic impact |
| P10 policy | Revert to prior policy data version |
| P11 cutover | Flip flags to `legacy`; post-removal, revert the PR |
