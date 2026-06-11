# AI-service — Production Gap Analysis

Assessed against LangGraph best practices, enterprise FastAPI patterns, agent-platform patterns, workflow-orchestration patterns, distributed-systems principles, multi-tenant SaaS principles, and observability/reliability engineering. Each finding cites the reference pattern (`docs/reference_repo_analysis.md`) that informs it.

**Severity scale.** Critical = silent data corruption / cross-tenant leak / unrecoverable state. High = production incident or compliance exposure under normal load. Medium = operability/quality/maintainability drag. Low = polish.

**Summary.** AI-service is unusually principled for its stage (agents-as-data, semantic routing, fail-closed approvals, single LLM egress, real observability). The gaps cluster in **durable-execution correctness under resume**, **distributed-systems hygiene (idempotency, tracing across NATS)**, **multi-tenant telemetry discipline**, and **structural maintainability** — not in product behavior.

| # | Finding | Cat | Sev |
|---|---|---|---|
| 1 | Side effects before `interrupt()` / non-idempotent action tools | Reliability/Correctness | **Critical** |
| 2 | NATS at-least-once without idempotency keys on fulfillment tasks | Reliability | **Critical** |
| 3 | PII in checkpoints and traces (no encryption/externalization) | Security/Compliance | High |
| 4 | NATS hops likely don't propagate `traceparent` | Observability | High |
| 5 | Metric cardinality — tenant/session on metrics risks Prometheus blowup | Observability/Reliability | High |
| 6 | Cost accounting: inline rates and/or missing cache+reasoning tokens | Cost/Correctness | High |
| 7 | No deterministic LLM test seam (`FakeGateway`) | Testing | High |
| 8 | Monolithic `app.py` + duplicated interrupt paths | Maintainability | High |
| 9 | DAL deferred — raw SQL in UC handlers bypasses the data boundary | Architecture/Security | High |
| 10 | Saga compensation correctness on parallel waves | Reliability | High |
| 11 | UC-8 retry/compensation paths untested (stale tests) | Testing/Reliability | High |
| 12 | Single timeout vs a retry/timeout taxonomy | Reliability | Medium |
| 13 | No eval/score substrate | Quality | Medium |
| 14 | No prompt versioning/registry | Quality/Operability | Medium |
| 15 | No replayer-as-CI-gate for checkpoint-shape changes | Reliability | Medium |
| 16 | Mirrored constant sets (entity-shaped params) | Maintainability | Medium |
| 17 | Single global settings blob | Maintainability | Medium |
| 18 | Concurrent-approval / parallel-callback locking | Reliability | Medium |
| 19 | Telemetry emission scattered (no central handler) | Observability/Maint. | Medium |
| 20 | Streaming internal-call `nostream` tagging (future) | Performance/UX | Low |
| 21 | `/docs` (OpenAPI) exposure not env-gated | Security | Low |
| 22 | Deterministic fetch nodes not node-cached | Performance | Low |

---

### Finding 1 — Side effects before `interrupt()` / non-idempotent action tools — **Critical**
- **Category:** Reliability / correctness (durable execution).
- **Affected modules:** `executor/step_runner.py`, `executor/nodes.py`, `use_cases/uc08_fulfillment/tools.py` (`create_service_request`, `_apply_approval_gate`), any action tool that interrupts.
- **Risk:** LangGraph re-runs a node from the top on `Command(resume=…)` (`reference §1.2`). Any DB write / ticket creation / external call placed *before* the `interrupt()` executes **twice** on resume. The slot-filling and catalog flows interrupt; the approval park happens around create. If a write precedes an interrupt in any path, resume double-writes.
- **Business impact:** duplicate tickets/requests, duplicate provisioning, duplicate approvals — silent, hard to detect, erodes trust in the AI.
- **Technical impact:** corrupted `itsm.*` state; idempotency violations; non-deterministic replay.
- **Recommended solution:** (a) audit every node that calls `interrupt()` and move all side effects *after* it; (b) wrap each side-effecting tool as an isolated unit and make it idempotent (deterministic key derived from request id); (c) add a regression test per interrupt flow: run→interrupt→resume, assert each write happened exactly once (the "resume double-execution" test, `reference §1.7/§2.4/§5`). Use `sync` durability before confirmed writes (`reference §1.2`).

### Finding 2 — NATS at-least-once without idempotency keys — **Critical**
- **Category:** Reliability (distributed systems).
- **Affected modules:** `adapters/nats_resilience.py`, `uc08_fulfillment/executor.py` (task dispatch), the fulfillment task handlers.
- **Risk:** NATS delivers at-least-once; a redelivered fulfillment task without an idempotency guard re-applies side effects (`reference §5 idempotency`). Combined with retries, the same provisioning step can run multiple times.
- **Business impact:** duplicate provisioning (accounts, licenses, access grants), cost and security exposure.
- **Technical impact:** non-idempotent task execution; compensation accounting drift.
- **Recommended solution:** each fulfillment task short-circuits if already applied (guard on a stable `(tenant, ritm, task)` key); derive a deterministic fulfillment-run id from the catalog request for create-or-get semantics (`reference §5`). Persist task outcome before acking.

### Finding 3 — PII in checkpoints and traces — **High**
- **Category:** Security / compliance.
- **Affected modules:** LangGraph checkpointer wiring (`executor/graph.py`), `observability/*`, Langfuse I/O capture.
- **Risk:** Checkpoints serialize full graph state (ticket descriptions, names, emails); traces can carry prompt/completion bodies. Neither is encrypted/externalized by default (`reference §1.3 EncryptedSerializer`, `§3.1 CompletionHook`).
- **Business impact:** PII at rest in checkpoint rows and in Tempo/Langfuse; GDPR/enterprise-DPA exposure; blocks regulated tenants.
- **Technical impact:** sensitive data in operational stores with broad access.
- **Recommended solution:** (a) wrap the checkpointer serde with an `EncryptedSerializer` (AES key from env); (b) gate trace body capture behind one handler-level tri-state switch (default off in prod) and externalize bodies to tenant-scoped object storage with only `…_ref` URIs on spans; hash-dedupe the (identical) router/system prompts (`reference §3.1`).

### Finding 4 — NATS likely doesn't propagate `traceparent` — **High**
- **Category:** Observability.
- **Affected modules:** `adapters/nats_resilience.py`, fulfillment publish/subscribe, `observability/*`.
- **Risk:** Without injecting W3C context into NATS headers on publish and extracting on consume, the dispatched fulfillment legs appear as **disconnected traces** (`reference §3.1 NATS/Kafka pattern`). End-to-end "catalog request → tasks" traces break exactly where debugging matters.
- **Business impact:** slow incident triage of fulfillment failures; no single-trace view of a request's lifecycle.
- **Technical impact:** fragmented Tempo traces; broken parent/child across the async boundary.
- **Recommended solution:** inject `traceparent` into NATS message headers on publish; extract and start the consumer span with that context before running the task.

### Finding 5 — Metric cardinality (tenant/session on metrics) — **High**
- **Category:** Observability / reliability.
- **Affected modules:** `observability/metrics.py` and every `increment()/histogram()` call site (router, executor, stores).
- **Risk:** If `tenant_id`/`session_id`/`request_id` land on Prometheus metric labels, series count explodes with tenants/sessions → OOM/scrape failure (`reference §3.1 cardinality firewall`).
- **Business impact:** monitoring outage as the platform scales tenants — the worst time to lose observability.
- **Technical impact:** unbounded time-series; Prometheus instability.
- **Recommended solution:** enforce a two-bucket split — high-cardinality identity (`tenant`, `session`, `ticket`) on **spans only**; metrics labeled only by bounded enums (operation, provider, model, status, error.type). Audit every metric call site. Use LLM-appropriate histogram buckets (sub-second–80 s).

### Finding 6 — Cost accounting (inline rates / missing token types) — **High**
- **Category:** Cost / correctness.
- **Affected modules:** `llm/gateway.py`, per-tenant cost emission.
- **Risk:** If per-model rates are hardcoded in code, price changes/aliases drift and violate the never-hardcode rule. If usage omits **cache tokens** and **reasoning/thinking tokens**, thinking-model cost is under-counted (`reference §3.1 C1–C3`).
- **Business impact:** inaccurate per-tenant billing/showback; under-recovery on reasoning models.
- **Technical impact:** cost computed from incomplete usage; brittle rate table.
- **Recommended solution:** spans carry **tokens, not dollars**; compute cost downstream from a **versioned price table** (JSON→table→cached lookup, regex model match + `start_date` versioning). Capture cache + reasoning tokens; `output = output + thinking` at emit (`reference §3.1/§3.2`).

### Finding 7 — No deterministic LLM test seam — **High**
- **Category:** Testing.
- **Affected modules:** `llm/gateway.py` (the port), test suite.
- **Risk:** Agents call the gateway; without a `FakeGateway` that scripts per-turn outputs, the 4-stage funnel and the executor can't be unit-tested without network/cost (`reference §2.4 FakeModel`). Routing/executor correctness is verified mostly live.
- **Business impact:** slow, expensive, flaky CI; routing regressions (like the ones surfaced this session) caught late.
- **Technical impact:** low determinism; the gateway port isn't formalized for injection.
- **Recommended solution:** formalize the LiteLLM gateway as a narrow `Model`/`ModelProvider`-style port (sync+stream+retry-advice), inject a `FakeGateway` in tests that scripts outputs and captures `last_turn_args`; add a pytest marker that fails any accidental real-model call (`reference §2.3/§2.4`).

### Finding 8 — Monolithic `app.py` + duplicated interrupt paths — **High**
- **Category:** Maintainability.
- **Affected modules:** `api/app.py` (2,570 LOC).
- **Risk:** Startup wiring, route mounting, static frontend, executor boot, fast-path, and the interrupt protocol (two code paths) all live in one file. Interrupt-adjacent changes must touch both paths or diverge (observed this session) (`reference §4 thin-main`).
- **Business impact:** higher change-failure rate; slower onboarding; concentrated regression risk.
- **Technical impact:** hidden coupling hub; hard to test in isolation.
- **Recommended solution:** move startup wiring into a `lifespan` module; extract per-UC routers; unify the two interrupt paths behind one helper that both the raised-`GraphInterrupt` and returned-`__interrupt__` branches call (single place for capture + persist + resume).

### Finding 9 — DAL deferred; raw SQL bypasses the data boundary — **High**
- **Category:** Architecture / security.
- **Affected modules:** ~10 raw asyncpg sites across `use_cases/uc03/uc05/uc08`, `use_cases/_shared/ticket_store.py`.
- **Risk:** No single data-access boundary; tenant scoping and policy redaction depend on each call site remembering `WHERE tenant_id=$1`. A missed predicate is a cross-tenant leak (`reference §4 get_db / SQL-first`; the platform's own DAL goal).
- **Business impact:** cross-tenant data exposure risk; inconsistent query patterns; rework when the DAL lands (building on raw SQL = rework).
- **Technical impact:** fragmented data access; no choke point for tenant filter / redaction / caching / metrics.
- **Recommended solution:** route all reads/writes through one injected data boundary (a `get_db()`-style dependency / port + adapter) that enforces tenant scope and emits query metrics centrally; migrate the 10 raw sites behind it. (Non-behavioral: same SQL, single seam.)

### Finding 10 — Saga compensation correctness on parallel waves — **High**
- **Category:** Reliability.
- **Affected modules:** `uc08_fulfillment/executor.py` (task DAG, compensation).
- **Risk:** `asyncio.gather` does **not** cancel sibling branches on first failure (`reference §5`); compensation must trigger on *true* cancel/fail (not on a retryable transient) and run in reverse order. If a parallel wave half-completes and one branch fails, started siblings may not be cancelled/compensated.
- **Business impact:** partial provisioning left dangling on failure; inconsistent request state.
- **Technical impact:** incomplete saga; orphaned side effects.
- **Recommended solution:** on first failure in a parallel wave, explicitly cancel/compensate already-started siblings; distinguish retryable-error (retry) from terminal cancel/fail (compensate); add a completion barrier before terminal transition; verify with tests asserting compensation order and call counts (`reference §5`).

### Finding 11 — UC-8 retry/compensation paths untested — **High**
- **Category:** Testing / reliability.
- **Affected modules:** `tests/unit/use_cases/uc08_fulfillment/{test_executor,test_executor_devils_play,test_catalog_unseen_probes}.py`.
- **Risk:** The Playbook-3 rewrite renamed task `tool_id`s; failure-injection tests assert old names (`provision_email_mailbox`) and don't inject failures → **retry/compensation/partial-failure paths are effectively untested** (8 red tests; happy-path passes). This is the test surface for Findings 1/2/10.
- **Business impact:** the riskiest paths (failure handling, compensation) ship unverified.
- **Technical impact:** coverage debt; green-vs-red signal is misleading.
- **Recommended solution:** update the tests to the current task tool_ids and re-verify retry/compensation/partial-failure behavior; treat as the verification layer for Findings 1/2/10. (Behavior-neutral test fixes.)

### Finding 12 — Single timeout vs retry/timeout taxonomy — **Medium**
- **Category:** Reliability.
- **Affected modules:** `executor/step_runner.py` (per-tool timeout), NATS dispatch, fulfillment tasks.
- **Risk:** One timeout conflates single-attempt vs total-budget vs liveness (`reference §5 timeout taxonomy`); best-effort vs critical steps get the same retry policy.
- **Recommended solution:** per-task-type retry policy (best-effort `max_attempts=1`, critical bounded-exponential) and distinct `start_to_close` / `schedule_to_close` / liveness timeouts. Express retry as data, with replay-safety (`reference §2.3`).

### Finding 13 — No eval/score substrate — **Medium**
- **Category:** Quality.
- **Affected modules:** observability, routing/KB quality.
- **Risk:** No structured way to record "routing correct? / KB answer grounded?" — quality is judged ad hoc (`reference §3.2 scores`).
- **Recommended solution:** append-only polymorphic score records (source ∈ {API user-feedback, EVAL judge, ANNOTATION human}) keyed to trace/observation; LLM-as-judge for grounded-ness/routing; attach to the routing trace.

### Finding 14 — No prompt versioning/registry — **Medium**
- **Category:** Quality / operability.
- **Affected modules:** router prompts, UC prompts (in code/JSON).
- **Risk:** Heavy prompt engineering with no version attribution; can't A/B or attribute regressions to a rev (`reference §3.2 prompt store`).
- **Recommended solution:** a versioned, labeled prompt store; record `promptVersion` on each LLM span; config-as-code with commit messages.

### Finding 15 — No replayer-as-CI-gate — **Medium**
- **Category:** Reliability (durable state).
- **Affected modules:** executor state schema, CI.
- **Risk:** A change to `ExecutorState` shape can break in-flight checkpointed/paused sessions on resume (`reference §5 replayer`).
- **Recommended solution:** capture a corpus of real paused checkpoints; replay against new graph code in CI to catch resume-incompatible changes; version the serialized paused/approval payload with migration notes (`reference §2.3 K3`).

### Finding 16 — Mirrored constant sets — **Medium**
- **Category:** Maintainability.
- **Affected modules:** `router.router._ENTITY_FIELD_NAMES`, `executor.step_runner._ENTITY_SHAPED_PARAMS`.
- **Risk:** Two hand-synced sets ("must match" by comment) drift.
- **Recommended solution:** one source of truth imported by both (or derived from the registry param metadata).

### Finding 17 — Single global settings blob — **Medium**
- **Category:** Maintainability.
- **Affected modules:** `config.py`, lifespan wiring.
- **Risk:** Many env host/port vars in one settings object; every concern reads every var (`reference §4 split BaseSettings`).
- **Recommended solution:** per-concern settings (gateway/cache/nats/db) with typed DSNs and an `ENVIRONMENT` enum.

### Finding 18 — Concurrent-approval / parallel-callback locking — **Medium**
- **Category:** Reliability.
- **Affected modules:** approval decide path, parallel fulfillment callbacks mutating shared state.
- **Risk:** Async handlers mutating shared state across an `await` without a lock corrupt it (`reference §5 lock-around-handler`); two approvals racing the same RITM.
- **Recommended solution:** serialize shared-state mutations (lock or single-writer); the `any_one` decide is transactional already — extend the discipline to parallel callbacks.

### Finding 19 — Telemetry emission scattered — **Medium**
- **Category:** Observability / maintainability.
- **Affected modules:** router, executor, stores (hand-rolled spans/metrics).
- **Risk:** Attribute drift across UCs; no single place to enforce semconv naming / cardinality / content-capture switch (`reference §3.1 TelemetryHandler`).
- **Recommended solution:** centralize span/metric/event emission in one handler; call sites use `handler.start_*()`; adopt `gen_ai.*` semconv op-names on LLM/tool legs so Langfuse auto-classifies.

### Finding 20 — `nostream` tagging (future streaming) — **Low**
- Tag router/planner/judge LLM calls `nostream`; stream only the final generation (`reference §1.5`). Relevant when the streaming handler ships.

### Finding 21 — `/docs` not env-gated — **Low**
- Gate `openapi_url`/docs to dev/staging before external deployment (`reference §4 security`).

### Finding 22 — Deterministic fetch nodes not node-cached — **Low**
- Apply `CachePolicy(ttl)` to deterministic fetch/retrieval nodes so resume/retry doesn't re-pay (`reference §1.7`); complements the existing Dragonfly caches.

---

## Disposition guidance
- **Ship-blocking for regulated/at-scale tenants:** Findings 1, 2, 3, 5.
- **Operability before scale:** Findings 4, 6, 8, 9, 11.
- **Quality/velocity compounders:** Findings 7, 13, 14, 19.
- The rest are steady-state hardening.

> Severities reflect a production system serving enterprise customers. Findings 1, 2, 10, 11 are interdependent (correctness + its missing test surface) and should be addressed as one workstream. See `docs/refactor_roadmap.md` for sequencing.
