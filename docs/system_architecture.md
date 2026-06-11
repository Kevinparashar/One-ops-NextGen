# AI-service — System Architecture (reverse-engineered)

**Scope.** The OneOps-NextGen agentic ITSM/ITOM platform: a single FastAPI service that exposes AI use cases (UC-1 summarize, UC-2 similar tickets, UC-3 KB lookup, UC-5 triage, UC-8 catalog fulfillment + approval) over a chat door and a fast-path button door, orchestrated by a LangGraph executor over a registry of data-defined agents/tools, backed by Postgres/pgvector, Dragonfly, and NATS, instrumented with OpenTelemetry + Langfuse.

**Size.** ~44k LOC across `src/oneops`. Largest units: `api/app.py` (2,570), `executor/nodes.py` (1,509), `uc08_fulfillment/tools.py` (1,181), `router/router.py` (1,108), `router/rewrite.py` (989), `uc08_fulfillment/executor.py` (869).

---

## 1. Component diagram

```
                              ┌──────────────────────────────────────────────┐
   Browser / API client  ───► │  FastAPI app  (api/app.py, 2570 LOC)          │
   (x-tenant-id, x-user-id,   │  • /api/chat  /api/fast/{uc}  /api/uc02 …     │
   x-role headers)            │  • static frontend (/, app.js)               │
                              │  • lifespan: wires all singletons below      │
                              │  • interrupt capture/resume (2 paths)        │
                              └───────┬───────────────────────┬──────────────┘
                                      │ chat door             │ button door (pre-built plan)
                                      ▼                       │
                  ┌───────────────────────────────┐           │
                  │ ROUTER  (router/*)             │           │
                  │ 4-stage funnel:                │           │
                  │  decompose→rewrite→retrieve    │           │
                  │  →filter→preroute→disambiguate │           │
                  │ → ExecutionPlan (agents+params)│           │
                  └───────────────┬───────────────┘           │
                                  ▼                            ▼
   ┌───────────────────────────────────────────────────────────────────────┐
   │ EXECUTOR  (executor/*)  — compiled LangGraph StateGraph                 │
   │  START→load_session→update_focus→control_gate→route→wave→run_step       │
   │        →aggregate→boundary→persist→END   (interrupt() inside run_step)  │
   │  Postgres checkpointer (thread_id = session_id)                         │
   └───────┬───────────────┬───────────────┬───────────────┬────────────────┘
           │ resolve        │ invoke         │ read/write     │ dispatch
           ▼                ▼                ▼                ▼
   ┌───────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐
   │ REGISTRY      │ │ LLM GATEWAY  │ │ TICKET/KB    │ │ NATS (fulfillment │
   │ agents+tools  │ │ (llm/*)      │ │ STORES +     │ │ task dispatch)    │
   │ as DATA       │ │ single egress│ │ raw asyncpg  │ │ + pgmq (embed     │
   │ +pgvector     │ │ →LiteLLM     │ │ (use_cases/*)│ │  refresh workers) │
   │ (ai.embed_*)  │ └──────┬───────┘ └──────┬───────┘ └──────────────────┘
   └───────┬───────┘        │                │
           │            ┌───▼────┐      ┌─────▼───────────────────────────┐
           │            │LiteLLM │      │ PostgreSQL (Supabase, Tokyo)    │
   ┌───────▼───────┐    │ proxy  │      │  itsm.*  (business)             │
   │ HANDLER       │    │ gpt-4o │      │  ai.*    (embeddings, pgvector) │
   │ RESOLVER      │    └────────┘      │  langgraph checkpoints          │
   │ handler_ref→fn│                    └─────────────────────────────────┘
   └───────────────┘
   ┌──────────────┐ ┌──────────────┐ ┌──────────────────────────────────┐
   │ POLICY/AUTHZ │ │ SESSION STORE│ │ OBSERVABILITY                    │
   │ RBAC/ABAC,   │ │ cold log +   │ │ OTel→Tempo/Prometheus/Grafana    │
   │ redaction    │ │ hot window   │ │ Langfuse (LLM I/O, per-tenant $) │
   │ (policy*)    │ │ (session/*)  │ │ Dragonfly cache (route/turn/edge)│
   └──────────────┘ └──────────────┘ └──────────────────────────────────┘
```

**Package ownership (`src/oneops/`):** `api` (HTTP + lifespan), `router` (funnel), `executor` (LangGraph graph, nodes, step_runner, state, entity_elicitation), `registry` (agents/tools as data, loader, service, store), `toolrunner` (handler resolver + runner), `llm` (gateway, models, transport), `policy`/`policy_engine`/`authz` (RBAC/ABAC, redaction), `session` (durable conversation memory), `use_cases` (uc01/02/03/05/08 handlers + `_shared` stores), `observability` (logger/tracer/metrics/langfuse), `adapters` (NATS resilience, integration adapters), `embeddings`, `conversation`, `tenancy`, `uc_common`, `workers`, `codec`, `db`, `errors`.

---

## 2. Execution flow (chat turn)

```
1. POST /api/chat  (headers → _principal_from_headers → tenant_id/user_id/role)
2. app builds envelope {request_id, tenant_id, user_id, role, message, session_id}
3. chat-turn cache lookup (Dragonfly, sha256 of tenant+user+role+session+message)
      └─ HIT → return cached TurnResponse (≈ms)
4. _run → run_turn(graph, envelope, config={thread_id: request_id})
      (resume path: resume_turn(graph, interrupt_answer, config={thread_id: paused_thread}))
5. GRAPH:
   load_session   → recent() into conversation_history (hot window, trimmed)
   update_focus   → focus_entity_id channel for multi-turn follow-ups
   control_gate   → stage-1 social/off-domain classifier (greeting/chitchat → boundary)
   route          → ROUTER funnel → ExecutionPlan (or decline → boundary)
   wave/run_step  → dispatch each plan step to a handler via resolver;
                    interrupt() here for action-tier approval / catalog selection / slot-fill
   aggregate      → collect step results, render final_response
   boundary       → conversational boundary responder (out-of-scope / no-match)
   persist        → append_turn_events(user + assistant) to session store
6. Interrupt? two surfacing paths:
   (a) raised GraphInterrupt   (no checkpointer)         → app.py exception block
   (b) returned __interrupt__  (checkpointer configured) → app.py post-run block
   Both: cache the pending interrupt (key __interrupt__{session_id}, with thread),
         persist user+clarification (append_turn_events), return final_status=interrupted
7. TurnResponse {door, final_status, final_response, step_results, interrupt, latency_ms, trace_id}
```

**Latency profile (measured, from prior RCA):** input size ≈ free; cost ≈ 0.8s network floor × N LLM calls + ~15–35 ms/output-token + remote-Tokyo-DB ~2.7 s/turn. Cold chat turns 7–12 s; warm (cache) ms.

---

## 3. Routing flow (the 4-stage funnel)

```
message
  │  stage 0a  decompose  (LLM: split multi-intent into sub-queries; canonical-id guard)
  │  stage 0b  rewrite    (LLM: resolve "it"/focus, normalize entity ids; no acronym regex)
  │  stage 2   retrieve   (pgvector over ai.embeddings_agent — top-K agent shortlist on FULL skill body)
  │  stage 3   filter     (activation_condition evaluation: entity_service_in / role_in / intent_in;
  │                        three-valued PASS/FAIL/INDETERMINATE)
  │  stage 3.4 preroute   (deterministic: bare entity id → uc01; doc-noun+id → uc03)
  │  stage 4   disambiguate (LLM: pick survivor(s); emits selected_agent_ids + intents + confidence)
  │            _resolve_chosen  → post-stage-4 guard: re-check activation under classified intent
  │            _declined_outcome → tiebreaker: sole-PASS candidate when disambiguator declines
  ▼
ExecutionPlan {steps:[{agent_id, tool params, depends_on}], route_outcome}
```

**Design principles in force:** descriptions are *semantic principles, not phrase catalogs* (§2.1 — no keyword/synonym tables on the path); the LLM is the decision-maker; routing is **card-driven (`use_when`/`not_when`), no rigid axis**; retrieve-then-LLM-decide scales to 100+ agents (inject-all collapses past ~30-50). Route decisions are cached in Dragonfly (decision is identical for everyone, forever) keyed on the rewritten query.

---

## 4. LangGraph flow (executor internals)

- **Graph build** (`executor/graph.py`): 9 nodes; conditional edges `_post_focus_branch` (fast-path button → straight to `wave`), `_post_gate_branch` (control-gate fired → `persist`), `route_branch` (execute vs boundary), `dispatch_wave` (`Send`-style fan-out to `run_step` or `aggregate`); `run_step → wave` loop; LLM nodes carry `retry_policy`.
- **State** (`executor/state.py`): `ExecutorState` carries tenant/user/role/session/message, `conversation_history`, `focus_entity_id/service`, `plan`, step results, `final_response`, `final_status`, `entry_mode`, `bound_inputs`/`previous_results` (data-flow binding).
- **Step runner** (`executor/step_runner.py`): selects the tool for a step (data-driven from required-param shape), resolves `handler_ref → callable`, builds the handler context, invokes with a per-tool timeout; catches `GraphInterrupt` and re-raises (control flow, not failure); **slot-filling gate** (this session) sits pre-dispatch behind `ONEOPS_ENTITY_ELICITATION_ENABLED`.
- **Interrupt protocol** (`executor/nodes.py`): `interrupt_for_selection / _input / _confirmation / _clarification` wrap LangGraph `interrupt()`, returning typed payloads (`kind`, prompt/question, options/fields). Resume delivers the answer as the `interrupt()` return value.
- **Checkpointer**: `InMemorySaver` (dev) / `AsyncPostgresSaver` (prod); `thread_id = session_id` for resumes (each fresh turn uses `request_id` as thread to avoid inheriting a prior plan).

---

## 5. Persistence flow

```
Conversation memory (session store, session/*):
  cold log (PostgresEventLog / DragonflyEventLog) — durable, full history
  hot window (DragonflyHotWindow / InMemory) — recent N events, rebuilt from cold on miss
  write: append_turn_events(user, assistant) — turn_index = live recent() count, user-dedup
  read:  recent() → hot window (or cold rebuild), trimmed by token budget at load_session

Checkpointer (LangGraph): per-super-step StateSnapshot keyed by thread_id (Postgres in prod)
  → enables interrupt/resume and (potentially) audit time-travel

Business data (itsm.*): incident/request/problem/change/asset/cmdb_ci, kb_knowledge,
  catalog_item, request_item (RITM), task, approval, approval_policy, group_role_map, sys_user
  accessed via per-UC stores (use_cases/_shared/ticket_store.py PostgresTicketStore) AND
  ~10 raw asyncpg call sites in UC handlers (no single DAL)

Embeddings (ai.*): ai.embeddings_<service> with chunk_type discriminator; refreshed by
  AFTER INSERT OR UPDATE triggers → pgmq queue → per-service worker process
```

---

## 6. Approval flow (UC-8, fail-closed)

```
create_service_request (after fulfil §3, before dispatch §4):
  if approval_enabled():                     # flag UC08_APPROVAL_ENABLED
    _apply_approval_gate(tenant, requester, catalog_id, ritm_id, request_id):
      item ← itsm.catalog_item (category, owner_group)
      decision ← resolve_approvers(item, requester, conn):
          load_policies()  → itsm.approval_policy (the MATRIX, config-as-code: JSON→table)
          match_policy()   → first-match by priority on attributes (category/owner_group)
          resolve by approver_type:
            manager_of_requester → sys_user.manager_id (active)
            owning_group         → group_role_map → role/department → sys_user members
            service_desk         → GRP-SERVICE-DESK members  (FAIL-SAFE fallback)
      required=False → return None (dispatch/fulfil now — self-service)
      required & unresolved → HOLD: set approval_state=requested, stamp request pending_approval
                              (NEVER auto-approve — §2.7)
      required & resolved → PARK (one transaction):
            insert itsm.approval row per approver (state=pending)
            set request_item.approval_state=requested
            set request.status=pending_approval, stage=approval   ← requester sees via UC-1/TRACK

decide_approval (NON-chat: endpoint/portal — "IT team handles it on the request"):
  validate actor == requested_from; idempotent on decided rows
  any_one semantics: first approve → withdraw sibling pending rows, transition RITM,
                     stamp request approved/fulfillment, should_dispatch=True (release)
  reject → stop
```

**Verified live (2026-06-11):** matrix→park (3 rows)→approve (siblings withdrawn, released). See `docs/verification/uc08-approval-live-verification.md`.

---

## 7. Observability flow

```
OTel: stage-granular spans over the funnel (router.stage0a.decompose … stage4) and the
      executor (executor.step.handler_call, executor.persist); span tree → Tempo;
      metrics → Prometheus (ai.agent.runs.total, ai.postgres.query.duration_ms, cost);
      Grafana dashboards (active agents, success ratio, cost).
Langfuse: per-call LLM I/O (input/output, redacted/content-gated) on handler/gateway spans;
      per-tenant cost.
Logging: structlog, OTel trace_id/span_id processor attaches correlation to every line.
Cache observability: Dragonfly route cache / chat-turn cache / fast-path edge cache + counters.
```

---

## 8. Data boundaries & tenant isolation

| Boundary | Mechanism | Strength |
|---|---|---|
| **HTTP identity** | `_principal_from_headers` → `x-tenant-id`/`x-user-id`/`x-role` (dev; JWT in prod) | Demo-grade; **identity is server-derived, not from body** ✓ |
| **Tenant scope** | Every store query takes `tenant_id`; `WHERE tenant_id=$1` always present (ticket store contract) | Structural where the store is used; **bypassed at the ~10 raw-SQL sites** (must audit each) |
| **RBAC/ABAC** | `authz/rbac.py` (role-permission registry), policy redaction (`field_policy`), `authz_recheck` hook per tool tier | Present; per-tool tier (read step ≠ write perm) |
| **Embeddings** | `ai.embeddings_<service>.tenant_id` SQL pre-filter | Structural |
| **Checkpointer** | `thread_id = session_id` (session is tenant-scoped) | Isolation via thread namespace |
| **Cache keys** | sha256 includes `tenant_id` (chat-turn cache); route cache is tenant-neutral by design (routing identical) | Chat-turn isolated ✓; route cache intentionally shared |
| **LLM gateway** | Single egress; per-tenant cost; policy mandatory per call | Central choke point ✓ |

**The DAL is the intended system-wide data boundary but is DEFERRED** — today data access is fragmented: a `PostgresTicketStore` for UC-1/2, plus raw asyncpg in UC-3/5/8 handlers. This is the single biggest boundary inconsistency.

---

## 9. Hidden coupling, cycles, violations, ownership

### 9.1 Hidden coupling
- **`api/app.py` (2,570 LOC) is a hub** — it wires the gateway, boundary, session store, route cache, checkpointer, UC route modules, the interrupt protocol (two paths), the fast-path, and the static frontend. Most subsystems are reachable only through it; changing startup order risks subtle breakage (observed this session: the slot-filling gateway had to be wired alongside other LLM components at exactly the right lifespan point).
- **Interrupt handling duplicated across two code paths** (raised `GraphInterrupt` vs returned `__interrupt__`) — any interrupt-adjacent change (e.g. persistence) must touch *both* or silently diverge (observed this session: the persistence fix initially patched only one path).
- **Router ↔ executor shared vocabulary** — entity-shaped param names exist in both `router._ENTITY_FIELD_NAMES` and `step_runner._ENTITY_SHAPED_PARAMS` (mirrored sets with a "must match" comment). Drift risk; a single source of truth would remove it.
- **`tools.set_connection_provider`** is module-global mutable state shared by the approval gate and tests — convenient but a hidden singleton.

### 9.2 Cyclic-dependency risks (managed, not realized)
- `executor.nodes → executor.step_runner` (ExecutorNodes uses StepExecutor) while `step_runner → executor.entity_elicitation → router.entity_id / use_cases.ticket_store` — the cycle is avoided only because `entity_elicitation` does **not** import `nodes` (it calls `langgraph.types.interrupt` directly and lazy-imports `get_ticket_store`). This is deliberate but fragile; a stray top-level import re-introduces the cycle.
- `toolrunner` re-exports `make_result` from `step_runner` via a lazy import to avoid a construction-time cycle.

### 9.3 Architecture violations / smells
- **Monolithic `app.py`** vs the thin-`main.py` principle (FastAPI guide §4.2).
- **Raw SQL in UC handlers** vs the DAL boundary (the platform's own stated goal).
- **Mirrored constant sets** (entity-shaped params) vs single-source-of-truth.
- **Pre-existing UC-8 executor test staleness** — the Playbook-3 rewrite renamed task tool_ids; failure-injection/compensation tests assert old names (8 red tests; coverage debt on retry/compensation paths).
- **No deterministic LLM test seam** — agents call the gateway; without a `FakeGateway` the funnel/executor can't be unit-tested without network/cost (OpenAI SDK §2.4).

### 9.4 Unclear ownership boundaries
- **Data access** — split between `_shared/ticket_store` and per-UC raw SQL; no owner for "how a UC reads the DB."
- **Prompt assets** — live in code/JSON across router + UC modules; no prompt registry/owner.
- **Interrupt/HITL semantics** — split between `executor/nodes` (helpers), `step_runner` (gate), and `api/app.py` (capture/resume across two paths); no single "HITL" module owns the protocol end-to-end.
- **Telemetry emission** — spans/metrics are hand-rolled at call sites across router/executor/stores; no single `TelemetryHandler` owner (OTel §3.1).

> Cross-references: routing principles and reliability/observability gaps are evaluated against the reference patterns in `docs/reference_repo_analysis.md`; severities and fixes follow in `docs/production_gap_analysis.md` and `docs/architecture_review.md`.
