# AI-service — Refactor Roadmap

**Hard constraints (apply to every item):** no feature changes · no business-behavior changes · no API-contract changes · no database-contract changes. Every item is a **behavior-preserving** refactor, hardening, internal-infra, or test change. Where an item adds a table/substrate, it is **additive and flag-gated**, default-off, with byte-for-byte unchanged behavior when off.

Phases group by intent (A Critical → E Long-term). **Dependencies are explicit** — some Phase-A correctness work can only be *verified* once a Phase-C test seam exists, so a few items are sequenced across phases.

Legend — Complexity / Risk: **S/M/L/XL**. Impact: reliability / security / cost / velocity / observability.

---

## Phase A — Critical fixes (correctness; ship-blocking for enterprise fulfillment)

### A1. Enforce "all side effects after `interrupt()`" + idempotent action tools
- **Reason:** LangGraph re-runs a node from the top on resume (`reference §1.2`); any write before an `interrupt()` double-executes (gap #1).
- **Expected impact:** eliminates duplicate tickets/requests/provisioning on resume. reliability.
- **Complexity:** M (audit + reorder + idempotency keys). **Risk:** M (touches action tools — gate with A3 tests first).
- **Dependencies:** A3 (test surface) to verify; pairs with A2.
- **Affected files:** `executor/step_runner.py`, `executor/nodes.py`, `use_cases/uc08_fulfillment/tools.py` (`create_service_request`, `_apply_approval_gate`), action handlers.

### A2. Idempotency keys on NATS-dispatched fulfillment tasks
- **Reason:** NATS at-least-once + retries re-apply non-idempotent tasks (gap #2; `reference §5`).
- **Expected impact:** safe redelivery/retry; no duplicate provisioning. reliability.
- **Complexity:** M. **Risk:** M.
- **Dependencies:** A3 (verification); deterministic run-id derivation from the catalog request (no contract change — internal key).
- **Affected files:** `adapters/nats_resilience.py`, `use_cases/uc08_fulfillment/executor.py`, fulfillment task handlers, `use_cases/uc08_fulfillment/db.py`.

### A3. Repair the stale UC-8 failure/compensation tests (un-block verification)
- **Reason:** tests assert old task `tool_id`s → retry/compensation/partial-failure paths untested (gap #11). This is the verification layer for A1/A2/B1.
- **Expected impact:** the riskiest paths become CI-verified; A1/A2/B1 become provable. velocity + reliability.
- **Complexity:** S–M. **Risk:** S (test-only; no product code).
- **Dependencies:** none — **do this first in Phase A.**
- **Affected files:** `tests/unit/use_cases/uc08_fulfillment/{test_executor.py,test_executor_devils_play.py,test_catalog_unseen_probes.py}` (update to current task tool_ids; add resume-double-execution test).

### A4. Encrypt checkpoint serde + gate trace body capture
- **Reason:** PII in checkpoints/traces unencrypted/inline (gap #3; `reference §1.3/§3.1`).
- **Expected impact:** removes PII-at-rest exposure; unblocks regulated tenants. security/compliance.
- **Complexity:** M. **Risk:** M (serde change — verify resume of existing checkpoints; or apply forward-only).
- **Dependencies:** none.
- **Affected files:** `executor/graph.py` (checkpointer wiring → `EncryptedSerializer`), `observability/*` (one tri-state content-capture switch, default off in prod), Langfuse I/O capture sites.

### A5. Metric-attribute cardinality firewall
- **Reason:** tenant/session on Prometheus metrics → series explosion at tenant scale (gap #5; `reference §3.1`).
- **Expected impact:** monitoring survives multi-tenant scale. observability/reliability.
- **Complexity:** S–M (audit every metric site). **Risk:** S (telemetry internals; no behavior change).
- **Dependencies:** ideally do alongside C5 (central handler) but can land standalone.
- **Affected files:** `observability/metrics.py` and every `increment()/histogram()` call site (router, executor, stores); move identity to spans only; LLM-appropriate buckets.

---

## Phase B — Reliability improvements

### B1. Saga compensation correctness on parallel waves
- **Reason:** `gather` doesn't cancel siblings on first failure; compensate only on true cancel/fail, reverse order (gap #10; `reference §5`).
- **Expected impact:** no dangling partial provisioning on failure. reliability.
- **Complexity:** M–L. **Risk:** M.
- **Dependencies:** A3 (tests), A1/A2 (idempotency).
- **Affected files:** `use_cases/uc08_fulfillment/executor.py`, compensation handlers, `adapters/*`.

### B2. Retry/timeout taxonomy (per-task-type, as data)
- **Reason:** one timeout conflates attempt/budget/liveness; best-effort vs critical share a policy (gap #12; `reference §5/§2.3`).
- **Expected impact:** correct failure budgets; no retry storms on locked sections. reliability.
- **Complexity:** M. **Risk:** S–M.
- **Dependencies:** A2 (idempotency makes retries safe).
- **Affected files:** `executor/step_runner.py` (timeout), `adapters/nats_resilience.py`, fulfillment task config (data-driven policy).

### B3. Serialize concurrent shared-state mutations (lock-around-handler)
- **Reason:** async handlers mutating shared state across `await` corrupt it; two approvals racing one RITM (gap #18; `reference §5`).
- **Expected impact:** correct concurrent approvals/callbacks. reliability.
- **Complexity:** S–M. **Risk:** S.
- **Dependencies:** none.
- **Affected files:** approval decide path (`use_cases/uc08_fulfillment/approval.py`), parallel fulfillment callbacks.

### B4. Replayer-as-CI-gate + version the paused-state payload
- **Reason:** a state-shape change can break in-flight paused sessions on resume (gap #15; `reference §5/§2.3`).
- **Expected impact:** safe evolution of executor state. reliability/velocity.
- **Complexity:** M. **Risk:** S (CI-only).
- **Dependencies:** A3 patterns; checkpoint corpus capture.
- **Affected files:** new `tests/` replay harness over captured checkpoints; `executor/state.py` (schema version note).

### B5. Node-cache deterministic fetch/retrieval nodes
- **Reason:** resume/retry re-pays for deterministic reads (gap #22; `reference §1.7`).
- **Expected impact:** lower resume/retry cost+latency. performance/reliability.
- **Complexity:** S. **Risk:** S.
- **Dependencies:** none (complements Dragonfly caches).
- **Affected files:** `executor/graph.py` / node defs (`CachePolicy(ttl)` on deterministic nodes).

---

## Phase C — Architecture improvements

### C1. Decompose `api/app.py` + unify the two interrupt paths
- **Reason:** 2,570-line hub; interrupt logic duplicated across raised-`GraphInterrupt` and returned-`__interrupt__` (gap #8; `reference §4`).
- **Expected impact:** lower change-failure rate; one place for interrupt capture/persist/resume. velocity/reliability.
- **Complexity:** L. **Risk:** M (large move — do behind tests, no route/response changes).
- **Dependencies:** none, but high value before further executor work.
- **Affected files:** `api/app.py` → `api/lifespan.py` (startup wiring), `api/routers/{uc01,uc02,uc03,uc05,uc08,chat,fast}.py`, `api/interrupt.py` (single capture/resume helper used by both paths).

### C2. Land the DAL — single injected data boundary
- **Reason:** ~10 raw-SQL UC sites bypass tenant scope/redaction; the DAL is the platform's stated boundary (gap #9; `reference §4`).
- **Expected impact:** one choke point for tenant filter / redaction / query metrics / caching; removes cross-tenant-leak risk. security/architecture.
- **Complexity:** L–XL. **Risk:** M (behavior-neutral: same SQL behind a seam; migrate incrementally).
- **Dependencies:** confirm the DAL contract first (memory: deferred until contract confirmed).
- **Affected files:** new `db/dal.py` (port) + adapter; migrate `use_cases/_shared/ticket_store.py` and the ~10 raw sites in `use_cases/uc03,uc05,uc08`.

### C3. Single source of truth for entity-shaped param names
- **Reason:** mirrored `_ENTITY_FIELD_NAMES` / `_ENTITY_SHAPED_PARAMS` drift (gap #16).
- **Expected impact:** removes a silent drift class. velocity.
- **Complexity:** S. **Risk:** S.
- **Affected files:** `router/router.py`, `executor/step_runner.py` → import one shared constant (or derive from registry param metadata).

### C4. Split global settings into per-concern typed settings
- **Reason:** one settings blob; every concern reads every var (gap #17; `reference §4`).
- **Expected impact:** clearer config ownership; typed DSNs; `ENVIRONMENT` enum. velocity.
- **Complexity:** M. **Risk:** S (config plumbing; keep env var names = no contract change).
- **Affected files:** `config.py` → `config/{gateway,cache,nats,db,otel}.py`; lifespan wiring.

### C5. Central `TelemetryHandler` + `gen_ai.*` semconv + NATS `traceparent`
- **Reason:** hand-rolled emission drifts; LLM legs lack semconv op-names; NATS legs disconnected (gaps #19, #4; `reference §3.1`).
- **Expected impact:** consistent spans/metrics; Langfuse auto-classifies LLM/tool legs; end-to-end traces across NATS. observability.
- **Complexity:** M–L. **Risk:** S–M (observability internals).
- **Dependencies:** pairs with A5 (cardinality).
- **Affected files:** new `observability/telemetry_handler.py`; migrate emission in `router/*`, `executor/*`, stores; `adapters/nats_resilience.py` (inject/extract `traceparent`).

### C6. Formalize the LLM gateway as a narrow `Model`/`ModelProvider` port
- **Reason:** enables injection (FakeGateway, E1) and provider-tiering; encapsulate retry-advice on the model (`reference §2.3/§2.4`).
- **Expected impact:** the gateway becomes a clean seam; unlocks deterministic tests. velocity/testing.
- **Complexity:** M. **Risk:** S–M (interface extraction; keep call shape).
- **Dependencies:** precursor to E1.
- **Affected files:** `llm/gateway.py`, `llm/models.py`, `llm/transport.py`.

---

## Phase D — Performance improvements (behavior-preserving)

### D1. `tool_use_behavior`-style short-circuit for deterministic read tools
- **Reason:** skip the second LLM hop for read tools (`reference §2.1 B5`); supports router-collapse latency.
- **Impact:** lower cold-turn latency + cost. **Complexity:** M. **Risk:** M (must preserve output). **Dependencies:** C6. **Files:** `executor/step_runner.py`, tool records (flag a tool as terminal-on-result).

### D2. Classify UCs into chains vs planner; reserve the dynamic planner
- **Reason:** deterministic UCs pay for a planner they don't need (`reference §1.1`).
- **Impact:** fewer LLM calls on UC-1/UC-5-style chains. **Complexity:** M. **Risk:** M (routing config; verify parity). **Dependencies:** none. **Files:** registry agent cards / route plan assembly (`router/plan.py`).

### D3. Tag internal LLM calls `nostream` (when streaming ships)
- **Reason:** keep router/planner/judge tokens off the wire/UI (`reference §1.5`; gap #20).
- **Impact:** lower on-wire cost, ~1 s perceived latency. **Complexity:** S. **Risk:** S. **Dependencies:** streaming handler. **Files:** `llm/gateway.py` call sites, streaming handler.

### D4. Family-aware low reasoning effort for latency-sensitive UCs
- **Reason:** reasoning effort as a per-UC dial (`reference §2.3`).
- **Impact:** latency/cost on simple UCs. **Complexity:** S. **Risk:** S (verify quality parity). **Files:** model-settings resolution, registry agent cards.

---

## Phase E — Long-term modernization (additive, flag-gated; not behavior-changing when off)

### E1. `FakeGateway` deterministic LLM test seam
- **Reason:** make the funnel + executor unit-testable without network/cost (gap #7; `reference §2.4`).
- **Impact:** fast, deterministic CI; routing regressions caught pre-merge. velocity/testing. **Complexity:** M. **Risk:** S (test infra). **Dependencies:** C6 (port). **Files:** `tests/fakes/fake_gateway.py`, a pytest marker forbidding real-model calls.

### E2. Eval/score substrate (additive tables, gated)
- **Reason:** systematically score routing/grounded-ness; capture user feedback + human labels + judge scores (gap #13; `reference §3.2`).
- **Impact:** quality measurable over time. **Complexity:** L. **Risk:** M (additive schema; off by default). **Dependencies:** C5. **Files:** new `observability/scores.py` + `ai.scores` table (additive), judge calls behind a flag.

### E3. Versioned prompt registry
- **Reason:** attribute quality regressions to a prompt rev; A/B prompts (gap #14; `reference §3.2`).
- **Impact:** prompt operability. **Complexity:** L. **Risk:** M (additive; migrate prompts from code/JSON). **Dependencies:** C5 (record `promptVersion` on spans). **Files:** new prompt store + loader; record version on LLM spans.

### E4. Cost-as-data — versioned price table + token completeness
- **Reason:** move rates out of code; capture cache+reasoning tokens (gap #6; `reference §3.1/§3.2`).
- **Impact:** accurate per-tenant cost/showback. **Complexity:** M–L. **Risk:** M (additive price table; cost numbers change to *correct* — confirm with finance, not a behavior change to the product). **Dependencies:** C5. **Files:** `llm/gateway.py` (usage capture), new `ai.model_price` table + cached lookup.

### E5. Specialist-as-subgraph scaling path
- **Reason:** independently testable capability domains with own checkpoint namespace (`reference §1.6`).
- **Impact:** scales the executor beyond a flat graph. **Complexity:** XL. **Risk:** M. **Dependencies:** C1/C6/E1. **Files:** `executor/*` (subgraph composition; `compile(checkpointer=True)` for subgraph interrupts).

### E6. Cross-thread memory Store (user profile / "same issue last week")
- **Reason:** durable cross-session recall, namespaced per tenant/user, semantic search (`reference §1.4`).
- **Impact:** richer multi-turn/returning-user UX. **Complexity:** L. **Risk:** M (additive; gated). **Dependencies:** C2 (DAL). **Files:** new memory store over pgvector substrate; runtime-context injection.

---

## Sequencing summary (dependency-aware)

```
A3 ──► A1 ──► A2 ──► B1            (correctness + verification, the top workstream)
A4, A5            (security/observability, parallel to A1)
C6 ──► E1 ──► (enables fast verification of all of the above in CI)
C1 (decompose) — unblocks safer executor/interrupt work
C5 + A5 (telemetry) — observability at scale
C2 (DAL) — security boundary; precursor to E6
B2/B3/B4/B5 — steady-state reliability
D1–D4 — performance, after C6
E2/E3/E4 — quality/cost substrates, additive
E5/E6 — long-term scaling
```

**Recommended first sprint:** A3 → A1 → A2 → A4 → A5, with C6→E1 in parallel to make the correctness work CI-provable. This closes every Critical finding and the two highest-severity security/observability items without a single behavior or contract change.

> No code is changed by this document. Implementation begins only after these review docs are reviewed. Cross-references: findings (`docs/production_gap_analysis.md`), patterns (`docs/reference_repo_analysis.md`), grades (`docs/architecture_review.md`).
