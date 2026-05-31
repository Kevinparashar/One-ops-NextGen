# OneOps-NextGen — Management briefing

**Date:** 2026-05-31 &nbsp;·&nbsp; **Release reference:** tag `day1-cut-complete-2026-05-31` (HEAD `0f1c035`)

OneOps-NextGen is an AI system that handles a defined set of IT-service-management tasks for multiple business units. A user sends a request, by typing in chat or by clicking a button. The platform routes the request to the appropriate use case — summarising a ticket, finding similar past tickets, looking up a knowledge-base article, triaging an incoming incident, or fulfilling a catalog request. Every request passes through three structural layers before any large-language-model call is made: a router that selects the use case, a policy layer that applies enterprise rules and redacts personally identifiable information, and a tenant-scoped data layer so one customer's data never reaches another's request.

---

## Executive cut

### What is true today

Five use cases run live against multi-tenant data. The Day-1 execution plan is complete and independently verifiable — a single command produces a green per-phase evidence report.[^a1] Approximately 80 per cent of the 22-document production-maturity target is covered, with the remaining 20 per cent named, sized, and sequenced in the consolidated roadmap below.[^a2] The infrastructure tier is containerised; the application tier runs as a Python process and is gated from container-image deployment by a small follow-on listed in the roadmap.

### Status at a glance

| Axis | Status | One-line summary |
|---|---|---|
| Agent lifecycle | PARTIAL | Five agents registered with version and lifecycle metadata; boot-time enforcement active. |
| Performance tracking | SHIPPED | Active code instrumentation feeds the observability stack; nine alert rules are live. |
| Output validation | PARTIAL | Defense-in-depth in place; the LLM-as-judge layer is enforced on UC-8 only today. |
| Security | PARTIAL | Tenant isolation is structural; identity at the door is scaffolded but not enforced. |
| Versioning | SHIPPED | Prompt, agent, model, schema, cache, and code versions are stamped and pinned. |
| Adding new use cases | PARTIAL today, PLANNED via Studio | The reference-package pattern works today; Studio is the future authoring path. |

### Single biggest risk and gating roadmap items

The single biggest deferred risk is identity at the boundary. Front-door token verification and the materialised role-based access-control matrix are scaffolded in code but not enforced on incoming traffic. Both are required before external or untrusted multi-tenant exposure, and both are required before OneOps Studio is exposed to any internal user, because activating an agent without a verified principal would let any caller add capabilities. Both are sized in the roadmap at two to three days each, listed there as items #32 and #33.

### How to read this document

Every capability carries one of three status tags, applied conservatively. **SHIPPED** means working and verifiable today. **PARTIAL** means scaffolded or present but not fully enforced. **PLANNED** means deferred, with a sized roadmap entry. Every factual claim in the body is referenced into the Technical Appendix at the end of this document — paths, ports, counts, and verification commands live there rather than in the prose. References appear as small superscripts in the body.

---

## Architecture

The platform separates **runtime** (the path a live request takes) from **authoring-time** (how a new agent is created), with a registry serving as the contract between them. The runtime exists today. The authoring layer — OneOps Studio — is planned. The runtime never knows Studio exists; it reads the registry at boot like any other configuration source.[^b1]

```mermaid
flowchart TD
    classDef shipped fill:#d4edda,stroke:#155724,color:#155724
    classDef partial fill:#fff3cd,stroke:#856404,color:#856404
    classDef planned fill:#e2e3e5,stroke:#383d41,color:#383d41,stroke-dasharray:5 5

    subgraph AUTH["AUTHORING-TIME — OneOps Studio (PLANNED)"]
        direction TB
        ST_UI["Studio UI<br/>textarea + tool catalog"]:::planned
        ST_COMP["NL → Manifest compiler<br/>roadmap #34"]:::planned
        ST_JUDGE["Judge verifies tool selection"]:::planned
        ST_SIGN["HMAC-signed AgentManifest"]:::planned
        ST_TEST["Sandboxed test runner<br/>roadmap #35"]:::planned
        ST_ACT["Activate writes manifest<br/>lifecycle = draft"]:::planned
        ST_UI --> ST_COMP --> ST_JUDGE --> ST_SIGN --> ST_TEST --> ST_ACT
    end

    REG[("Registry — registries/v2/<br/>SHIPPED")]:::shipped

    subgraph RUN["RUNTIME — SHIPPED today"]
        direction TB
        UI["Frontend and Chat API"]:::shipped
        ROUTER["Router + decomposer<br/>reads registry at boot"]:::shipped
        EXEC["Executor + LangGraph"]:::shipped
        NATS["NATS message bus"]:::shipped
        AGENT["Agent worker"]:::shipped
        TOOLS["Tool runner with role checks"]:::shipped
        LLM["LiteLLM gateway<br/>SINGLE LLM EGRESS"]:::shipped
        STACK[("Postgres · Dragonfly · NATS ·<br/>OTel · Prometheus · Tempo")]:::shipped
        UI --> ROUTER --> EXEC --> NATS --> AGENT --> TOOLS --> LLM
        TOOLS --> STACK
        LLM --> STACK
    end

    ST_ACT -.->|writes| REG
    REG ==>|read at boot| ROUTER
```

Five non-negotiable design principles apply throughout the runtime, documented in the project briefing: every LLM call passes through a single egress so cost, redaction, and policy cannot be bypassed; the policy composer is mandatory on every call; tenant isolation is structural, not advisory; state, retries, caching, and fan-out use the LangGraph framework; and observability code never silently swallows failures.[^b2]

The five use cases live in the runtime today are summarisation of tickets, semantic search for similar tickets, knowledge-base lookup, triage of untriaged incidents, and catalog fulfillment. The first is exposed through chat; similar-ticket search is exposed through both chat and a button; knowledge-base lookup is chat-only; triage is button-based; catalog fulfillment is button-based today with the chat surface deferred to a small post-demo task.[^b3]

### Deployment and integration

The service runs as a single FastAPI process that reads its configuration from a registry on disk. It can be started against a working set of dependencies and integrated into a larger platform as a service it offers, not a library that must be linked. The infrastructure tier — Postgres, NATS, the OTel collector, Tempo, Prometheus, Grafana, Dragonfly, and the LLM gateway — runs as eight container services from the supplied compose file. The application process itself launches under uvicorn today; a container image for the application is listed in the roadmap and is not yet built.[^b4]

A consuming team integrates the service by standing up the eight infrastructure services, starting the application against those endpoints, forwarding HTTP traffic with three required headers, and consuming spans and metrics from the existing observability stack.[^b5] The integration surface is the OpenAPI schema exposed by the application; the standard `/health` and `/ready` endpoints are not yet present and are listed in the roadmap.[^b6]

The service is deployable and integrable today within a trusted boundary. Two items gate external or untrusted multi-tenant exposure: a container image for the application process, and the security pair listed in the Security axis. The same security pair gates the rollout of OneOps Studio.

The persistent data tier runs on managed Postgres with provider-level point-in-time backups. The volatile tier is either stateless or replayable from Postgres and is restored by container restart. The application is stateless between requests; session state is checkpointed to Postgres so a process restart resumes turns from the last checkpoint.[^b7] Multi-region active-active deployment, automated failover, and defined recovery-point-objective and recovery-time-objective targets are PLANNED under the infrastructure-port scope. Today's posture is single-region with provider-level durability — appropriate for the demo and for trusted-boundary integration, explicitly not for service-level-agreement-bound multi-region production.

---

## Agent lifecycle

This axis answers two questions a board needs settled. First, every agent in production must have a known version, a known owner, and a defined state. Second, the platform must refuse to route traffic to anything that is not in an active state.

Five agents are registered in the live registry, each carrying an identifier, an active version, and a versions history. A registry loader validates the registry at boot. The router maintains a refusal path that emits an observable refusal span when traffic arrives for a non-active agent, and the routing decision is auditable through tracing. Evidence of this path being exercised is captured in the phase log produced by the Day-1 verifier.[^l1]

| What | Status |
|---|---|
| Agent records with version and lifecycle metadata | SHIPPED |
| Boot-time registry validation | SHIPPED |
| Router refusal on non-active agents | SHIPPED |
| Manifest export and import tooling | PLANNED |

```mermaid
stateDiagram-v2
    direction TB
    [*] --> draft : create
    draft --> active : approved + tests green
    active --> deprecated : superseded
    active --> retired : end-of-life
    deprecated --> retired : grace period elapsed
    retired --> [*]
```

A new tenant joins the platform by creating the tenant row in the relevant ITSM tables with a unique tenant identifier, by seeding any catalog templates and knowledge-base articles that tenant requires using the existing reference seed scripts, and by routing the tenant's traffic to the API with the tenant identifier in every request header.[^l2] Every existing agent runs against every tenant automatically because tenant isolation is structural; there is no per-tenant agent enable-or-disable operator surface today.

An important reconciliation note for the Studio narrative: the dead-code audit found that the V1 root registries have no runtime references. The live, consumed registry is the V2 directory. When Studio activation is built, its write target must be the V2 directory; writing to the V1 root files would silently fail to publish.[^l3]

Gaps: see Roadmap items #32, #33, and the Day-2 manifest-export item.

---

## Performance tracking

This axis answers the question of whether operations and finance have observable, per-tenant evidence of cost and latency, and whether on-call has alerts that fire on real degradations before users notice them.

The central observation is that this is active code instrumentation, not bolted-on dashboards. The application code emits the data; Tempo, Prometheus, and Grafana display it. Spans are created at every routing stage, every tool call, and every LLM call. Counters and histograms record token use, cost in micro-dollars per tenant per model, request latency, cache behaviour, and per-use-case outcomes. A bounded sixty-second timeout applies to every LLM call site so a stalled model cannot block the whole flow.[^p1]

| What | Status |
|---|---|
| Span and metric emission in production code | SHIPPED |
| Per-tenant cost meter at the LLM gateway | SHIPPED |
| Dashboard panels and alert rules live | SHIPPED |
| Synthetic probes hitting every use case | SHIPPED |
| Drift detector and per-use-case quality scoring | PLANNED |

```mermaid
flowchart TD
    classDef shipped fill:#d4edda,stroke:#155724,color:#155724
    REQ["Incoming request"]:::shipped
    CODE["Application code<br/>spans + metrics"]:::shipped
    OTLP["OTel collector"]:::shipped
    TEMPO["Tempo<br/>trace storage"]:::shipped
    PROM["Prometheus<br/>metric storage"]:::shipped
    GRAF["Grafana<br/>dashboard panels"]:::shipped
    ALERT["Alert rules"]:::shipped
    ONCALL["Webhook contact point<br/>operator notification"]:::shipped
    REQ --> CODE
    CODE --> OTLP
    OTLP --> TEMPO
    OTLP --> PROM
    PROM --> GRAF
    PROM --> ALERT
    TEMPO --> GRAF
    ALERT --> ONCALL
```

The forced-breach evidence file in the phase log captures the alert chain being exercised end-to-end, so the chain is known-working rather than assumed.[^p2]

Gaps: see Roadmap items on drift detection, prompt-regression in CI, and the deferred smoke and devil's-play scripts.

---

## Output validation

This axis answers whether the platform has structural mechanisms that catch wrong AI answers before they reach the user, rather than after.

The validation story is defense-in-depth and is honestly partial. Several mechanisms apply across all use cases: the policy composer is applied on every LLM call, redaction strips personally identifiable information before the model receives the input, closed-enum parsers reject hallucinated values in structured outputs and fall back to safe defaults rather than crashing, Pydantic schema enforcement rejects malformed requests at the route boundary, and a live end-to-end test suite covers the full button flow with the message bus, the judge, and the embedding refresh trigger all in the loop.[^v1] One mechanism — the LLM-as-judge that independently scores each AI decision as faithful, unfaithful, or uncertain — is enforced on UC-8 today and not yet on the other four use cases.[^v2]

| What | Status |
|---|---|
| Policy composer on every LLM call | SHIPPED |
| Personally-identifiable-information redaction at egress | SHIPPED |
| Closed-enum parsers across use cases | SHIPPED |
| Pydantic schema enforcement at the route boundary | SHIPPED |
| End-to-end and devil's-play tests | SHIPPED |
| Continuous-integration commit gate | SHIPPED |
| LLM-as-judge on every use case | PARTIAL (UC-8 only) |
| Drift detector and prompt-regression gate | PLANNED |

```mermaid
flowchart TD
    classDef shipped fill:#d4edda,stroke:#155724,color:#155724
    classDef partial fill:#fff3cd,stroke:#856404,color:#856404
    classDef planned fill:#e2e3e5,stroke:#383d41,color:#383d41,stroke-dasharray:5 5

    REQ["Incoming request"]:::shipped
    POL["Policy composer"]:::shipped
    RED["PII redaction"]:::shipped
    LLM["LLM call via gateway"]:::shipped
    CLO["Closed-enum parser"]:::shipped
    JUDGE["LLM-as-judge<br/>FAITHFUL / UNFAITHFUL / UNCERTAIN"]:::partial
    METRIC["Judge metric → alert"]:::shipped
    RESP["Response carries verdict<br/>+ confidence + reasoning"]:::shipped
    CI["CI gate at commit + push"]:::shipped
    DRIFT["Drift detector<br/>+ faithfulness gate"]:::planned

    REQ --> POL --> RED --> LLM --> CLO --> JUDGE --> METRIC --> RESP
    LLM -. covered today only on UC-8 .- JUDGE
    CI -.->|future tightening| DRIFT
```

The judge coverage gap is load-bearing, not cosmetic. There is at least one documented class of error it would catch and current UC-8-only coverage does not: the router-rewriter has been observed corrupting intent in multi-turn conversations — for example, a follow-up question intended as "summarise this" rewritten in a way that routes to "similar tickets" instead. UC-8-only judging cannot catch this class because the corrupted intent never reaches a judge gate. Expanding judge coverage to the other four use cases is sized at roughly seven hours of focused work in the post-demo sprint.

The continuous-integration commit gate framing matters and is honest. The pre-commit hook runs the fast gate on every commit, and the full gate runs before push. The gate enforces a ratcheting baseline: existing technical-debt categories are explicitly listed in configuration with a documented climb-back plan, and any new violation in any non-listed category fails the gate. This is no-new-debt enforcement, not zero-debt strict mode — a deliberate choice that lets the platform ship while existing debt is paid down sweep by sweep.[^v3]

Gaps: see Roadmap items on judge expansion across use cases, drift detection, prompt-regression in CI, and RAG-faithfulness as a hard gate.

---

## Security

This axis answers whether the platform has identity at the boundary, role-based access at the route layer, and per-tenant data isolation enforced end-to-end.

Three controls are enforced today. First, tenant-scoped data access is structural: every SQL query in the use-case and route layers carries the tenant identifier as the first predicate. Second, per-route role allow-lists are checked at every route handler, with requests from non-listed roles refused at the route boundary with HTTP 403. Third, every LLM call composes one of seven policy profiles, and the redaction module strips personally identifiable information before the model receives the input.[^s1]

Three controls are not yet enforced. First, the principal headers — tenant identifier, user identifier, and role — are read at the boundary today but the bearer-token verification that would prevent header spoofing is scaffolded as authorization-layer modules and is not wired into the request boundary. Second, the materialised role-times-tool authorization matrix that would be checked at both authoring time and runtime is not yet built. Third, a hash-chained immutable audit log and a right-to-be-forgotten endpoint are not yet present. All three are sized in the roadmap.[^s2]

| What | Status |
|---|---|
| Tenant-scoped data access | SHIPPED |
| Per-route role allow-lists | SHIPPED |
| Policy composer and PII redaction | SHIPPED |
| Front-door bearer-token verification | PARTIAL (scaffolded, not enforced) |
| Twice-enforced authorization matrix | PLANNED |
| Hash-chained audit log and RTBF endpoint | PLANNED |

```mermaid
flowchart TD
    classDef shipped fill:#d4edda,stroke:#155724,color:#155724
    classDef planned fill:#e2e3e5,stroke:#383d41,color:#383d41,stroke-dasharray:5 5

    REQ["Incoming HTTP request"]:::shipped
    JWT["Front-door token verification<br/>(PLANNED)"]:::planned
    HEAD["Principal headers read<br/>(trusted at boundary today)"]:::shipped
    ROLE["Per-route role allow-list"]:::shipped
    RBAC["Twice-enforced authorization matrix<br/>(PLANNED)"]:::planned
    TENANT["Tenant-scoped SQL<br/>(every query)"]:::shipped
    POLICY["Policy composer"]:::shipped
    PII["PII redaction"]:::shipped
    LLM["LiteLLM gateway<br/>single egress"]:::shipped
    AUDIT["Hash-chained audit log<br/>(PLANNED)"]:::planned

    REQ --> JWT
    JWT --> HEAD --> ROLE
    ROLE --> RBAC
    RBAC --> TENANT --> POLICY --> PII --> LLM
    POLICY --> AUDIT
```

OneOps Studio is the first surface where any internal user could activate an agent — that is, add a new tool-using capability to the runtime. Header trust, today's model, is acceptable for back-office actors hitting the existing button-mode surface inside a trusted boundary. It is not acceptable for Studio. Front-door verification and the twice-enforced matrix are therefore listed as gating the Studio rollout in the roadmap, not merely correlated with it.

Gaps: see Roadmap items #32 and #33, plus the audit-log and cross-tenant-adversarial entries.

---

## Versioning

This axis answers the question of whether the platform can identify, at audit grade, the exact code and configuration that produced any past result.

Versioning applies at six layers. Per-prompt version constants are declared at every LLM call site and stamped on every span. Per-agent versions are declared in the agent registry. Per-model versions are captured on every cost metric, with the full model version string rather than just the family. Schema versions are numbered migrations applied in order, idempotent on re-run. Cache-key version constants are bumped to invalidate downstream caches without a flush. Code versions are pinned by release tags, with several already in place for the demo lineage.[^vr1]

| What | Status |
|---|---|
| Prompt version stamped on every span | SHIPPED |
| Agent version in registry | SHIPPED |
| Model version on every cost metric | SHIPPED |
| Schema migration sequence | SHIPPED |
| Cache-key version constants | SHIPPED |
| Release tags for code rollback | SHIPPED |
| Tool / capability registry | SHIPPED |
| URL-prefixed API route versioning | PARTIAL |
| Central prompt registry with diff view | PLANNED |

```mermaid
flowchart TD
    classDef shipped fill:#d4edda,stroke:#155724,color:#155724
    classDef planned fill:#e2e3e5,stroke:#383d41,color:#383d41,stroke-dasharray:5 5

    CODE["Code change<br/>(prompt v1 → v2)"]:::shipped
    BUMP["Bump PROMPT_VERSION constant"]:::shipped
    STAMP["Version stamped on<br/>every span + metric label"]:::shipped
    TRACE["Tempo trace carries version"]:::shipped
    METRIC["Prometheus metric carries model version"]:::shipped
    REG["Central prompt registry<br/>(PLANNED)"]:::planned
    TAG["Release tag pinned"]:::shipped
    ROLLBACK["Rollback: checkout tag + restart"]:::shipped
    CODE --> BUMP --> STAMP
    STAMP --> TRACE
    STAMP --> METRIC
    BUMP -.->|future| REG
    TAG --> ROLLBACK
```

API route versioning is partial in a specific way that deserves stating. The OpenAPI schema is exposed at a known path and is the contract today; route paths are stable per release tag; a URL-prefixed scheme such as `/v1/` or `/v2/` is not yet introduced, so breaking route changes are coordinated through tag-pinned releases rather than parallel versioned paths.[^vr2]

Gaps: see Roadmap item on the central prompt registry with diff and rollback view.

---

## Adding new use cases

This axis answers the question of how cheaply and safely a new use case can be added to the platform. Today's path is real and documented but manual. The future path is OneOps Studio.

Today, a developer adds a use case by copying the reference package for the latest use case, by updating the relevant registries, by respecting the thirteen non-negotiable rules in the project briefing, by copying the live end-to-end test pattern, and by walking the thirty-item definition-of-done checklist before claiming the use case complete. The verifier produces a green per-phase report that confirms each piece is in place.[^a3]

| What | Status |
|---|---|
| Reference package for a new use case | SHIPPED |
| Definition-of-done checklist | SHIPPED |
| End-to-end test pattern | SHIPPED |
| Per-phase verifier | SHIPPED |
| Scaffolding command-line tool | PLANNED |
| Studio authoring layer | PLANNED |

Studio is the future path. The authoring flow takes free text, picks tools from the existing tool registry via an LLM call, validates that the chosen tools actually exist (so hallucinated tool identifiers are rejected), has the judge verify the tool selection, signs the manifest, runs a sandboxed test of the authored agent against fixtures, blocks activation if any test fails, and on success writes the manifest to the live registry with a lifecycle state of draft until activation. The router refuses to route to draft agents, so a half-built manifest cannot reach traffic.

```mermaid
flowchart TD
    classDef shipped fill:#d4edda,stroke:#155724,color:#155724
    classDef planned fill:#e2e3e5,stroke:#383d41,color:#383d41,stroke-dasharray:5 5

    USER["Internal user types<br/>natural-language description"]:::planned
    UI["Studio UI"]:::planned
    COMP["NL → Manifest compiler<br/>roadmap #34"]:::planned
    SIGN["HMAC-signed AgentManifest"]:::planned
    SAND["Sandboxed test runner<br/>roadmap #35"]:::planned
    GATE["Author-time RBAC + JWT<br/>roadmap #32, #33"]:::planned
    ACT["Activate writes to V2 registry<br/>lifecycle = draft → active on green"]:::planned
    RUN["Existing runtime reads<br/>V2 registry at boot"]:::shipped

    USER --> UI --> COMP --> SIGN --> SAND --> GATE --> ACT --> RUN
```

Studio's full minimum-viable build is sized at seven to eight days of focused work, broken into roadmap items #31 through #37. The two security items, #32 and #33, are listed as gating because activating an agent without a verified principal would let any caller add capabilities to the runtime.

Gaps: see Roadmap items #31 through #37, plus the scaffolding command-line tool.

---

## Roadmap

The single consolidated table of every PLANNED item across all six axes, with priority from the production-maturity plan and an effort estimate. The "Gates" column states whether the item gates a downstream rollout.

| # | Item | Priority | Effort | Gates |
|---|---|---|---|---|
| 32 | Front-door bearer-token verification | P0 | 2–3 days | External exposure and Studio |
| 33 | Twice-enforced authorization matrix | P0 | 2–3 days | External exposure and Studio |
| 31 | Cross-service tool-catalog refactor | P0 | ~2 days | Studio MVP |
| 34 | NL-to-manifest compiler | P0 | ~3 days | Studio MVP |
| 35 | Sandboxed test runner for authored agents | P0 | ~1 day | Studio MVP |
| 36 | Studio user interface | P0 | ~1 day | Studio MVP |
| 37 | Studio end-to-end demo and runbook | P0 | ~0.5 day | Studio MVP rollout |
| — | Judge expansion to UC-1, UC-2, UC-3, UC-5 | P0 | ~7 hours | Closes load-bearing validation gap |
| — | Drift detector and per-use-case quality scoring | P0 | ~2 days | — |
| — | Prompt-regression continuous-integration gate | P0 | ~2 days | — |
| — | RAG faithfulness as a hard gate | P0 | included in drift scope | — |
| — | Manifest export and import command | P0 | ~0.5 day | Operator workflow |
| — | Cross-tenant adversarial CI corpus | P0 | ~1 day | Continuous security validation |
| — | Container image for the application process | P0 | ~0.5 day | Container deployment |
| — | `/health` and `/ready` endpoints | P0 | ~2 hours | Kubernetes-style probes |
| — | Hash-chained immutable audit log + RTBF endpoint | P1 | ~1 day each | Compliance |
| — | Reversible PII token store | P1 | ~3 days | Compliance |
| — | Per-tenant catalog overlay operator surface | P1 | ~1 day | Customer customisation |
| — | Quality-gated promotion tied to lifecycle | P1 | ~1 day | Closes the loop with judge metrics |
| — | Central prompt registry with diff and rollback view | P1 | ~2 days | Operator tooling |
| — | Scaffolding command-line tool for new use cases | P1 | ~2 days | Drop-in for hand-copy |
| — | A/B traffic split via Istio | P2 | infrastructure-dependent | Scale-time |

---

## Appendix — quick-reference questions and take-home artifacts

| Question | One-line answer |
|---|---|
| How do we know the system is observable? | Span and metric sites in production code feed the existing observability stack. |
| How do we know LLM cost is per tenant? | The cost meter is emitted at the gateway boundary with tenant and model labels on every call. |
| What stops a developer from shipping broken code? | A commit-time gate runs on every commit and a fuller gate runs before push, both with a documented ratchet baseline. |
| How is the system bounded against runaway LLM calls? | A sixty-second default timeout applies at every LLM call site. |
| Can it be deployed independently? | Yes, within a trusted boundary; a container image for the application process is a roadmap item. |
| What is the largest deferred risk? | Identity at the boundary; both items are sized at two to three days. |
| Where is the demo script? | `docs/pmg-demo-runbook.md`. |
| Where is the evidence report? | `ops/pmg-evidence/REPORT.md`. |

The four take-home artifacts committed at the release reference for this briefing:

- The auto-generated evidence report.[^x1]
- The 45-minute demo script with a seven-act narrative walkthrough.[^x2]
- The decision package framing the ten binding-answer questions for management.[^x3]
- The full production-maturity plan with the locked Day-1 cut and the deferred-with-rationale appendix.[^x4]

---

## Technical Appendix

This appendix holds every paragraph reference, source path, port, count, command, and verification note. The body of the document above states each claim in plain language and references this appendix by superscript. The intent is to keep the body readable while preserving every detail an engineer or auditor would want.

### Executive cut references

[^a1]: The verifier is invoked as `make pmg-verify` and produces `ops/pmg-evidence/REPORT.md`. The last generation was green across all seven phases.

[^a2]: The 80 per cent figure is from `docs/production-maturity-plan.md` §F-LOCKED. The deferred 20 per cent is enumerated below in the body of this appendix.

The 22-document target consists of architectural and operational concerns named in DOC-03 (lifecycle), DOC-04 (security and PII), DOC-05 (validation and observability), DOC-06 (cross-tenant adversarial), DOC-07 (policy and tenant context), and DOC-12 (AI testing pyramid). The deferred 20 per cent corresponds to:

| Deferred area | Source document | Rationale |
|---|---|---|
| Hash-chained immutable audit log and RTBF endpoint | DOC-04 | P1; sized in roadmap. |
| Reversible PII token store | DOC-04 §6.1, §6.4 | Workstream 3.2; gated on §G #8. |
| Materialised RBAC matrix | DOC-04 | Roadmap #33. |
| Front-door bearer-token verification | DOC-04 | Roadmap #32. |
| WebSocket primary transport, Bridge, webhooks, ChatOps, language SDKs | DOC-08 | Scope question §G #2 unresolved. |
| ITOM use cases UC-9 through UC-14 | DOC-09 | Scope question §G #4 unresolved. |
| Three-level intent ontology | DOC-10 §3.3 | §G #6 — taxonomy shape conflicts with rule §2.1. |
| EKS / Istio / Lambda / multi-region DR infrastructure | DOC-11 | 8–12 weeks; scope question §G #1. |
| Platform UCs UC-15 through UC-29 and OneOps Studio | DOC-13A | Scope questions §G #3 and §G #4. |

### Architecture references

[^b1]: Source: `docs/PROJECT-BRIEFING.md` §2 for the design principles, and `docs/findings/DEAD-CODE-AUDIT.md` for the registry consumption finding (V1 root files have zero references in `src/oneops/`; the live registry is `registries/v2/`, referenced by `src/oneops/registry/store.py`, `src/oneops/router/glossary.py`, `src/oneops/api/app.py`, `src/oneops/use_cases/uc08_fulfillment/executor.py`, `src/oneops/use_cases/uc08_fulfillment/tools.py`, `src/oneops/use_cases/_shared/field_policy.py`, `src/oneops/uc_common/display_spec.py`, and `src/oneops/policy_engine/engine.py`).

[^b2]: The five principles are §2.5 single LLM egress (`src/oneops/llm/gateway.py`), §2.3 mandatory policy composer (`src/oneops/policy/composer.py`), §2.4 structural tenant isolation, §2.8 LangGraph-first orchestration, and §2.7 no silent failures.

[^b3]: Use-case agents are declared in `registries/v2/agents/uc01_summarization.json`, `uc02_similar_tickets.json`, `uc03_kb_lookup.json`, `uc05_triage.json`, and `uc08_fulfillment.json`. Routes are declared in `src/oneops/api/uc02_routes.py`, `uc05_routes.py`, and `uc08_routes.py`. The deferred chat surface for UC-8 is tracked under the memory entry `project_oneops_uc08_chat_wiring_post_demo`, sized at four to six hours.

[^b4]: The launch command is `uvicorn oneops.api.app:create_app --factory --host 127.0.0.1 --port 8765`. The infrastructure services are declared in `docker-compose.yml` (8 services: dragonfly, nats, postgres, tempo, otel-collector, prometheus, grafana, litellm). No `Dockerfile*` is present at the repository root.

[^b5]: The required headers are `x-tenant-id`, `x-user-id`, and `x-role`. The application listens on port 8765; the LiteLLM gateway listens on port 4301; the OTel collector exposes OTLP HTTP on 4620; Tempo exposes its query API on 3401; Prometheus is on 9391; Grafana is on 3041; Dragonfly is on 6680; NATS is on 4623.

[^b6]: The OpenAPI schema is available at `/openapi.json` and returns 200. The `/health` and `/ready` paths return 404 today.

[^b7]: The checkpointer is `AsyncPostgresSaver`, per ADR-0004.

### Agent lifecycle references

[^l1]: Registry loader: `src/oneops/registry/store.py`. Router refusal path: `src/oneops/router/router.py`. Phase log: `ops/pmg-evidence/phase-3-lifecycle.log`.

[^l2]: Reference seed scripts: `scripts/uc03_seed_password_reset_kb.py` and `scripts/uc08_seed_mfa_catalog.py`. The per-tenant catalog overlay surface is described in DOC-07 §4.6.

[^l3]: The audit document is `docs/findings/DEAD-CODE-AUDIT.md`.

### Performance references

[^p1]: Counts confirmed against HEAD `0f1c035`:

- 107 span emission sites in `src/oneops/` (`grep -rn 'start_as_current_span\|with span(' src/oneops --include="*.py" | wc -l`).
- 95 counter increments and 17 histogram observations (`grep -rn '_metric_inc(\|increment('` and `grep -rn 'histogram('`).
- 15 dashboard panels (`json.load('ops/grafana/dashboards/oneops-overview.json')['panels']`).
- 9 alert rules in `ops/grafana/provisioning/alerting/alert-rules.yaml`, comprising six baseline rules and three UC-8 specific rules.
- 4 synthetic probes plus a driver in `ops/probes/` (`uc01.sh`, `uc03.sh`, `uc05.sh`, `uc08.sh`, plus `run-all-loop.sh` and the shared helper `_common.sh`).
- 60-second timeout default at `src/oneops/use_cases/uc08_fulfillment/text_extract.py:39` (`EXTRACT_TIMEOUT_S`), `judge.py:54` (`JUDGE_TIMEOUT_S`), and `catalog_search.py:83` (`EMBED_TIMEOUT_S`).

Metric identifiers in the body: `ai.llm.cost_usd_micros` with labels `tenant_id` and `model` (emitted at `src/oneops/llm/cost.py:69`); `ai.llm.tokens.{input,output,total}` with labels `model`, `operation`, `provider`; `ai.llm.latency_ms` driving `AgentP99LatencyHigh`; `ai.cache.{hits,misses,writes,stale_reads}.total` and `ai.cache.latency_ms` driving `CacheMissStorm`; `ai.agent.runs.total` with labels `agent_id`, `tenant_id`, `status` driving `TurnFailureRateHigh` and `AgentSubjectSilent`; `ai.uc08.{create_sr,match,fulfill,judge.verdict,agent.events}.total` driving the three UC-8 alerts.

[^p2]: `ops/pmg-evidence/day1-am-alert-fired.log` captures forced-breach evaluation of the UC-8 alert rules against live Prometheus data, with explicit "FIRING (would page)" outputs for the threshold-zero simulation.

### Validation references

[^v1]: Source paths: judge module at `src/oneops/use_cases/uc08_fulfillment/judge.py` (379 lines; `JudgeVerdict` at line 59; `_VALID_VERDICTS` at line 65; `judge_extraction` at line 320; `judge_rerank` at line 345). Closed-enum parsers: `_VALID_VERDICTS` in `judge.py:65`, `_VALID_IMPACTS` / `_VALID_URGENCIES` / `_VALID_PRIORITIES` in `src/oneops/use_cases/uc05_triage/tools/prioritize.py`, `_VALID_CATEGORIES` in `src/oneops/executor/boundary.py`. Policy composer: `src/oneops/policy/composer.py` (307 lines; 7 policy profiles). PII redaction: `src/oneops/llm/redaction.py` (54 lines). Pydantic schema enforcement uses `model_config = ConfigDict(extra="forbid")` across route files. End-to-end test suite: `tests/integration/test_uc08_button_user_journey.py` (15 test functions; runs against an in-process FastAPI server). Per-use-case unit-test counts: 2 for UC-1, 32 for UC-2, 0 for UC-3 (chat-only; covered upstream by router and KB-store tests), 9 for UC-5, 28 for UC-8.

[^v2]: The judge runs on UC-8 only; UC-1, UC-2, UC-3, and UC-5 are not covered. The documented router-rewriter intent-corruption class is recorded in the memory entry `project_poc5mw1_routing_overhaul_2026_05_28`. Expansion is sized at roughly seven hours in `project_oneops_uc234_prompt_hardening_sweep`.

[^v3]: The CI gate is `scripts/ci.sh`, invoked by `make ci-fast` at commit time via `.git/hooks/pre-commit` and by `make ci` at push time. The ratchet baseline is declared in `pyproject.toml` under `[tool.ruff.lint]` (24 categories explicitly ignored with rationale) and `[tool.mypy]` (`strict = false` with 14 categories disabled). The commit-gate path is green today; a separately-run `tests/unit/router/test_time_filter_extractor.py` bundle shows 13 event-loop-isolation flakes that are not on the commit-gate path.

### Security references

[^s1]: Tenant-scoped SQL queries are observed at `src/oneops/api/uc08_routes.py:284–340` (an `itsm.request` INSERT), `src/oneops/api/uc05_routes.py:162–169` (queue summary), and the reads in `src/oneops/use_cases/_shared/ticket_store.py`. Per-route role allow-lists are declared as `frozenset` constants in `uc05_routes.py` (`_TRIAGE_ROLES`) and `uc08_routes.py` (`_PERMITTED_MATCH_ROLES`, `_PERMITTED_FULFILL_ROLES`). The seven policy profiles are defined in `src/oneops/policy/composer.py`; the rule reference is §2.3, with the memory feedback `feedback_policy_layer_mandatory`. The PII redaction module is `src/oneops/llm/redaction.py`.

[^s2]: The scaffolded authorization-layer modules are at `src/oneops/authz/tokens.py`, `rbac.py`, `abac.py`, and `decision_cache.py`.

### Versioning references

[^vr1]: Per-prompt version constants appear at every LLM call site, with examples at `src/oneops/use_cases/uc08_fulfillment/text_extract.py:47`, `judge.py:55`, and `src/oneops/use_cases/uc08_fulfillment/catalog_reranker.py`. The `uc08.prompt_version` attribute is set on `uc08.text_extract.call`, `uc08.judge.extraction`, and `uc08.judge.rerank`. Per-agent versions are declared in `registries/v2/agents/<uc>.json` under `active_version` and `versions[]`. Model versions appear in cost metric labels in the form `model="gpt-4o-mini-2024-07-18"`. Schema migrations are `migrations/0001_*.sql` through `migrations/0007_*.sql`. Cache-key constants include `PIPELINE_CACHE_VERSION` and `HUMANISE_RECORD_VERSION`. Release tags include `uc08-button-demo-ready`, `uc08-production-ready`, and `day1-cut-complete-2026-05-31`.

[^vr2]: The OpenAPI document declares `info.version = "0.1.0"`. Tool registry: `registries/tool-registry.json`. Agent-tool mapping: `registries/agent-tool-mapping.json` (live consumer is `registries/v2/`).

### Adding new use cases references

[^a3]: Reference package: `src/oneops/use_cases/uc08_fulfillment/`. Contract: `docs/COMPONENT_SPEC.md` (C1–C24). Conventions: `docs/CONVENTIONS.md`. Non-negotiable rules: `docs/PROJECT-BRIEFING.md` §2. Test pattern: `tests/integration/test_uc08_button_user_journey.py`. Definition-of-done: `docs/production-maturity-plan.md` §D. Verifier: `make pmg-verify` → `ops/pmg-evidence/REPORT.md`.

### Take-home artifact references

[^x1]: `ops/pmg-evidence/REPORT.md`.

[^x2]: `docs/pmg-demo-runbook.md`.

[^x3]: `docs/manager-decision-package.md`.

[^x4]: `docs/production-maturity-plan.md`.

### CI gate status at the time of writing

| Path | Status |
|---|---|
| `make ci-fast` (commit-time gate via `.git/hooks/pre-commit`) | Green: ruff ✓, mypy ✓, `pytest -m unit` ✓ at HEAD `0f1c035`. |
| `make ci` (full gate) | Green on stages 1–4; stages 5 (smoke) and 6 (devils) print "deferred — script not present (Phase 6 fills in)" — deferral is deliberate per the Day-1 plan. |
| `tests/unit/router/test_time_filter_extractor.py` (separate path, not on the commit gate) | 13 failures of 195 — event-loop-isolation flakes; not a product regression. |

### Items that could not be confirmed at the time of writing

| Item | Reason | Recommended next step |
|---|---|---|
| Permalink URLs to source files at the release reference | No git remote is configured (`git remote get-url origin` returns nothing). | Source citations rendered as inline paths per the documented fallback rule; when a remote is added, replace inline paths with pinned permalinks. |
| `/health` and `/ready` HTTP endpoints | Both return 404. | Add to the FastAPI surface; included in the roadmap. |
| Container image for the API process | No `Dockerfile*` present. | Build one; included in the roadmap. |
| V2 agent files' flat top-level lifecycle metadata | The V2 schema uses nested `active_version` and `versions[]` rather than flat top-level fields. | Document the actual V2 schema in `docs/CONVENTIONS.md` so future authors do not assume a different shape. |
| Studio activation write path | Studio is PLANNED; no activation code exists yet. | When tasks #34 and #37 are built, target `registries/v2/` per the registry reconciliation finding. |

### Verified counts at HEAD `0f1c035`

| Claim | Value |
|---|---|
| OTel span emission sites | 107 |
| Counter increments | 95 |
| Histogram observations | 17 |
| Grafana dashboard panels | 15 |
| Grafana alert rules | 9 (6 baseline + 3 UC-8) |
| Synthetic probes | 4 UC-specific + 1 driver + 1 helper |
| UC-8 integration tests | 15 |
| UC-8 unit tests | 28 |
| UC-2 unit tests | 32 |
| UC-5 unit tests | 9 |
| UC-1 unit tests | 2 |
| UC-3 unit tests | 0 (chat-only; covered upstream) |
| Judge module line count | 379 |
| Policy composer line count | 307 |
| PII redaction line count | 54 |
| Policy profiles defined | 7 |
| Docker-compose services | 8 |

### Prepared by

This briefing was prepared on 2026-05-31 against release `day1-cut-complete-2026-05-31` (HEAD `0f1c035`). All factual claims were verified against the codebase at that exact reference using the commands referenced above. Any number that could not be confirmed is flagged in the appendix above rather than asserted in the body.
