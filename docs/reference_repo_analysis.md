# Reference Repository Analysis

**Purpose.** Extract engineering patterns, architectural principles, and production-grade practices from the reference repositories in `repo/reference-repos/`, then evaluate their applicability to **AI-service** (the OneOps-NextGen agentic ITSM/ITOM platform). Patterns are *not* to be copied; they are a knowledge base for the gap analysis, architecture review, and refactor roadmap.

**Reference corpus.**
| Repo | What it is | Primary lens |
|---|---|---|
| `langgraph_docs/` | LangGraph official docs (12 files) | Graph orchestration, HITL/interrupts, persistence, durable execution |
| `agno_docs/` | Agno framework docs (14 files) | Agent/team/workflow/memory/session design |
| `openai-agents-python/` | OpenAI Agents SDK (778 py) | Agent abstraction, tools, handoffs, guardrails, model-provider port, deterministic testing |
| `opentelemetry-python-contrib/` | OTel instrumentation (776 py) | GenAI semantic conventions, instrumentor pattern, cardinality discipline, context propagation |
| `langfuse/` | LLM-observability product (2,576 ts) | Observation data model, cost-as-data, evals/scores, prompt versioning, PII externalization |
| `fastapi-best-practices/` | Opinionated FastAPI guide | Project structure, async discipline, DI, settings, DB, errors |
| `samples-python/` | Temporal Python samples (601 py) | Durable execution, saga/compensation, retry taxonomy, idempotency, time-skipping tests |

AI-service context assumed throughout: Python 3.12, FastAPI, **LangGraph executor** (`load_session → update_focus → control_gate → route → wave → run_step → aggregate → boundary → persist`, Postgres checkpointer, `interrupt()` for catalog-selection / approval / slot-filling), a **4-stage routing funnel** (decompose → rewrite → retrieve-then-decide → disambiguate), **registry-driven agents-as-data**, a **`handler_ref → callable` resolver**, a **LiteLLM gateway** as the single LLM egress, Dragonfly cache, NATS dispatch for fulfillment, OTel→Tempo/Prometheus/Grafana + Langfuse.

---

## 1. LangGraph + Agno — orchestration, HITL, persistence, memory

LangGraph is the runtime in use, so these patterns are first-class; Agno is read as a cross-framework design check (it agrees on the substance).

### 1.1 Workflow / graph orchestration
- **State as typed channels with reducers** (`02_graph_api`). Each state key carries a reducer (overwrite vs `add`-accumulate). *Applies:* AI-service's `aggregate` must collect concurrent `run_step` writes into an additive channel, or risk `INVALID_CONCURRENT_GRAPH_UPDATE`. **This is a correctness requirement, not a nicety.**
- **Super-step parallelism + `Send` map-reduce** (`02_graph_api`, `07_workflows_agents`). One routing fn emits `Send(node, payload)` per planned step; a wave = one super-step; fan-in via additive reducer. *Applies:* the `wave`/`run_step`/`dispatch_wave` loop is exactly this; lean on it instead of hand-rolled concurrency.
- **`Command(update=…, goto=…)`** unifies state mutation + routing in one node return. *Applies:* the planner→executor handoff. **Caveat the docs stress:** never pass `Command(update=…)` as *invoke input* for a fresh turn — only `Command(resume=…)` is a valid Command input (relevant to AI-service's resume path).
- **Named workflow archetypes** (`07_workflows_agents`; Agno `12_workflows`): prompt-chaining, parallelization, routing, orchestrator-worker, evaluator-optimizer, ReAct. *Applies:* classify each UC — deterministic UCs (UC-1 summary, UC-5 triage) are *chains* and should not pay for a dynamic planner; open chat is *routing + orchestrator-worker*. Reserve the planner for unconstrained intent. **Cost lever.**

### 1.2 Human-in-the-loop / interrupts / durable execution
- **`interrupt()` restarts the node from the top on resume** (`05_interrupts`, `08_durable_execution`). **The single highest-leverage correctness rule for AI-service.** On `Command(resume=…)` the node body re-runs; therefore *all side effects (DB writes, ticket creation, charges) must happen AFTER the `interrupt()`*, never before, or they double-execute.
- **Three canonical HITL shapes** map 1:1 to AI-service's three interrupt sites: approve/reject (`goto cancel` by default = fail-closed) → **approvals**; review-and-edit → **catalog form**; validate-loop (`while True: interrupt`) → **slot-filling**.
- **Side effects must be `@task`-wrapped for replay safety** (`08`, `10`). Replay re-runs node code; only `@task` results are memoized. Combined with idempotency, this is the safe-resume contract.
- **Durability mode dial** (`exit`/`async`/`sync`). *Applies:* `async` for read flows, **`sync` before a confirmed write** so the checkpoint is durable before the next node.
- **Resume-after-failure with `invoke(None)`** (`03`, `08`): completed nodes in a failed super-step aren't re-run (pending-writes). *Applies:* free fault-tolerance for LLM/transport blips — retry the run, not the whole plan.

### 1.3 Persistence & checkpointing
- **Thread = session; checkpoint = per-super-step snapshot** with a `next` field (mid-flow vs complete). *Applies:* `session_id ↔ thread_id`; the `next` field answers "is this session awaiting input?".
- **Postgres checkpointer is the production tier**; `.setup()` once. *Validates* AI-service's choice (auditability over Redis latency).
- **State history + time-travel + `update_state(as_node=…)`** = the **audit trail** for ITSM: every AI step replayable; an operator can correct state and resume. Strong compliance fit.
- **Checkpoint encryption (`EncryptedSerializer`, AES key from env).** *Gap flag:* checkpoints contain ticket PII; encrypt the serde, don't rely only on DB-at-rest.

### 1.4 State & memory
- **Two-tier memory**: checkpointer (short, per-thread) vs **Store** (long, cross-thread, namespaced `(tenant,user)`), with semantic search inside the Store. Agno states it crisply: *Session History* (transcript) vs *Memory* (learned facts). *Applies:* AI-service has short-term; a cross-thread profile/RBAC/"same issue as last week" store is the natural extension of its pgvector substrate.
- **Identity rides in runtime context, not state** (`Runtime[Context]`; Agno `RunContext`). *Applies:* **tenant/user/role must not be LLM-mutable state** — a multi-tenant isolation boundary. Tools read context, never trust LLM-supplied tenant args.
- **Session-state-as-slot-store surfaced into the prompt by `{key}`** (Agno `10_session_state`). *Applies:* the exact model for slot-filling — the draft lives in checkpointed state, the interrupt loop fills it, the prompt renders current draft each turn.
- **History token budgeting** (`num_history_runs`, summaries). *Applies:* cap history per agent (more for slot-filling, less for one-shot readers); summarize long sessions.

### 1.5 Streaming
- **Seven stream modes**: `values/updates/messages/custom/checkpoints/tasks/debug`. *Applies:* `updates` → the step feed ("running triage…"); `messages` → user-facing tokens; `custom` → progress; `checkpoints` → the audit view.
- **Tag-based filtering (`nostream`)**: tag router/planner/judge calls `nostream`, stream only the final generation. *Applies:* keeps internal reasoning out of UI + off the wire; supports the ~1s-perceived-latency goal.

### 1.6 Subgraphs / multi-agent / teams
- **Specialist = compiled subgraph node; orchestrator routes** (`09_subgraphs`). *Applies:* the scaling path beyond a flat executor — capability domains become independently testable subgraphs with their own checkpoint namespace. **A subgraph needs `compile(checkpointer=True)` to own interrupts.**
- **Delegation modes as a cost/error matrix** (Agno teams): `route` (one member), `coordinate` (decompose+synthesize, **tolerates partial member failure**), `broadcast`, `tasks`. *Applies:* the `aggregate` node should replicate `coordinate`'s partial-failure semantics — **don't fail the whole wave on one step's error.**

### 1.7 Reliability (LangGraph)
- **Pending-writes** (only the failed step re-runs), **idempotent `@task` tools**, **node caching** (`CachePolicy(ttl)`), **recursion-limit guard** (cap loops; graceful terminal branch). *Applies:* AI-service's wave/aggregate, action tools, deterministic fetch nodes, and any evaluator/retry loop.

---

## 2. OpenAI Agents SDK — agent abstraction, tools, guardrails, model port, testing

### 2.1 Agent & tools
- **Agent-as-config dataclass** (`agent.py`): instructions + tools + handoffs + model + guardrails + `output_type`, pure data, validation in `__post_init__`, orchestration in the `Runner`. *Validates* agents-as-data; AI-service's **registry-as-table is the production form** of this (SDK keeps agents in Python literals).
- **Derive tool schema from the handler signature** (`@function_tool`, `function_schema.py`): JSON schema generated from the typed signature + docstring via `griffe`. *Applies:* generate registry tool `params_json_schema` from the handler's typed signature instead of hand-maintaining JSON (a §2.1 "derive, don't hardcode" win).
- **Uniform invoker `FunctionTool(…, on_invoke_tool(ctx, json)→output)`** — this *is* AI-service's `handler_ref → callable` shape; `ToolContext` carries per-call identity.
- **`tool_use_behavior` short-circuit** (`stop_on_first_tool`, callback): skip the second LLM hop for deterministic read tools. *Applies directly* to the router-collapse latency work (3-LLM→1).
- **Conditional tool enabling** (`is_enabled(ctx,agent)`): hide tools the role can't use *at the model boundary*. *Applies:* RBAC tool-surface filtering, cheaper than post-hoc rejection.
- **Per-tool timeouts** with recoverable default. *Applies:* DAL/DB tool calls and the remote-Tokyo-DB latency tail.

### 2.2 Guardrails (defense-in-depth)
- **Three tiers** at distinct boundaries: input (first agent), output (final agent), **tool guardrails (every function-tool call, before+after)**. *Applies:* the tool tier is the one to adopt for ITSM — validate/redact every DAL or action-tool call.
- **Blocking vs parallel knob**: blocking (before agent — no spend on tripwire) for destructive/cost-sensitive; parallel (concurrent) for read latency. *Reusable cost/latency lever.*
- **Guardrail-as-cheap-agent**: a small model guards an expensive one (composes with the LiteLLM gateway).

### 2.3 Model-provider abstraction (the gateway template)
- **`Model` + `ModelProvider` two-ABC split** (`models/interface.py`): `Model.get_response()/stream_response()` (identical signatures) + `close()` + **`get_retry_advice()`**; `ModelProvider.get_model(name)`. The whole SDK depends only on these two ABCs. *This is the canonical shape for AI-service's LiteLLM gateway port* — the executor never knows the concrete provider; LiteLLM/OpenAI/**a fake** all satisfy it. Provider-specific retry hints (Retry-After, replay-safety) are **encapsulated on the model**, not leaked into the runner.
- **Three-tier model resolution**: env default → `RunConfig` per run → `Agent.model` per agent; family-aware `ModelSettings` (reasoning effort per UC). *Applies:* override model at env/request/agent-record levels; low reasoning effort for latency-sensitive UCs.
- **Composable retry policies as data** with explicit **replay-safety** so non-idempotent calls aren't blindly retried. *Applies:* retry rules as composed data (§2.1), never re-issue non-idempotent ITSM action tools.

### 2.4 Structured outputs, errors, testing
- **`output_type` → provider structured-output + typed `final_output`**; routers emit closed-enum `Literal`. *Applies:* deterministic downstream handling, exact routing assertions.
- **Typed exception hierarchy** (`ModelBehaviorError`, `ModelRefusalError`, `MaxTurnsExceeded`, tripwires) + `RunErrorDetails` attaching full run state; **`error_handlers`** return a safe canned output instead of raising. *Applies:* distinguish model-misbehavior vs user/config error vs guardrail-block; return safe answers on turn-limit/refusal rather than 500.
- **`FakeModel` implementing the `Model` interface** (`tests/fake_model.py`): script per-turn outputs (incl. exceptions), capture `last_turn_args`. **The single most copyable testing pattern.** *Applies:* a `FakeGateway` over the LiteLLM port makes the *entire* funnel + executor deterministically testable with zero network/cost. A `pytest` marker gates any real-model call.

### 2.5 Where AI-service is already ahead
- Agents-as-data **in a DB table** (SDK: Python literals).
- **Retrieve-then-LLM-decide funnel at 100+ agents**, provider-neutral via LiteLLM (SDK: inject-all + hosted tool-search, OpenAI-only, collapses ~30-50 tools). Keep the funnel; adopt the SDK's **<10-functions-per-namespace** shortlist-size heuristic.

---

## 3. OpenTelemetry-contrib + Langfuse — observability

The two meet at the **GenAI semantic conventions** (`gen_ai.*`): contrib *emits* them, Langfuse *ingests* them. AI-service sits on this seam (OTel→Tempo + Langfuse).

### 3.1 Emission discipline (contrib)
- **Instrumentor pattern** (idempotent enable/disable) + **hard layer boundary**: provider packages only parse I/O; **all** span/metric/event work lives in one `TelemetryHandler`. *Applies — highest structural leverage:* centralize span/metric emission in one handler; routers/executor/NATS handlers call `handler.start_*()`, never `tracer`/`meter` directly. **This kills attribute drift across UCs.**
- **GenAI semconv attributes from the enum module, never string literals** (`gen_ai.request.*`, `gen_ai.response.*`, `gen_ai.usage.{input,output,cache_*,reasoning}_tokens`, `gen_ai.provider.name`). *Gap flag:* AI-service likely misses **cache tokens + reasoning tokens** → cost under-counting on thinking models. **Output tokens = output + thinking, summed at emit.**
- **Cardinality firewall**: invocation carries two dicts — `attributes` (spans, high-card OK) and `metric_attributes` (**must be low cardinality**: operation/provider/model/error.type only). *Applies — critical:* **tenant_id/session_id on spans only, never on metrics**, or Prometheus explodes. Two GenAI instruments total (duration + token histograms), with **LLM-appropriate bucket boundaries** (sub-second to ~80s) not HTTP defaults — relevant to the latency-RCA band.
- **Metrics independent of span sampling** — record cost/token metrics even on un-sampled traces, or billing silently drops sampled-out traffic.
- **NATS context propagation** (the Kafka pattern): inject `traceparent` into message headers on publish, extract on the subscriber before starting the span. *Likely concrete gap:* if NATS hops don't carry `traceparent`, cross-service fulfillment legs appear as disconnected traces.
- **PII externalization (CompletionHook)**: write prompts/outputs off the hot path to object storage, stamp only `…messages_ref` URIs on the span; hash-dedupe identical prompts (the router prompt is identical across calls). *Applies:* spans stay PII-free, bodies sit in tenant-scoped storage. **Telemetry must never throw into the app** (wrap all emit sites).

### 3.2 Backend data model (Langfuse)
- **Cost is derived downstream from tokens + a versioned price table**, not on the span (regex `match_pattern` model lookup, `start_date`-versioned prices, L1+L2 cache). *Applies — high, and aligns with the never-hardcode rule:* **pricing belongs in a versioned DB table (JSON→table→cached lookup), never literals in code.**
- **Session ⊃ trace ⊃ observation + user dimension**: stamp `sessionId` + tenant/user on the root trace → free multi-turn replay (`gen_ai.conversation.id` is the emit-side equivalent).
- **Scores as append-only polymorphic records** (`source ∈ {API, EVAL, ANNOTATION}`, attach to trace/observation/session). *Gap:* AI-service has **no eval substrate**; this is the model — LLM-as-judge (`EVAL`), user feedback (`API`), human label (`ANNOTATION`). For ITSM: "was routing correct / KB answer grounded" scores attach to the routing trace.
- **Versioned, labeled prompt store**; each generation records `promptVersion`. *Gap:* AI-service prompts live in code/JSON; a versioned store lets you A/B prompts and attribute accuracy regressions to a rev — serves the prompt-hardening work.
- **Observability 2.0 / wide events** (Langfuse's thesis): model the observation as the primary unit; a trace is a correlation handle; prefer wide richly-attributed immutable events over fragmented metrics+logs. *Review rubric:* before adding a metric, ask if a wide event answers it; before a join, ask if the attribute should be denormalized onto the observation.

---

## 4. FastAPI best-practices — structure, async, DI, settings, DB

### 4.1 Patterns advocated
- **Domain-driven packages**: `src/{domain}/{router,schemas,models,service,dependencies,config,exceptions}.py`; file-type folders don't scale. *Applies:* UCs are bounded contexts — each UC should be a package.
- **`main.py` is thin: app init + lifespan only**; logic in per-domain routers. **Primary divergence flag** (see §6).
- **Async discipline**: `async def` only for non-blocking awaitable I/O; blocking I/O → plain `def` (threadpool) or wrap sync SDKs in `run_in_threadpool`; never block the loop; threadpool is bounded (40). *Applies:* audit executor/UC handlers for sync calls in async paths.
- **Excessive Pydantic validation at the boundary**; one custom base model (UTC serialization); `ValueError` in validators → 422.
- **Dependencies validate, not just inject**; chain deps (decode token → load tenant → check role → load entity); per-request dep cache; prefer async deps; **override deps in tests, don't monkeypatch**.
- **Split `BaseSettings` per domain** with typed DSNs + an `ENVIRONMENT` enum. **Divergence flag.**
- **SQLAlchemy 2.0 async** `get_db()` dependency, `pool_pre_ping`; **SQL-first, Pydantic-second** (joins/JSON shaping in Postgres). *Applies:* similar-ticket/KB shaping belongs in SQL; the `get_db()` injection aligns with the DAL-as-single-boundary goal.
- **Typed module-scoped exceptions**; never catch bare `Exception` around a route body.
- **Durable queue (not `BackgroundTasks`)** for retriable seconds-to-minutes work. *AI-service already conforms* (NATS + pgmq + worker for embedding refresh).
- **Async test client (`httpx.AsyncClient`) + real DB in integration tests.** *Conforms* to the standing "tests read real seed" rule.
- **Security**: PyJWT (not python-jose), JWT decode in a dependency, CORS via typed settings, hide `/docs` outside dev/staging, secrets via env settings.

### 4.2 Divergences to flag
1. **Monolithic `api/app.py` (2,570 lines)** — startup wiring + route mounting + static frontend + executor boot all in one file. The guide mandates a thin `main.py` + `lifespan` + per-domain routers.
2. **UCs not consistently domain-packaged** (logic spread across shared modules).
3. **Likely one global settings blob** (litellm/dragonfly/nats/checkpointer/otlp host-ports) vs scoped per-concern typed DSN settings.
4. **Raw asyncpg + raw SQL in 10 UC sites** bypassing a single data boundary (the project's own DAL goal).

---

## 5. Temporal samples — workflow orchestration & reliability (patterns, not Temporal)

These map to AI-service's LangGraph-DAG-over-NATS fulfillment + LangGraph-`interrupt()` HITL. **Recommendation framing: adopt the *shapes*, not Temporal.**

- **Durable execution = deterministic orchestration + side-effects-in-activities**; **replayer-as-CI-gate** (re-run real histories against new code to catch determinism breaks); **`patched()` for safe mid-flight code evolution**. *Applies:* capture real fulfillment checkpoints, replay against new DAG code in CI; gate behavior changes so resumed runs follow their original shape.
- **HITL primitives**: signal (async mutate), query (read-only inspect), **update-with-validator (reject pre-commit)**, `wait_condition` (durable pause). The repo's **`langgraph_plugin/.../human_in_the_loop`** is the *exact* LangGraph-`interrupt()` + signal + query shape for AI-service's stack. **Tradeoff table:** LangGraph-interrupt is lighter (no new infra) but pushes **resume-orchestration, input validation, concurrency-serialization, and pending-state exposure into app code**; Temporal exposes those as named primitives. *Adopt the shapes:* validator-before-commit, query-for-pending-draft, **lock-around-async-handler**, finish-handlers-before-exit.
- **Retry/timeout taxonomy**: per-activity `RetryPolicy` (best-effort steps `max_attempts=1`, critical steps bounded-exponential); `start_to_close` vs `schedule_to_close` vs `heartbeat` timeouts. *Applies:* AI-service likely conflates these into a single timeout.
- **Saga compensation**: compensate-only-on-true-cancel/fail (not on a retryable error), reverse-order undo, completion barrier; **`asyncio.gather` does NOT cancel siblings on first failure** — a real bug class for parallel fulfillment waves.
- **Idempotency**: guard inside the handler (short-circuit if already applied) — **critical for NATS at-least-once delivery**; deterministic run-id from the catalog request for create-or-get.
- **Continue-as-new with result caching** to bound replay/checkpoint growth without re-running completed steps (mirrors AI-service's data-flow-binding).
- **Time-skipping tests + activity mocking**: test SLA timers / retry backoff / approval waits in milliseconds; assert call counts to verify idempotency / no-double-fire.
- **`asyncio.Lock` around async handlers** that mutate shared state across an `await` — **a likely latent bug class in any hand-rolled interrupt-resume orchestrator** (concurrent approvals / parallel callbacks).

---

## 6. Cross-cutting applicability matrix

| Pattern (source) | AI-service status | Action class |
|---|---|---|
| Side-effects-after-`interrupt()` + `@task` + idempotency (LangGraph, Temporal) | **At risk** — must verify per interrupt/approval/fulfillment flow | Critical correctness |
| Reducer on the `aggregate` channel (LangGraph) | Likely OK (graph compiles) — verify additive | Correctness |
| `coordinate` partial-failure in `aggregate` (Agno) | Verify wave doesn't fail-all on one step error | Reliability |
| NATS `traceparent` propagation (OTel/Kafka) | **Likely gap** — cross-service legs may be disconnected | High observability |
| Cost = tokens on span, dollars from versioned price table (Langfuse) | **Gap if rates are inline** | High (cost-as-data, §2.1) |
| Cache + reasoning tokens in usage (OTel semconv) | **Likely gap** — cost under-count | High |
| Metric-attr cardinality firewall (OTel) | **At risk** — tenant on metrics? | High (Prometheus stability) |
| One `TelemetryHandler` boundary (OTel) | Hand-rolled spans likely scattered | Architecture |
| `Model`/`ModelProvider` two-ABC gateway port (OpenAI SDK) | Partial (gateway exists) — formalize the port | Architecture |
| `FakeGateway` deterministic LLM testing (OpenAI SDK) | **Gap** — would unlock funnel/executor unit tests | High (testing) |
| Tool guardrail tier on every DAL/action call (OpenAI SDK) | Partial (policy layer) — add pre-LLM + tool-level | Security |
| Identity in runtime context, not LLM-mutable state (LangGraph/Agno) | Verify tenant/role can't be set from LLM args | Security (tenant isolation) |
| Checkpoint PII encryption + trace PII externalization (LangGraph/OTel) | **Gap** — PII in checkpoints/traces | High (compliance) |
| Eval/score substrate (Langfuse) | **Absent** | Medium (quality) |
| Versioned prompt store (Langfuse) | **Absent** (prompts in code) | Medium |
| Thin `main.py` + per-domain routers (FastAPI) | **Divergence** — 2,570-line app.py | Architecture (maintainability) |
| Per-domain settings + typed DSN (FastAPI) | **Divergence** — global blob | Maintainability |
| DAL-as-single-boundary / `get_db()` (FastAPI) | **Divergence** — 10 raw-SQL sites | Architecture (the project's own goal) |
| Saga compensate-on-true-cancel + sibling-cancel (Temporal) | Verify `gather` sibling cancel + compensation trigger | Reliability |
| Idempotency keys for NATS at-least-once (Temporal) | Verify per fulfillment task | Critical reliability |
| Retry/timeout taxonomy per task type (Temporal) | Likely single timeout | Reliability |
| Replayer-as-CI-gate (Temporal) | **Absent** | Medium (durable-state safety) |
| Lock-around-async-handler (Temporal) | Verify concurrent-approval safety | Reliability |
| `nostream` tag for internal LLM calls (LangGraph) | Verify when streaming ships | Performance/UX |

**Single highest-leverage, lowest-cost item across all sources:** enforce **"all side effects after `interrupt()`, every action tool `@task`-isolated and idempotent"** — because LangGraph re-runs node code on resume, this is the line between a safe approval/slot-filling/fulfillment flow and silent double-writes. Every other recommendation is secondary to this.

> Provenance: §1 LangGraph/Agno docs; §2 OpenAI Agents SDK; §3 OTel-contrib + Langfuse; §4 FastAPI best-practices; §5 Temporal samples. Each recommendation in the downstream docs cites its source section here.
