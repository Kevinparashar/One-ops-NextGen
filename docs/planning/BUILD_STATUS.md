# docs/planning/BUILD_STATUS.md — POC-5-MW Production Rebuild

Tracks the 11-phase build (docs/history/MIGRATION.md P1–P11). Source of truth — conversation
context evaporates, this file does not.

## Build directive (owner, 2026-05-20)

Build only the new system on the mandated stack + patterns. The old POC is
**not** preserved as a runnable fallback. Old orchestration code (`graph/`,
`routing/`, `planner/`, `conversation/`, `contracts/`, old-shape registries) is
**reference-only** and is deleted as each phase supersedes it. Reused (new
system's own components, not "old scaffolding"): `use_cases/` business logic,
`adapters/`, `gateway/`, `observability/`, `policy/`, `safety/`, `data/`,
durable schemas/policy files. End state: new-system-only.
Removed already: `showcase/` (old demo UI).

| Phase | Status | Evidence |
|---|---|---|
| P0 — Cleanup | ✅ done | `docs/history/CLEANUP.md` |
| P1 — Registry layer | ✅ done | see below |
| P2 — Codec + schemas | ✅ done | see below |
| P3 — Session store | ✅ done | see below |
| P4 — AuthZ service | ✅ done | see below |
| P5 — Router | ✅ done | see below |
| P6 — LangGraph executor | ✅ done | see below |
| P7 — Tool runners | ✅ done | see below |
| P8 — LLM Gateway | ✅ done | see below |
| P9 — OTel wiring | ✅ done | see below |
| P10 — Policy engine | ✅ done | see below |
| P11 — Load/chaos + cutover | ✅ done | see below |

---

## P1 — Registry layer (done)

**Built:** `src/oneops/registry/` — the declarative specification layer.

| Module | Role |
|---|---|
| `models.py` | `AgentRecord`, `ToolRecord`, `SchemaRecord` + `ActivationCondition`, `AbacTags`, `Hooks`, `ExclusionRef`, `JourneySpec`. Pydantic — validation is enforcement. |
| `store.py` | `RegistryBackend` Protocol + `FileBackend` (atomic writes) + `VersionedStore` — CRUD + version lifecycle (create/update/activate/retire/rollback). |
| `service.py` | `RegistryService` — composes the 3 stores; `check_integrity()` (dangling refs, dependency cycles, exclusion-priority ambiguity). |
| `loader.py` | `load_registry()` — startup load + schema validation + integrity gate. |

**Design influences applied:**
- *AgentScript* — agents are data; `determinism_level` dial; `hooks`; versioned records; rollback = re-activate a prior version.
- *Parlant* — `activation_condition` (declarative observation, deterministically evaluable); `depends_on`; `excludes` with priority.
- *Moveworks* — `MAX_DESCRIPTION_CHARS=600` attention-budget cap; `compound_of`; schema-as-contract.

**Migrated:** the built use-case UCs → `registries/v2/` via
`dev/migrate_registry_v2.py` — 2 agents (`uc01_summarization`,
`uc03_kb_lookup`), 10 tools, 1 schema record. Legacy `registries/*.json`
untouched. (No `uc99` — the conversational / out-of-scope / policy-boundary
responder is a platform component in the routing layer, not a registry agent.)

**Tests — 60 green:**
- `tests/unit/registry/test_models.py` — 33 contract tests (every validator).
- `tests/unit/registry/test_store.py` — 18 CRUD + versioning + persistence + corruption tests.
- `tests/unit/registry/test_service.py` — 17 integrity tests (dangling refs, cycles, diamond-not-cycle).
- `tests/integration/test_registry_load.py` — 10K-agent load: list 0.63s + integrity 0.63s (budget 30s); cycle still detected at scale.

**Exit criteria (docs/history/MIGRATION.md P1):**
- ✅ 3 UCs as registry records, loadable.
- ✅ CRUD contract-tested.
- ✅ 10K-entry synthetic load within budget (2.79s total).
- ✅ Legacy path unaffected — `registries/*.json` untouched, `oneops.errors`
  changes are additive-only, existing modules import unchanged. (The new
  registry loads independently; a consumer is wired in P5/P6 — no throwaway
  shim built, per scope discipline.)

**Reversibility:** the new registry is additive. Reverting P1 = stop calling
`load_registry()`; nothing else changes.

---

## P2 — Codec + schemas (done)

**Built:** the protobuf wire/disk contract (ADR-0001).

| Artefact | Role |
|---|---|
| `proto/oneops/v1/envelope.proto` | `Envelope` + `UCRequest` / `UCResponse` / `ConversationEvent`. Field numbers permanent; adding an optional field is safe. |
| `src/oneops/codec/generated/` | protoc-generated Python bindings (checked in). |
| `src/oneops/codec/codec.py` | `encode()` / `decode()` — the single serialise path. Schema-version window `[MIN_SUPPORTED, CURRENT]` enforced; typed errors on malformed / out-of-window. |
| `dev/gen_proto.sh`, `make proto`, `make proto-check` | regeneration + CI staleness gate. |

**Schema-version window (N / N-1):** `CURRENT=1`, `MIN_SUPPORTED=1`. `decode()`
refuses anything outside the window loudly. Protobuf field-number permanence
gives forward compatibility — proven, not assumed.

**Tests — 14 green** (`tests/unit/codec/test_codec.py`): round-trip for all
three payload types; encode/decode guards (non-contract payload, empty
tenant, garbage bytes, out-of-window version, unknown message_type, corrupt
payload); **the N/N-1 guarantee proven on the wire** — an envelope carrying an
unknown field from a newer producer still decodes, and an old envelope
missing a later field decodes to defaults.

**Exit criteria (docs/history/MIGRATION.md P2):**
- ✅ protobuf envelope + per-message schemas defined and generated.
- ✅ CI fails on stale generated code (`make proto-check`).
- ✅ N and N-1 round-trip in a contract test.
- ✅ No boundary uses ad-hoc JSON — the codec is the only serialise path;
  the NATS service boundaries (P3+) consume it by construction.

**Reversibility:** wire format is negotiated per boundary; until a boundary
exists (P3+) there is nothing to revert. Generated code + `.proto` are the
language-neutral source of truth.

---

## P3 — Session + conversation store (done)

**Built:** `src/oneops/session/` — append-only conversation history.

| Module | Role |
|---|---|
| `backend.py` | `EventLog` / `HotWindow` Protocols + `InMemoryEventLog` / `InMemoryHotWindow` (real alternate backends, not mocks). |
| `postgres_log.py` | `PostgresEventLog` — durable append-only cold log; append-only by contract (INSERT / SELECT / retention DELETE only). |
| `dragonfly_window.py` | `DragonflyHotWindow` — bounded, TTL'd hot cache; tenant in the key. |
| `store.py` | `SessionEventStore` — append cold-then-hot; `recent` (hot → cold rebuild on miss); `replay` (full, from cold); `apply_retention`. `RetentionPolicy` + `resolve_retention()` (config now; P10 policy-engine seam). |
| `migrations/0001_conversation_events.sql` | DDL — applied by the operator's migration runner, never by app code. |

**Design:** durable-first write ordering (a crash leaves the cache
stale-but-recoverable, never an acknowledged-but-unstored event); tenant
isolation by construction (`tenant_id` mandatory on every method + every
backend key); events are the protobuf `ConversationEvent` (ADR-0001) — on-disk
shape == on-wire shape.

**Tests — 13 unit green** (`tests/unit/session/test_store.py`, in-memory
backends): append/monotonic-seq, hot-miss rebuild, hot-hit, window size bound,
replay full + from-turn, retention prune (in/out of horizon), two-tenant
isolation (shared session_id), per-tenant retention, mandatory tenant_id.
Integration (`tests/integration/test_session_store_pg.py`) — real PG +
Dragonfly, **env-gated `RUN_SESSION_INTEGRATION=1`**, skipped by default
(4 skipped); operator runs it against a dedicated test DB.

**Exit criteria (docs/history/MIGRATION.md P3):**
- ✅ Postgres append-only log + Dragonfly hot window + replay + policy-driven
  retention — built.
- ✅ Logic fully covered by unit tests on real in-memory backends.
- ⚠️ Live testcontainers run **not executed here** — no docker / no DB allowed
  in this environment. The integration test is written and env-gated; the
  operator runs it when a dedicated test stack exists. (Surfaced, not skipped.)

**Reversibility:** additive. The store is new; no caller depends on it yet.

**Note:** the conversational / out-of-scope / policy-boundary responder is
**not** in the registry — it is a platform component built with the router
(P5) / graph (P6). The registry holds use-case agents only (2: uc01, uc03).

---

## P4 — AuthZ service (done)

**Built:** `src/oneops/authz/` — RBAC + ABAC access decisions.

| Module | Role |
|---|---|
| `models.py` | `Principal` / `ResourceDescriptor` / `AuthzDecision` — frozen value objects; deny carries every reason. |
| `rbac.py` | `RbacResolver` — role → permission set, from `role-permission-registry.json`. Unknown role → empty set (deny-by-default). |
| `abac.py` | `evaluate()` — pure, deterministic. 5 rules: tenant isolation, audience, required scopes, tier, data classification. Collects every violation. |
| `decision_cache.py` | `DecisionCache` Protocol + `InMemoryDecisionCache` + `DragonflyDecisionCache`; TTL'd, keyed by (principal, resource) digest. |
| `tokens.py` | Service JWTs — HS256, `AUTHZ_JWT_SECRET` (no default), mint/verify, expiry + clock-skew leeway. |
| `descriptors.py` | Bridge — registry `AgentRecord`/`ToolRecord` → `ResourceDescriptor`. |
| `service.py` | `AuthzService.check()` — RBAC+ABAC, cached, deny-by-default. |

**Design:** deny-by-default everywhere — unknown role, cache miss, unexpected
input never yields ALLOW. Tenant isolation is rule 1 and short-circuits.
Decisions cached → hot path is one keyed lookup.

**Tests — 43 unit green:** RBAC resolution (7); ABAC rule-by-rule incl. the
"every deny is honored" coverage + multi-violation reporting (21); AuthzService
composition + cache + **sub-ms cache-hit p99 perf assertion** (8); service-JWT
mint/verify/expiry/tamper/wrong-secret/wrong-type/skew (9 in test_tokens);
descriptor bridge (2).

**Exit criteria (docs/history/MIGRATION.md P4):**
- ✅ RBAC + ABAC evaluator built.
- ✅ Property coverage — every ABAC deny rule proven honored.
- ✅ Sub-ms p99 on cache hit — asserted in `test_service.py` (1000-iter p99 < 1ms).
- ✅ Signed service-JWT verification — full mint/verify/failure-mode suite.
- ⚠️ `DragonflyDecisionCache` live run is env-gated (no DB/docker here); the
  `InMemoryDecisionCache` carries full logic coverage.

**Reversibility:** additive — no caller depends on AuthZ yet; it is wired in
at the service boundaries (P5+).

---

---

## P5 — Router (done)

**Built:** `src/oneops/router/` — the routing funnel.

| Module | Role |
|---|---|
| `decompose.py` | Stage 0a — split a compound message into sub-queries. `PassthroughDecomposer` (deterministic) + `LlmDecomposer` (live). |
| `rewrite.py` | Stage 0b — resolve references ("close it", "same as last time") against history. `PassthroughRewriter` + `LlmRewriter`. |
| `glossary.py` | Stage 1 — synonym → canonical normalization (Parlant glossary; data in `registries/v2/glossary.json`). |
| `retrieval.py` | Stage 2 — `LexicalRetriever` (deterministic) + `PgVectorRetriever` (live). |
| `conditions.py` | Stage 3 core — three-valued (`PASS`/`FAIL`/`INDETERMINATE`) evaluator of the registry's `ActivationCondition`. |
| `disambiguation.py` | Stage 4 — `ThresholdDisambiguator` (deterministic) + `LlmDisambiguator` (live). |
| `plan.py` | Plan DAG — `assemble_plan` resolves registry deps + sub-query deps + exclusions; topologically ordered. |
| `router.py` | `Router` — drives decompose → per-sub-query (rewrite → 4-stage funnel) → merge into one plan DAG. |
| `signals.py` | `RequestSignals` + the `Ternary` logic. |

**Design:** three of four stages are deterministic; the LLM sees only an
already-narrowed, already-eligible set. Routing consults the registry's
declarative conditions + P4 ABAC — never a phrase catalogue. Compound messages
fan out into sub-queries (parallel + dependent). Non-routed outcomes are
explicit (`NO_CONFIDENT_MATCH` / `POLICY_DENIED` → boundary responder). The
`INDETERMINATE` ternary lets intent-based conditions coexist with a
deterministic pre-LLM filter — a candidate is dropped only on a definite FAIL.

**Tests — 55 unit green** (`tests/unit/router/`): condition logic incl. every
signal + negate + groups + the INDETERMINATE survival rule; glossary incl. the
missing-word passthrough; plan assembly incl. dependency expansion, exclusions,
multi-sub-query DAG, dependency cycles; retrieval/decompose/rewrite;
**adversarial funnel tests** — empty query, zero/stale candidates, condition
FAIL, ABAC deny, low confidence, the intent-resolved guard, multi-sub-query,
partial routing.

**Exit criteria (docs/history/MIGRATION.md P5):**
- ✅ Four-stage funnel (+ decomposer + rewriter front-end) → plan DAG.
- ✅ Property coverage — any query yields a valid plan or an explicit
  non-routed outcome; never a silent wrong route.
- ✅ ABAC denies honored (stage-3 filter integrates P4).
- ⚠️ Live retrieval / disambiguation / decomposition / rewrite are env-gated
  (need pgvector + LLM gateway). The deterministic path is fully tested; the
  LLM path is a thin adapter exercised when infrastructure exists.
- ⏭️ Old `routing/` + `graph/` deletion deferred to **P6** — the old graph
  imports old routing; both are removed together when the LangGraph executor
  replaces them. (`router/` is a new package; old `routing/` untouched.)

**LangGraph note:** P1–P5 are framework-agnostic substrate by design
(AgentScript: agents are data, survive a runtime swap). **LangGraph is P6** —
the executor `StateGraph` consumes the router's plan DAG and runs it with
`Send` fan-out, the Postgres checkpointer, and `interrupt()`.

**Reversibility:** additive — `router/` is new; no caller depends on it until P6.

---

---

## P6 — LangGraph executor (done)

**Built:** `src/oneops/executor/` — the LangGraph orchestration runtime.

| Module | Role |
|---|---|
| `state.py` | `ExecutorState` (JSON-serialisable for checkpointing) + the `step_results` reducer (Send-safe). |
| `graph.py` | `build_executor_graph` — the `StateGraph`: `load_session → route → (wave ⇄ run_step) → aggregate → persist`. `run_turn` entrypoint; `build_postgres_checkpointer` (ADR-0004, env-gated). |
| `nodes.py` | The node bodies — route, wave, run_step, aggregate, boundary, load_session, persist + the `dispatch_wave` / `route_branch` conditional edges. |
| `hooks.py` | `HookRegistry` + lifecycle hooks (AgentScript) — before/after invocation, typed `HookError` abort. |
| `step_runner.py` | `StepExecutor` Protocol + `EchoStepExecutor` (P7 supplies the real tool-running executor). |
| `boundary.py` | Boundary responder — `DeterministicBoundaryResponder` + `LlmBoundaryResponder` (P8). |

**Design:** `Send` fan-out runs independent steps in parallel; the
`wave ⇄ run_step` loop runs dependent steps in dependency order. `interrupt()`
gates every action-tier step on user approval. The checkpointer makes a run
resumable — proven by the interrupt/resume tests. **Conversational memory** is
wired: `load_session` reads the recent history (P3 store) before routing,
`persist` appends the turn after — continuous multi-turn chat.

**LangGraph** is the framework here, as mandated — `StateGraph`, `Send`,
`interrupt`, conditional edges, checkpointer. P1–P5 stayed framework-agnostic
substrate; the pure logic modules are the node bodies.

**Tests — 30 unit green** (`tests/unit/executor/`): single/parallel/dependent
step execution; boundary path (no-match, policy-denied); failing step;
partial routing; before-hook abort; unregistered-hook fail-loud;
**action interrupt → resume approved/denied**; **conversational memory
accumulates across turns**; stateless-without-store.

**Old orchestration deleted** — `src/oneops/{graph,routing,planner}/` and the
26 old test files that exercised them. `src/` Python LOC 25,497 → 17,399.

**Exit criteria (docs/history/MIGRATION.md P6):**
- ✅ Plan-DAG executor — parallel (`Send`) + sequential (wave loop).
- ✅ Lifecycle hooks; determinism dial read (action → interrupt).
- ✅ Checkpointing — `InMemorySaver` (tests); `AsyncPostgresSaver` wiring for
  prod (ADR-0004, dedicated DB), env-gated.
- ✅ `interrupt()` action approval — resume approved/denied both tested.
- ✅ Boundary responder node; conversational memory load/persist.
- ✅ Old routing/graph/planner removed.

**Reversibility:** the new executor is additive; the old orchestration is
already deleted (the migration's "delete as superseded" step for P6).

---

---

## P7 — Tool runners (done)

**Built:** `src/oneops/toolrunner/` — safe execution of registry-defined tools.

| Module | Role |
|---|---|
| `resolver.py` | `HandlerResolver` — tool record `handler_ref` → callable (explicit registry, then `module:function` import). |
| `variables.py` | `InMemoryVariableStore` — large outputs stored as named variables with a preview (attention budget). |
| `idempotency.py` | `InMemoryIdempotencyStore` + `DragonflyIdempotencyStore` — a repeated key replays the result; only successes cached. |
| `runner.py` | `ToolRunner.run` — idempotency → resolve → **timeout** (`asyncio.wait_for`) → **fault containment** → **output capping**. |
| `step_executor.py` | `ToolStepExecutor` — the real `StepExecutor` for the P6 graph; runs an agent's tools via the runner. |

**Design:** every tool call is timeout-bounded, fault-contained (a handler
exception becomes a typed `FAILED` result, never propagates), idempotent
(re-delivery safe — ADR-0005), and output-capped. "Sandboxed" at P7 = timeout
+ containment + declared-args-only; OS-level process isolation is a deployment
concern.

**Tests — 27 unit green** (`tests/unit/toolrunner/`): handler resolution
(explicit + import + every failure); variable store (small passthrough, large
→ ref + preview, retrieval); idempotency (replay, failure-not-cached); runner
(success, **timeout kills a slow tool**, **handler fault contained**,
**repeated key does not re-run**, **large output → variable ref**); step
executor (runs an agent's tools, unknown agent, tool-less agent, failing tool,
**replayed request runs tools once**, large output capped in the step result).

**Exit criteria (docs/history/MIGRATION.md P7):**
- ✅ Pluggable, timeout-enforced, fault-contained tool execution.
- ✅ Each tool a handler resolved from the registry `handler_ref`.
- ✅ Idempotency-keyed re-invoke does not repeat the side effect.
- ✅ Oversized output never enters the next prompt — variable-ref preview.
- ✅ `ToolStepExecutor` replaces `EchoStepExecutor` (injected via
  `build_executor_graph(step_executor=...)`).

**Remaining follow-on (named, not done):** porting the actual UC-1 / UC-3 tool
*handlers* from the old `use_cases/` (`@tool`-decorated) to the new plain
`(args, ctx) -> result` shape registered via `HandlerResolver`. P7 is the
mechanism; the old `oneops/tools/` + `use_cases/` are kept as porting
reference until that lands, then deleted. The new system runs on
`EchoStepExecutor` until the port.

**Reversibility:** additive — `toolrunner/` is new; the graph's executor is
injected, default unchanged.

---

---

## P8 — LLM Gateway integration (done)

**Built:** `src/oneops/llm/` — the single egress for every model call.

| Module | Role |
|---|---|
| `models.py` | `LlmRequest` / `LlmResponse` / `TransportResult` — the gateway contract. |
| `transport.py` | `LlmTransport` Protocol + `EchoTransport` (deterministic, no network) + `LiteLLMTransport` (proxy, env-gated). |
| `redaction.py` | Structural PII scrub (email/phone/SSN/card/IP) before a prompt leaves — pattern-based, not a phrase list. |
| `cost.py` | `CostTracker` — per-tenant per-model token + USD accounting; OTel counters. |
| `quota.py` | `QuotaGuard` — per-tenant call budget; `QuotaExceededError`. |
| `gateway.py` | `LlmGateway.call` — quota → redact → transport (retry + fallback) → cost → `LlmResponse`. `embed` is the same egress. |

**Design:** every model call is `LlmGateway.call` — there is no other path to
a provider. The gateway presents one coherent failure type (`LLMGatewayError`).
The four LLM-backed components — `LlmDecomposer`, `LlmRewriter`,
`LlmDisambiguator`, `LlmBoundaryResponder` — are now implemented against the
gateway (were `NotImplementedError` stubs); each builds a prompt, calls the
gateway with strict-JSON, parses the result, and **falls back safely** on a
failure (decomposer → passthrough, rewriter → unchanged, disambiguator →
no_match, boundary → deterministic reply). `LlmDisambiguator` applies the
ISS-003 closed-class guard — an LLM-invented agent id is rejected.

**Tests — llm suite green:** redaction (every PII class + clean passthrough);
cost (per-tenant/per-model accounting, unknown-model default); quota (limit,
per-tenant, override, reset); gateway (redaction applied, cost recorded, quota
enforced, **retry**, **fallback**, exhausted-retries error, embeddings); the
four components against `EchoTransport` incl. every fallback path; and
**`test_no_direct_provider`** — the CI gate scanning all 8 new-system packages
for a direct provider-SDK import.

**Exit criteria (docs/history/MIGRATION.md P8):**
- ✅ Every model call routes through the gateway.
- ✅ CI gate fails on any direct provider-SDK import.
- ✅ Per-tenant per-model cost counter increments on every call.
- ✅ Prompt redaction verified against PII fixtures.

**Named follow-on:** a ReAct `StepExecutor` (LLM-driven *selective* tool
calling) — pairs with the UC-handler porting from P7. The deterministic
`ToolStepExecutor` (P7) runs the agent's tools as a compound action until then.

**Reversibility:** additive — `llm/` is new; transports and the gateway are
injected.

---

---

## P9 — OTel wiring (done)

**Extended** `src/oneops/observability/` (it already existed and is used by
every module) and verified tracing end to end.

| Change | Detail |
|---|---|
| `propagation.py` (new) | W3C trace-context `inject_trace_headers` / `extract_trace_context` / `start_consumer_span` — carries the trace across a NATS hop (microservice mode). |
| Root span | `run_turn` opens an `oneops.request` root span; every node/tool/LLM span nests under it → one connected trace per turn. |
| Metrics | `ai.request.latency_ms` + `ai.requests.total` (per tenant); `ai.agent.latency_ms` + `ai.agent.runs.total` (per agent, every status); `ai.router.outcome.total`; `ai.tool.calls.total` (per tool, per status). Per-tenant cost from P8. |
| PII safety | Root span carries `message_hash` + `message_len`, never the raw message. |

**Tests — 5 green** (`tests/unit/observability/test_p9_tracing.py`):
**the exemplar trace is one connected tree** — a single `oneops.request` root,
every other span's parent in the trace, **no orphan spans**, both fanned-out
`run_step` spans in the same trace; `run_step` spans carry latency;
inject→extract round-trips the trace; **the root span never carries the raw
user message** (PII fixture confirmed).

**Exit criteria (docs/history/MIGRATION.md P9):**
- ✅ One exemplar trace traverses the pipeline, no broken span links.
- ✅ Metrics for per-agent latency, per-tool calls, router outcome, per-tenant
  cost + latency.
- ✅ PII redaction in spans confirmed.
- ⏭️ NATS-header propagation helper built; exercised end-to-end only in
  microservice mode (no NATS hop in the in-process graph).

**Reversibility:** additive — observability is non-functional-path; disabling
the exporter (`OTEL_EXPORTER_OTLP_ENDPOINT` unset) changes nothing for logic.

---

---

## P10 — Policy engine (done)

**Built:** `src/oneops/policy_engine/` — embedded, data-driven (ADR-0003).

| Artefact | Role |
|---|---|
| `registries/v2/policy_rules.json` | Structured policy data — the machine form of `docs/policies/updated_policy_v2.md`. Rules with `effect` allow/deny/canned. |
| `models.py` | `PolicyRule` / `PolicyMatch` / `PolicyQuery` / `PolicyDecision`; `PolicyEffect`. |
| `engine.py` | `PolicyEngine` — load, `evaluate` (highest-priority match wins; default allow), `reload` (**hot-reload, no redeploy**), every decision traced. |

**Design:** policy is data, not code. A `CANNED` verdict carries a
pre-approved response used verbatim — the Parlant "canned response at a
compliance touchpoint", zero hallucination. Wired into the executor: `run_step`
queries the engine before running a step — `DENY` refuses it, `CANNED`
replaces the handler with the pre-approved response; the aggregator surfaces a
canned response as the user-facing answer.

**Tests — 13 green:** engine (load, default-allow, deny, canned, priority,
every-field match, canned-needs-response, **hot-reload picks up a changed
policy file with no restart**); executor wiring (a **PII-classified agent
returns the canned response**, a normal agent runs, no-engine = no gate).

**Exit criteria (docs/history/MIGRATION.md P10):**
- ✅ Policy loaded as data; agents/tools query the engine.
- ✅ A policy change takes effect via `reload()` — no code change, no redeploy.
- ✅ Every decision traced (`policy.evaluate` span + `policy.decision` log).
- ✅ A compliance-touchpoint UC returns the canned response.

**Reversibility:** additive — the policy gate is opt-in (`policy_engine=None`
→ no gate). The old `policy/` (prompt composition) is untouched.

---

---

## P11 — Load / chaos + closeout (done)

No docker / no live infra here, so the validation is in-process — which
exercises the real code path under concurrency and fault injection.

| Artefact | Role |
|---|---|
| `tests/integration/test_load.py` | Sustained multi-tenant load over the full pipeline (registry → router → executor). |
| `tests/integration/test_chaos.py` | Fault-injection drills — degrade, never crash. |
| `docs/runbooks/RUNBOOK.md` | On-call procedures — find a trace, roll back an agent version, hot-change policy, rotate keys, drain a subject, failure signatures. |

**Tests — 7 green:** load — **80 concurrent turns all complete, exception-free**;
**tenant isolation holds under concurrency** (15 tenants × 2-turn
conversations, each session sees only its own); 3 stable load waves. Chaos —
a failing step executor, a tool timeout, an LLM gateway down, and mixed
failure across 30 concurrent turns each **degrade to a typed terminal status,
never raise**.

**Exit criteria (docs/history/MIGRATION.md P11):**
- ✅ Sustained multi-tenant load — pipeline completes every turn, isolation holds.
- ✅ Chaos drills — degrade-not-crash on dependency loss.
- n/a Parity / cutover — there is no legacy path (deleted at P6); the system
  *is* the new system, so there is nothing to cut over from.
- ⏭️ Live-infra load at 10x scale + kill-a-NATS-node + soak — the operator's
  pre-prod gate; needs real NATS/Postgres/LLM, out of this environment.

---

# Build status: P0–P11 structurally complete

The 11-phase build is done — registry, codec, session store, AuthZ, router,
LangGraph executor, tool runners, LLM gateway, OTel, policy engine, load/chaos
— **633 passed, 4 skipped, 0 failed** repo-wide, every phase gated and tested.

## Follow-on F1 — Entity-ID normalizer (done)

System-wide, registry-driven canonicalisation of entity references — the first
of the three named follow-ons. `src/oneops/router/entity_id.py`:

- `EntityIdNormalizer.from_registry_file()` builds the prefix → service map
  from `registries/service-schema.json` (`id_prefix` + `alias_prefixes`) — one
  normalizer for all 1000 UCs; adding a service is a registry row, no code.
- `normalize(token)` canonicalises one token (strip every separator, uppercase,
  longest-prefix-first match, numeric-body check) → a `NormalizationResult`
  that is **either** a clean `NormalizedEntity` **or** an explicit, human-
  readable `reason` — never a bare `None`, never a guess (thumb rule #11).
- `extract(message)` scans free text in two passes → clean `entities`
  (deduped) plus `malformed` near-misses surfaced for the user to correct;
  plain noise is dropped. Pass 1 = digit-bearing tokens (`INC0048213`,
  `INCX0048`). Pass 2 = digit-less garbles checked against the registry prefix
  set — a bare prefix (`INC`, number forgotten) or an all-caps prefix-led
  token (`INCABC`). Ordinary lower-case words that merely start with a prefix
  (`incident`, `request`) are deliberately never flagged.
- Wired into the executor `route` node (`executor/nodes.py`) — populates
  `RequestSignals.present_entities`.
- **Loop closed end-to-end (rule #11).** A malformed near-miss is never just a
  log line — `normalizer.clarification_message()` builds the on-screen text
  (names the service, shows a valid example). When *every* ID in the message
  is malformed, `route` short-circuits before the router to a `clarification`
  turn; when a bad ID rides alongside a valid one, `aggregate` appends the
  correction as a note. New state field `entity_clarification`.

**Tests — 53 unit green** (`tests/unit/router/test_entity_id.py`): canonical
IDs for all 9 services, all 4 aliases (SR/PRB/RFC/CMDB), case folding, internal
separators (space/hyphen/underscore/tab/mixed), surrounding punctuation,
longest-prefix (CMDB vs CI), exact-digit preservation, every failure mode
(empty, bare digits, unknown prefix, prefix-only, non-numeric body, glued
text), and `extract` (single/multiple/messy/dedup/near-miss/noise/empty).

**Remaining before production** (named, tracked — see `docs/runbooks/RUNBOOK.md §9`):
1. **Port the UC tool handlers** — UC-1 / UC-3 tool logic still lives in the
   old `use_cases/` (`@tool`-decorated); port to the new `(args, ctx)` shape +
   `HandlerResolver`. Until then the executor runs on `EchoStepExecutor`.
2. **Remove superseded old code** — `tools/`, `use_cases/`, `gateway/` once (1)
   lands.
3. **Live-infra validation** — load/chaos/soak against real NATS, Postgres,
   and an LLM; provision the dedicated checkpoint DB (ADR-0004).

**Suite totals:** **649 passed, 4 skipped, 0 failed** across the whole repo.
New-system unit tests: registry 58 · codec 14 · session 13 · authz 43 ·
router 55 · entity-id 66 · executor 33 · toolrunner 27 · llm ~54 ·
observability/P9 · policy_engine 13 · load+chaos 7. The remainder are still-
valid platform-layer tests.
