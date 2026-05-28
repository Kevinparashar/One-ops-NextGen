# ARCHITECTURE.md — OneOps AI Engine (POC-5-MW → Production)

**Status:** Target architecture. Authoritative for the rebuild.
**Horizon:** 5+ years, multi-tenant SaaS, 1000-use-case scale.
**Supersedes:** `docs/design/routing-layer-architectural-review.md` (Option C+E) —
see "Routing layer" for how the C+E lesson is preserved.

---

## 1. Design tenets (non-negotiable)

1. **Agents are data, not code.** A use case is a registry record. The executor
   interprets it. Swapping LangGraph for another runtime in year 3 must not
   touch a single agent definition. (AgentScript principle.)
2. **Move logic out of the LLM.** Routing, validation, dependency resolution,
   transformation — deterministic code. The LLM understands intent and
   generates language; nothing else. Any "let the LLM decide" needs an ADR.
   (Moveworks principle.)
3. **Per-turn context narrowing.** Never put 1000 agent descriptions in a
   prompt. Retrieve → filter → disambiguate over a handful. (Parlant principle.)
4. **Structured outputs are the contract.** Every LLM call that drives
   downstream behavior returns schema-validated structured output. Free-form
   text only at user-facing surfaces.
5. **Stateless compute, externalized state.** No in-memory state on the request
   path. Every run is checkpointed and resumable.
6. **Tenant isolation by construction.** Cross-tenant access is impossible
   because schemas, keys, and subjects make it impossible — not because a
   check catches it.
7. **AuthZ at every boundary.** Not just ingress. Internal services verify
   caller identity.
8. **Fail loud.** No bare `except`, no swallowed exceptions, no silent
   fallbacks. A degraded path is an explicit, traced, typed decision.

---

## 2. Logical components

```
                        ┌──────────────────┐
                        │  AWS API Gateway │   WAF · throttling · JWT validation
                        └────────┬─────────┘
                                 │ HTTPS
                        ┌────────▼─────────┐
                        │  Ingress Service │   FaaS · validates tenant+session ·
                        │                  │   assigns trace id · idempotency key
                        └────────┬─────────┘
                                 │ NATS (request/reply, tenant-scoped subjects)
        ┌────────────────────────┼────────────────────────┐
        ▼                        ▼                        ▼
  ┌───────────┐          ┌───────────────┐         ┌──────────────┐
  │  AuthZ    │          │   Router      │         │  Session     │
  │ RBAC+ABAC │◀────────▶│  glossary →   │◀───────▶│  Service     │
  │ Service   │          │  retrieve →   │         │ (Dragonfly + │
  │           │          │  filter →     │         │  Postgres)   │
  └───────────┘          │  disambiguate │         └──────────────┘
                         └───────┬───────┘
                                 │ plan DAG
                        ┌────────▼─────────┐
                        │  LangGraph        │  stateful · checkpointed ·
                        │  Executor         │  parallel + sequential
                        └────────┬─────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
       ┌────────────┐    ┌──────────────┐   ┌──────────────┐
       │ Tool        │    │  LLM Gateway │   │  Policy      │
       │ Runners     │    │  (single     │   │  Engine      │
       │ (1000+ FaaS)│    │   egress)    │   │ (data-driven)│
       └─────┬───────┘    └──────┬───────┘   └──────┬───────┘
             │                   │                  │
       ┌─────▼───────┐    ┌──────▼───────┐   ┌──────▼───────┐
       │ Tenant data │    │ LLM providers│   │updated_policy│
       │ (Postgres)  │    │              │   │   _v2.md     │
       └─────────────┘    └──────────────┘   └──────────────┘

   OTel traces/metrics/logs flow from every box. Trace id propagates
   through NATS headers end to end.
```

| Component | Responsibility | Stateless? | Backed by |
|---|---|---|---|
| API Gateway | Ingress, WAF, throttle, JWT validation | n/a | AWS |
| Ingress Service | Tenant+session validation, trace id, idempotency key mint | yes | — |
| AuthZ Service | RBAC role resolution + ABAC attribute evaluation | yes | Dragonfly cache, Postgres cold |
| Router | Intent → agent(s); emits plan DAG | yes | pgvector, glossary, registry |
| Session Service | Conversation history read/write | yes | Dragonfly hot, Postgres log |
| LangGraph Executor | Runs the plan DAG; checkpoints | yes (state externalized) | Postgres checkpointer |
| Tool Runners | Execute one tool; sandboxed, timeout-enforced | yes | per-tool |
| LLM Gateway | Single model egress; quota, cost, fallback, redaction | yes | Dragonfly counters |
| Policy Engine | Evaluate guardrails/tenant policy as data | yes | policy file, Dragonfly cache |

---

## 3. Routing layer — the heart of the system

A query must reach the right agent(s) out of 1000 **without** an LLM ever
seeing 1000 descriptions. Four-stage funnel, three of them deterministic:

```
 user query
     │
     ▼
 ┌────────────────────┐
 │ 1. Glossary norm.   │  domain synonyms → canonical terms. Deterministic.
 │    (no LLM)         │  Tenant glossary overlays the platform base.
 └─────────┬──────────┘
           ▼
 ┌────────────────────┐
 │ 2. Semantic retrieval│ embed query → pgvector kNN over agent capability
 │    (no LLM)         │  embeddings → top-K (K≈10). Deterministic, ~ms.
 └─────────┬──────────┘
           ▼
 ┌────────────────────┐
 │ 3. Condition + ABAC │  for each of the K: evaluate the agent's declarative
 │    filter (no LLM)  │  ACTIVATION CONDITION; drop agents the caller's
 │                     │  tenant/role/attributes cannot invoke. Deterministic.
 └─────────┬──────────┘
           ▼
 ┌────────────────────┐
 │ 4. LLM disambiguation│ small prompt over ONLY the survivors (typically 1-5).
 │    (LLM Gateway)     │ Returns {agent_id, parameters, confidence} as
 │                     │  schema-validated structured output.
 └─────────┬──────────┘
           ▼
   plan DAG (agents + declared dependencies + resolved exclusions)
```

**Why this preserves the C+E lesson.** The superseded C+E review argued
retrieval-primary routing is fragile because principle text is a finite token
surface against an infinite phrasing surface. Stage 3 answers that: the
**activation condition** is the deterministic decision layer. Retrieval (stage
2) only *narrows*; conditions (stage 3) *decide* eligibility; the LLM (stage 4)
only disambiguates among already-eligible survivors. Retrieval is never the
sole signal. If stage 2 misses on novel phrasing, stage 4 still has a small
correct set or the router emits an explicit `no_confident_match` → clarify —
never a silent wrong route.

**Single-candidate fast path.** If stage 3 yields exactly one agent whose
condition holds decisively, stage 4 is skipped — direct route, no LLM call.

**Plan emission.** The router resolves declared **dependencies** (agent B
`depends_on` agent A) into DAG edges and **exclusions** (agent X excludes
agent Y; priority breaks the tie) into a pruned node set. Output is a plan DAG,
schema-validated, handed to the executor.

**Conversational & boundary responder — a platform component, NOT an agent.**
There is no scripted "greeting agent", no predefined social-intent enum, and
no `uc99` in the registry. Out-of-scope handling and conversational replies
are *platform behavior*, not a use case — they do not belong in the
1000-agent catalog. The registry holds **use-case agents only**, every one
with a real `activation_condition`.

The **boundary responder** is a node in the routing layer. It is reached
*structurally*, never by matching:

  * stage-4 disambiguation finds no confident in-scope task agent → boundary
    responder;
  * a turn is purely conversational (no task) → boundary responder;
  * the policy engine flags an out-of-scope or policy-breach turn (prompt
    injection, data-exfiltration attempt, scope violation) → boundary
    responder voices the refusal.

Its replies are **LLM-generated from live context** — the real registered
capabilities, the policy verdict, conversation history — never a template,
never a predefined greeting. The one exception is **high-stakes compliance**
touchpoints (legal / financial / PII): those are served by the policy layer's
**canned responses** (§9) — zero hallucination where it is mandatory.
Ordinary conversation and ordinary refusals stay dynamic. The responder is
built with the router (P5) / graph (P6); it is not a registry record.

---

## 4. Agent & tool model

### 4.1 Agent definition (registry record — data, not code)

```
agent:
  id:               "uc_0427_incident_summary"
  version:          "3"
  owner:            "team-itsm"
  description:      <tight; embedded for stage-2 retrieval>
  activation_condition:                      # Parlant observation, deterministic
      <structured predicate over: intent tokens, entity presence,
       focus state, tenant capability flags>
  tool_refs:        ["tool_get_ticket", "tool_summarize"]   # by id, versioned
  policy_refs:      ["policy_pii_redact", "policy_itsm_scope"]
  abac_tags:        {service: incident, tier: read, audience: [agent, manager]}
  determinism_level: "high" | "medium" | "low"   # AgentScript dial
  hooks:
      before_invocation: ["hook_authz_recheck", "hook_state_validate"]
      after_invocation:  ["hook_output_redact"]
  depends_on:       []                       # other agent ids → DAG edges
  excludes:         [{agent: "uc_0431_...", priority: 10}]
  compound_of:      []                       # if set, this is a compound action
```

- **Determinism dial.** `high` → executor gates every step with hooks, uses
  canned responses at compliance touchpoints, minimal LLM autonomy. `low` →
  the agent reasons more freely. Recorded per agent; the executor respects it.
- **Lifecycle hooks** run in **code**, not prompts: `before_invocation`
  (auth re-check, state validation), `after_invocation` (output redaction,
  transformation). Hook failure is a typed, traced abort — never swallowed.
- **Compound actions.** When N steps always run together, register one
  compound agent. Intermediate payloads stay internal; the executor and the
  LLM context see one clean response. (Moveworks attention-budget discipline.)

### 4.2 Tool model

- Tools are registered **separately**, referenced by id+version. One tool
  serves many agents. An agent never embeds tool code.
- Tools carry their own activation condition — a tool enters the LLM context
  only when its condition holds. No always-on tool list.
- Tool outputs above a size threshold are stored as a **named variable with a
  preview**; the full payload never enters the next prompt.
- Each tool runner is a FaaS handler: sandboxed, timeout-enforced, idempotent.

### 4.3 Versioning

Agents and tools are versioned. Old versions remain runnable until explicitly
retired. A migration bumps references atomically. Rollback = re-point the
reference at the prior version — no redeploy.

---

## 5. Multi-tenancy

- Every record carries `tenant_id`. Every Dragonfly key is tenant-prefixed.
  Every NATS subject embeds tenant where a tenant boundary exists
  (`oneops.<tenant>.uc.<agent>.<op>`).
- **Isolation by construction:** the data-access layer takes `tenant_id` as a
  non-optional parameter; there is no query path that omits it. A missing
  tenant is a typed error at the boundary, not a full-table scan.
- Tenant config (rate limits, allowed agents, model preferences, retention)
  lives in a config service: Dragonfly hot, Postgres cold.
- Per-tenant feature flags gate every risky change.

---

## 6. Conversation state

- **Append-only event log** per session in Postgres. Hot window cached in
  Dragonfly (tenant-scoped key).
- The **codec** (ADR-0001) defines the on-wire and on-disk event shape.
  Schemas are versioned; every consumer handles version N and N−1.
- Retention is **policy-driven** from `updated_policy_v2.md` — no hardcoded TTL.
- Server time only for ordering (NTP-synced); client timestamps are never
  trusted for sequencing.

---

## 7. Observability (OTel)

- Trace id minted at the API Gateway, propagated via NATS headers through
  every service, every LangGraph node, every tool call, every LLM Gateway call.
- **Metrics:** per-agent latency, per-tool error rate, per-tenant cost,
  router top-K hit-rate, LLM token spend per tenant per model, idempotency
  hit rate, circuit-breaker state.
- **Logs:** structured JSON, correlated by trace id, **redacted** per the
  policy file before egress (PII classified in schemas — see §9).
- The existing `docs/observability/architecture_map.md` span/metric inventory
  is the baseline; the rebuild extends it to the new service boundaries.

---

## 8. Resilience

- Every external call: timeout + retry-with-jitter + circuit breaker.
- Every LangGraph run: checkpointed (ADR-0004) → resumable after a crash.
- NATS consumers idempotent — re-delivery must not double-execute a side
  effect. Idempotency keys in Dragonfly, minted at ingress.
- Backpressure surfaces as **429 at the gateway**, never as a downstream queue
  collapse.
- DR: RPO/RTO targets per ADR; backups are verified by restore drills
  (an untested backup does not exist).

---

## 9. Security & policy

- AuthZ on **every** boundary. Internal calls carry a signed service JWT;
  receivers verify before acting.
- `updated_policy_v2.md` is loaded into the **Policy Engine** (ADR-0003) as
  data. Agents and tools query it at runtime. A policy change deploys without
  a code change.
- **Canned responses** for compliance/legal/financial/PII touchpoints: the
  agent draft is replaced with the closest pre-approved template. Zero
  hallucination at high-stakes surfaces. Lives in the policy layer.
- **PII** classified per-field in schemas; redacted before logs, traces, and
  LLM Gateway prompts.
- Secrets via AWS Secrets Manager / Vault — never in container env files.
- **Prompt-injection mitigation:** tool inputs schema-validated; LLM outputs
  that drive tool calls pass a structured-output validator — never `eval`/`exec`.

---

## 10. Critical-flow sequence diagrams

### Flow A — Query → plan → parallel execute (the happy path)

```
User → API GW:        POST /chat  {jwt, tenant, session, message}
API GW:               validate JWT · WAF · throttle · mint trace_id
API GW → Ingress:     forward (NATS oneops.<tenant>.ingress)
Ingress:              validate tenant+session · mint idempotency_key
Ingress → AuthZ:      resolve(role, abac attrs)            [cache hit ~µs]
Ingress → Session:    load conversation hot window
Ingress → Router:     route(message, focus, abac_ctx)
  Router stage 1:     glossary normalize        (deterministic)
  Router stage 2:     pgvector kNN → top-10     (deterministic)
  Router stage 3:     condition + ABAC filter → 3 survivors (deterministic)
  Router stage 4:     LLM Gateway disambiguation → structured plan
  Router:             resolve deps+exclusions → plan DAG  (agents A, B independent)
Router → Executor:    run(plan DAG)
  Executor:           checkpoint(state_0)
  Executor:           A and B have no edge → Send fan-out, run in PARALLEL
    node A:           before_hooks → tool runner(s) → LLM Gateway → after_hooks
    node B:           before_hooks → tool runner(s) → LLM Gateway → after_hooks
  Executor:           reducer merges A,B results → checkpoint(state_1)
  Executor:           aggregate → final response
Executor → Ingress → API GW → User
   (OTel span emitted at every arrow; trace_id constant throughout)
```

### Flow B — Dependent / sequential chain with a determinism-high agent

```
Router plan DAG:  C → D   (D depends_on C; D.determinism_level = high)
Executor:         wave 1 = {C}
  node C:         before_hooks → tools → LLM → after_hooks → result_C
Executor:         checkpoint · inject result_C.<field> into D's parameters
Executor:         evaluate D — determinism=high:
                    before_hook hook_authz_recheck   → pass
                    before_hook hook_state_validate  → pass
                    if D touches a compliance surface → canned response,
                       LLM draft discarded
  node D:         tools → (LLM or canned) → after_hook hook_output_redact
Executor:         aggregate(result_C, result_D) → response
  (a hook failure here = typed abort, traced, surfaced — never swallowed)
```

### Flow C — Failure and resume

```
Executor running plan {E, F}; node E mid-LLM-call.
LLM Gateway:      upstream 5xx → retry w/ jitter ×3 → circuit breaker OPEN
Executor:         node E raises typed LLMUpstreamError
Executor:         checkpoint already persisted at state_1 (before E)
                  RetryPolicy on the node: 3 attempts exhausted
Executor:         mark E status=failed(error_class=transient); F unaffected
Executor:         partial-result aggregation → response names E as failed,
                  with an actionable retry window
--- process crash before response? ---
On restart:       Executor loads checkpoint state_1 by thread_id
                  re-runs from the last good wave — F not re-executed if it
                  already committed (idempotency key in Dragonfly)
```

### Flow D — Multi-tenant isolation on a cache/route path

```
Tenant T1 query and Tenant T2 query arrive concurrently.
Ingress:        each request carries tenant_id from the validated JWT,
                NOT from the message body.
Router:         pgvector query filtered by tenant-allowed agent set;
                Dragonfly embedding cache key = sha(T<id>:model:qtext)
Executor:       NATS subject oneops.<tenant>.uc.<agent> — T1 and T2
                never share a subject.
Session:        key session:<tenant>:<session_id> — disjoint by construction.
   No code path can read T2's data while serving T1: tenant_id is a
   required parameter on every repository method; there is no overload
   without it.
```

---

## 11. What the LLM is and is NOT allowed to do

| Allowed (LLM) | Not allowed (must be deterministic code) |
|---|---|
| Understand user intent | Pick an agent from 1000 (retrieval + conditions do this) |
| Disambiguate among ≤5 pre-filtered survivors | Resolve dependencies / build the DAG |
| Generate user-facing language | Validate tool inputs / transform outputs |
| Extract parameters into a schema | Decide tenant/role eligibility (ABAC) |
| | Decide retry / timeout / circuit-breaker behavior |
| | Anything at a compliance touchpoint (canned response) |

Any future feature proposing "let the LLM decide" outside the left column
requires an ADR justifying why it cannot be deterministic.

---

## 12. Open items resolved by ADRs

| Decision | ADR |
|---|---|
| Wire format — protobuf vs msgpack | ADR-0001 |
| Vector store — pgvector vs Qdrant | ADR-0002 |
| Policy engine — embedded data-driven vs OPA | ADR-0003 |
| LangGraph checkpoint store | ADR-0004 |
| NATS topology — single vs cluster, JetStream usage | ADR-0005 |

See `DECISIONS/`.
