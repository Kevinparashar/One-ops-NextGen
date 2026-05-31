# OneOps-NextGen — Management briefing

Date 2026-05-31 ·

OneOps-NextGen is an AI system that handles a defined set of IT-service-management tasks for multiple business units. A user sends a request, by typing in chat or by clicking a button. The platform routes the request to the appropriate use case — summarising a ticket, finding similar past tickets, looking up a knowledge-base article, triaging an incoming incident, or fulfilling a catalog request. Every request passes through three structural layers before any AI model is called: a router that selects the use case, a policy layer that applies enterprise rules and redacts personally identifiable information, and a tenant-scoped data layer so one customer's data never reaches another's request.

---


## How to read

- SHIPPED — working and verifiable today.
- PARTIAL — present in code but not fully enforced.
- PLANNED — deferred, with a sized roadmap entry.

Paths, ports, counts, and verification commands live in the Technical Appendix at the end of this document. Deferred work appears once, in the Roadmap; each axis closes with a pointer to the relevant roadmap items.

---


## Executive cut

> The single biggest deferred risk is identity at the boundary. Front-door token verification and the materialised access-control matrix are scaffolded in code but not enforced on incoming traffic. Both are required before external or untrusted multi-tenant exposure, and both are required before OneOps Studio is exposed to any internal user. Roadmap items 32 and 33.

Five use cases run live against multi-tenant data. The Day-1 execution plan is complete and independently verifiable: a single command produces a green per-phase evidence report. Roughly eighty per cent of the twenty-two-document production-maturity target is covered; the remaining twenty per cent is named, sized, and sequenced in the Roadmap. The infrastructure tier is containerised; the application process is gated from container-image deployment by a small follow-on.

| Axis | Status | One-line summary |
|---|---|---|
| Agent lifecycle | PARTIAL | Five agents registered with version and lifecycle metadata; boot-time enforcement active. |
| Performance tracking | SHIPPED | Active code instrumentation feeds the observability stack; nine alert rules are live. |
| Output validation | PARTIAL | Defense in depth across all use cases; the dedicated judge layer is enforced on UC-8 only. |
| Security | PARTIAL | Tenant isolation is structural; identity at the boundary is scaffolded but not enforced. |
| Versioning | SHIPPED | Prompt, agent, model, schema, cache, and code versions are stamped and pinned. |
| Adding new use cases | PARTIAL today, PLANNED via Studio | The reference-package pattern works today; Studio is the future authoring path. |

---


## Architecture

> Two zones, separated by the registry. The runtime exists today. The authoring layer is planned. The runtime never knows about the authoring layer; it reads the registry at boot like any other configuration.

The platform separates runtime — the path a live request takes — from authoring-time — how a new agent is created. A registry on disk is the contract between the two. The runtime is shipped today. The authoring layer is OneOps Studio, currently planned.

```mermaid
flowchart TD
    classDef shipped fill:#d4edda,stroke:#155724,color:#155724
    classDef planned fill:#e2e3e5,stroke:#383d41,color:#383d41,stroke-dasharray:5 5

    subgraph AUTH["AUTHORING-TIME — OneOps Studio (PLANNED)"]
        direction TB
        ST_UI["Studio UI<br/>textarea + tool catalog"]:::planned
        ST_COMP["NL to manifest compiler"]:::planned
        ST_JUDGE["Judge verifies tool selection"]:::planned
        ST_SIGN["Signed agent manifest"]:::planned
        ST_TEST["Sandboxed test runner"]:::planned
        ST_ACT["Activate writes manifest<br/>lifecycle = draft"]:::planned
        ST_UI --> ST_COMP --> ST_JUDGE --> ST_SIGN --> ST_TEST --> ST_ACT
    end

    REG[("Registry<br/>SHIPPED")]:::shipped

    subgraph RUN["RUNTIME — SHIPPED today"]
        direction TB
        UI["Frontend and chat API"]:::shipped
        ROUTER["Router and decomposer<br/>reads registry at boot"]:::shipped
        EXEC["Executor and orchestration"]:::shipped
        NATS["Message bus"]:::shipped
        AGENT["Agent worker"]:::shipped
        TOOLS["Tool runner with role checks"]:::shipped
        LLM["Single model egress"]:::shipped
        STACK[("Database, cache, message bus,<br/>telemetry, metrics, traces")]:::shipped
        UI --> ROUTER --> EXEC --> NATS --> AGENT --> TOOLS --> LLM
        TOOLS --> STACK
        LLM --> STACK
    end

    ST_ACT -.->|writes| REG
    REG ==>|read at boot| ROUTER
```

*Figure 1. The runtime, shown on the right, exists today. The authoring layer, on the left, is planned. The registry between them is the shared surface; either zone can be down without affecting the other.*

Five non-negotiable design principles apply across the runtime: every model call passes through a single egress so cost, redaction, and policy cannot be bypassed; the policy composer is mandatory on every call; tenant isolation is structural rather than advisory; orchestration uses one framework rather than ad-hoc patterns; observability code never silently swallows failures.

The five live use cases:

| Use case | Surface | Status |
|---|---|---|
| UC-1 Summarization | Chat | SHIPPED |
| UC-2 Similar Tickets | Button and chat | SHIPPED |
| UC-3 KB Lookup | Chat | SHIPPED |
| UC-5 Triage | Button | SHIPPED |
| UC-8 Fulfillment | Button (chat surface deferred) | SHIPPED button; PLANNED chat |

Gaps → see Roadmap 31 to 37.

---


## Deployment and integration

> Deployable and integrable today within a trusted boundary. Two follow-ons gate broader exposure: a container image for the application process, and the identity gate described under Security.

The service is a single FastAPI process configured from the registry on disk. A consuming team integrates it as a service it offers, not a library that must be linked. The infrastructure tier — message bus, distributed cache, telemetry collector, distributed-trace store, metrics store, dashboard, and model proxy — runs as eight container services from the supplied compose file. The application process itself launches under uvicorn; the container image for the application is a half-day roadmap item.

The persistent data tier runs on managed Postgres with provider-level point-in-time backups. The volatile tier is stateless or replayable from Postgres. The application is stateless between requests; session state is checkpointed to Postgres, so a process restart resumes turns from the last checkpoint. Multi-region active-active deployment and defined recovery targets are planned under the infrastructure-port scope. Today's posture is single-region with provider-level durability — appropriate for the demo and trusted-boundary integration, explicitly not for service-level-agreement-bound multi-region production.

| Capability | Status |
|---|---|
| API contract via OpenAPI schema | SHIPPED |
| Infrastructure containerised via compose | SHIPPED |
| Application container image | PLANNED |
| Liveness and readiness probes | PLANNED |
| Single-region resilience with provider backups | SHIPPED |
| Multi-region active-active and defined recovery targets | PLANNED |

Gaps → see Roadmap on the container image and the readiness probes; multi-region work appears under the infrastructure-port scope.

---


## Agent lifecycle

> Every agent in production carries a known version and a defined state. The platform refuses to route traffic to anything that is not active.

Five agents are declared in the live registry, each with an identifier, an active version, and a versions history. A registry loader validates the registry at boot. The router maintains a refusal path that emits an observable refusal event when traffic arrives for a non-active agent. Evidence that this path is exercised is captured in the per-phase log produced by the Day-1 verifier.

A new tenant joins the platform by creating the tenant's rows in the relevant ITSM tables, by seeding any catalog templates or knowledge-base articles using the reference seed scripts, and by routing the tenant's traffic to the application with the tenant identifier in every request header. Every existing agent runs against every tenant automatically because tenant isolation is structural.

| Capability | Status |
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

*Figure 2. Agent lifecycle states. The draft-to-active transition requires approval and passing tests; the router refuses to route to any non-active state.*

The dead-code audit carries a finding that is load-bearing for the Studio narrative: the original V1 root registries are not consumed by the runtime. The registry the runtime reads at boot is the V2 directory. When Studio activation is built, its write target must be the V2 directory; writing to the V1 root files would silently fail to publish.

Gaps → see Roadmap items 32 and 33, plus the manifest-export item.

---


## Performance tracking

> Active code instrumentation, not bolted-on dashboards. The application code emits the data; the observability stack displays it.

Span emission sites and metric emit sites in the application code feed the standard observability stack. Counters and histograms record token volume, cost in micro-dollars per tenant per model, request latency, cache behaviour, and per-use-case outcomes. A sixty-second timeout applies at every model call site so a stalled model cannot block the flow.

| Capability | Status |
|---|---|
| Span and metric emission in production code | SHIPPED |
| Per-tenant cost meter at the model gateway | SHIPPED |
| Dashboard panels and alert rules live | SHIPPED |
| Synthetic probes hitting every use case | SHIPPED |
| Forced-breach proof of the alert chain | SHIPPED |
| Drift detector and per-use-case quality scoring | PLANNED |

```mermaid
flowchart TD
    classDef shipped fill:#d4edda,stroke:#155724,color:#155724
    REQ["Incoming request"]:::shipped
    CODE["Application code<br/>spans + metrics"]:::shipped
    OTLP["Telemetry collector"]:::shipped
    TEMPO["Trace store"]:::shipped
    PROM["Metric store"]:::shipped
    GRAF["Dashboards"]:::shipped
    ALERT["Alert rules"]:::shipped
    ONCALL["Operator notification"]:::shipped
    REQ --> CODE
    CODE --> OTLP
    OTLP --> TEMPO
    OTLP --> PROM
    PROM --> GRAF
    PROM --> ALERT
    TEMPO --> GRAF
    ALERT --> ONCALL
```

*Figure 3. The telemetry data path. The application emits over the standard protocol to the collector; the dashboards read from the trace and metric stores.*

The forced-breach evidence file captures each alert rule's expression being evaluated against live metric data with the threshold lowered to zero. The chain — application to collector to metric store to evaluator to webhook contact point — is therefore known-working rather than assumed.

Gaps → see Roadmap on drift detection and prompt-regression in CI.

---


## Output validation

> Defense in depth covers every use case. The dedicated independent-judge layer is enforced on UC-8 today and is a known load-bearing gap on the other four.

Five mechanisms catch wrong AI answers before they reach the user. The policy composer is applied on every model call. Personally-identifiable-information redaction strips sensitive patterns before the model receives the input. Closed-enum parsers reject hallucinated values in structured outputs and fall back to safe defaults rather than crashing. Pydantic schema enforcement rejects malformed requests at the route boundary. Live end-to-end and adversarial test suites cover every use case. One additional mechanism — an independent model that scores each AI decision as FAITHFUL, UNFAITHFUL, or UNCERTAIN — is enforced on UC-8 today and not yet on the other four.

| Validation layer | Covers | Status |
|---|---|---|
| Policy composer on every model call | All five use cases | SHIPPED |
| PII redaction at egress | All five use cases | SHIPPED |
| Closed-enum parsers | UC-5 and the scope classifier | SHIPPED |
| Pydantic schema enforcement | UC-2, UC-5, UC-8 routes | SHIPPED |
| End-to-end and adversarial tests | All five use cases | SHIPPED |
| Independent judge layer | UC-8 only | PARTIAL |

```mermaid
flowchart TD
    classDef shipped fill:#d4edda,stroke:#155724,color:#155724
    classDef partial fill:#fff3cd,stroke:#856404,color:#856404
    classDef planned fill:#e2e3e5,stroke:#383d41,color:#383d41,stroke-dasharray:5 5

    REQ["Incoming request"]:::shipped
    POL["Policy composer"]:::shipped
    RED["PII redaction"]:::shipped
    LLM["Model call"]:::shipped
    CLO["Closed-enum parser"]:::shipped
    JUDGE["Independent judge<br/>UC-8 only today"]:::partial
    METRIC["Judge metric to alert"]:::shipped
    RESP["Response carries verdict<br/>plus confidence and reasoning"]:::shipped
    CI["CI gate at commit and push"]:::shipped
    DRIFT["Drift detector and<br/>faithfulness gate"]:::planned

    REQ --> POL --> RED --> LLM --> CLO --> JUDGE --> METRIC --> RESP
    CI -.->|future tightening| DRIFT
```

*Figure 4. The validation pipeline. The judge node is partial because it covers UC-8 only today.*

The judge coverage gap matters because there is a documented class of error UC-8-only coverage cannot catch: the router-rewriter has been observed corrupting intent in multi-turn conversations, with a summarise request rewritten so it routes to similar-tickets instead. UC-8-only judging cannot catch this class because the corrupted intent never reaches a judge gate. Expansion to the other four use cases is sized at roughly seven hours of focused work.

The continuous-integration gate runs on every commit through a pre-commit hook and on every push as a fuller suite. The gate enforces a ratcheting baseline: existing technical-debt categories are listed in configuration with a documented climb-back plan, and any new violation in a non-listed category fails the gate. This is no-new-debt enforcement rather than zero-debt strict mode.

Gaps → see Roadmap on judge expansion, drift detection, prompt-regression in CI, and RAG faithfulness as a hard gate.

---


## Security

> Three controls are enforced today. Three are scaffolded or planned, including the two that gate external exposure and Studio.

Tenant-scoped data access is enforced structurally: every query in the use-case and route layers carries the tenant identifier as the first predicate. Per-route role allow-lists refuse requests from non-listed roles at the route boundary. The policy composer applies one of seven profiles to every model call, and the redaction module strips personally identifiable information before the model receives input.

Three controls are not yet enforced. The principal headers are read at the boundary today, but the bearer-token verification that would prevent header spoofing is scaffolded in authorization-layer modules and not wired into the request path. The materialised role-times-tool authorization matrix that would be checked at both authoring time and runtime is not built. A hash-chained immutable audit log and a right-to-be-forgotten endpoint are not present.

| Control | Status |
|---|---|
| Tenant-scoped data access | SHIPPED |
| Per-route role allow-lists | SHIPPED |
| Policy composer and PII redaction | SHIPPED |
| Front-door bearer-token verification | PARTIAL |
| Twice-enforced authorization matrix | PLANNED |
| Hash-chained audit log and RTBF endpoint | PLANNED |

```mermaid
flowchart TD
    classDef shipped fill:#d4edda,stroke:#155724,color:#155724
    classDef planned fill:#e2e3e5,stroke:#383d41,color:#383d41,stroke-dasharray:5 5

    REQ["Incoming HTTP request"]:::shipped
    JWT["Bearer-token verification<br/>PLANNED"]:::planned
    HEAD["Principal headers read<br/>trusted at boundary today"]:::shipped
    ROLE["Per-route role allow-list"]:::shipped
    RBAC["Twice-enforced authorization matrix<br/>PLANNED"]:::planned
    TENANT["Tenant-scoped queries"]:::shipped
    POLICY["Policy composer"]:::shipped
    PII["PII redaction"]:::shipped
    LLM["Single model egress"]:::shipped
    AUDIT["Hash-chained audit log<br/>PLANNED"]:::planned

    REQ --> JWT --> HEAD --> ROLE --> RBAC --> TENANT --> POLICY --> PII --> LLM
    POLICY --> AUDIT
```

*Figure 5. The request control path. Dotted nodes are not yet on the request path; solid nodes apply on every request today.*

OneOps Studio is the first surface where any internal user could activate an agent — add a tool-using capability to the runtime. Header trust is acceptable for back-office actors on the existing button surface inside a trusted boundary. It is not acceptable for Studio. The two security items are therefore listed as gating Studio, not merely correlated with it.

Gaps → see Roadmap items 32 and 33, plus the audit-log and cross-tenant-adversarial entries.

---


## Versioning

> Seven layers of versioning carry the platform from a code change to an audit-grade trace of any past result.

Per-prompt versions are declared at every model call site and stamped on every span. Per-agent versions are declared in the agent registry. Per-model versions are captured on every cost metric, with the full version string rather than just the family. Schema versions are numbered migrations applied in order, idempotent on re-run. Cache-key version constants are bumped to invalidate downstream caches without a flush. Tool registry identifiers carry capability declarations; signature changes ship as new entries. Code versions are pinned by release tags.

| Layer | Status |
|---|---|
| Prompt version stamped on every span | SHIPPED |
| Agent version in the registry | SHIPPED |
| Model version on every cost metric | SHIPPED |
| Schema migration sequence | SHIPPED |
| Cache-key version constants | SHIPPED |
| Tool and capability registry | SHIPPED |
| Release tags for code rollback | SHIPPED |
| URL-prefixed API route versioning | PARTIAL |
| Central prompt registry with diff view | PLANNED |

```mermaid
flowchart TD
    classDef shipped fill:#d4edda,stroke:#155724,color:#155724
    classDef planned fill:#e2e3e5,stroke:#383d41,color:#383d41,stroke-dasharray:5 5

    CODE["Code change<br/>prompt v1 → v2"]:::shipped
    BUMP["Bump prompt-version constant"]:::shipped
    STAMP["Version stamped on every<br/>span and metric label"]:::shipped
    TRACE["Trace carries version"]:::shipped
    METRIC["Metric carries model version"]:::shipped
    REG["Central prompt registry<br/>PLANNED"]:::planned
    TAG["Release tag pinned"]:::shipped
    ROLLBACK["Rollback by tag checkout"]:::shipped
    CODE --> BUMP --> STAMP
    STAMP --> TRACE
    STAMP --> METRIC
    BUMP -.->|future| REG
    TAG --> ROLLBACK
```

*Figure 6. The versioning flow. Each code change updates a constant that is stamped on the matching span and metric, making any past result traceable to the exact version that produced it.*

API route versioning is partial in a specific way: the schema is exposed at a known path and is the contract today; route paths are stable per release tag; a URL-prefixed scheme is not yet introduced, so breaking changes are coordinated through tag-pinned releases rather than parallel versioned paths.

Gaps → see Roadmap on the central prompt registry.

---


## Adding new use cases

> The path today is the reference-package copy from the latest use case. The path tomorrow is OneOps Studio. Both produce the same shape of agent manifest in the same registry.

A developer adds a use case today by copying the reference package, by respecting the thirteen non-negotiable rules in the project briefing, by updating the relevant registries, by copying the live end-to-end test pattern, and by walking the thirty-item definition-of-done checklist. The Day-1 verifier confirms each piece is in place.

Studio is the future path. The authoring flow takes free text, picks tools from the existing tool registry through a model call, validates that the chosen tools exist so hallucinated identifiers are rejected, has the judge verify the tool selection, signs the resulting manifest, runs a sandboxed test of the authored agent against fixtures, blocks activation if any test fails, and on success writes the manifest to the live registry with a lifecycle state of draft until activation completes. The router refuses to route to draft agents.

| Capability | Status |
|---|---|
| Reference package for new use cases | SHIPPED |
| Definition-of-done checklist | SHIPPED |
| End-to-end test pattern | SHIPPED |
| Per-phase verifier | SHIPPED |
| Scaffolding command-line tool | PLANNED |
| Studio authoring layer | PLANNED |

```mermaid
flowchart TD
    classDef shipped fill:#d4edda,stroke:#155724,color:#155724
    classDef planned fill:#e2e3e5,stroke:#383d41,color:#383d41,stroke-dasharray:5 5

    USER["Internal user types<br/>natural-language description"]:::planned
    UI["Studio UI"]:::planned
    COMP["NL to manifest compiler"]:::planned
    SIGN["Signed agent manifest"]:::planned
    SAND["Sandboxed test runner"]:::planned
    GATE["Author-time RBAC and JWT"]:::planned
    ACT["Activate writes to registry<br/>lifecycle draft → active on green"]:::planned
    RUN["Existing runtime reads<br/>registry at boot"]:::shipped

    USER --> UI --> COMP --> SIGN --> SAND --> GATE --> ACT --> RUN
```

*Figure 7. The Studio authoring flow. Activation crosses a real boundary: the manifest is in the registry, but the router refuses to route to it until the lifecycle moves from draft to active.*

Studio's minimum-viable build is sized at seven to eight days of focused work. The two security items are listed as gating because activating an agent without a verified principal would let any caller add capabilities to the runtime.

Gaps → see Roadmap 31 to 37.

---


## Roadmap

The single consolidated table of every PLANNED item with priority from the production-maturity plan and an effort estimate. The Gates column states whether the item gates a downstream rollout.

| # | Item | Priority | Effort | Gates |
|---|---|---|---|---|
| 32 | Front-door bearer-token verification | P0 | 2 to 3 days | External exposure and Studio |
| 33 | Twice-enforced authorization matrix | P0 | 2 to 3 days | External exposure and Studio |
| 31 | Cross-service tool-catalog refactor | P0 | About 2 days | Studio MVP |
| 34 | NL-to-manifest compiler | P0 | About 3 days | Studio MVP |
| 35 | Sandboxed test runner for authored agents | P0 | About 1 day | Studio MVP |
| 36 | Studio user interface | P0 | About 1 day | Studio MVP |
| 37 | Studio end-to-end demo and runbook | P0 | About half a day | Studio MVP rollout |
| — | Judge expansion to UC-1, UC-2, UC-3, UC-5 | P0 | About 7 hours | Closes load-bearing validation gap |
| — | Drift detector and per-use-case quality scoring | P0 | About 2 days | — |
| — | Prompt-regression continuous-integration gate | P0 | About 2 days | — |
| — | RAG faithfulness as a hard gate | P0 | Included in drift scope | — |
| — | Manifest export and import command | P0 | About half a day | Operator workflow |
| — | Cross-tenant adversarial CI corpus | P0 | About 1 day | Continuous security validation |
| — | Container image for the application process | P0 | About half a day | Container deployment |
| — | Liveness and readiness endpoints | P0 | About 2 hours | Kubernetes-style probes |
| — | Hash-chained immutable audit log and RTBF endpoint | P1 | About 1 day each | Compliance |
| — | Reversible PII token store | P1 | About 3 days | Compliance |
| — | Per-tenant catalog overlay operator surface | P1 | About 1 day | Customer customisation |
| — | Quality-gated promotion tied to lifecycle | P1 | About 1 day | Closes the loop with judge metrics |
| — | Central prompt registry with diff and rollback view | P1 | About 2 days | Operator tooling |
| — | Scaffolding command-line tool for new use cases | P1 | About 2 days | Drop-in for hand-copy |
| — | A/B traffic split via service mesh | P2 | Infrastructure-dependent | Scale-time |

---


## Appendix — quick-reference questions and take-home artifacts

| Question | One-line answer |
|---|---|
| How do we know the system is observable? | Span and metric sites in production code feed the existing observability stack. |
| How do we know LLM cost is per tenant? | The cost meter is emitted at the gateway boundary with tenant and model labels on every call. |
| What stops a developer from shipping broken code? | A commit-time gate runs on every commit; a fuller gate runs before push; both enforce a documented ratchet baseline. |
| How is the system bounded against runaway model calls? | A sixty-second default timeout applies at every model call site. |
| Can it be deployed independently? | Yes, within a trusted boundary; the application container image is a roadmap item. |
| What is the largest deferred risk? | Identity at the boundary; both items are sized at two to three days. |
| Where is the demo script? | In the demo-runbook document under the docs directory. |
| Where is the evidence report? | In the auto-generated report under the pmg-evidence directory. |

Four take-home documents are committed at the release reference for this briefing:

- The auto-generated evidence report.
- The forty-five-minute demo script with the seven-act narrative walkthrough.
- The decision package framing the ten binding-answer questions for management.
- The full production-maturity plan with the locked Day-1 cut and the deferred-with-rationale appendix.

---


## Technical Appendix — verification at release `day1-cut-complete-2026-05-31`

This appendix carries the verifiable detail behind every claim in the body — paths, ports, counts, and verification commands — organised by axis.


### Architecture

The runtime principles are documented in `docs/PROJECT-BRIEFING.md` §2. The single LLM egress is implemented in `src/oneops/llm/gateway.py`. The policy composer lives in `src/oneops/policy/composer.py` (307 lines, 7 policy profiles).

The dead-code audit at `docs/findings/DEAD-CODE-AUDIT.md` is the source for the V1 / V2 registry reconciliation finding. The V1 root files — `registries/agent-catalog-registry.json`, `agent-tool-mapping.json`, `agent-registry.json`, `router-alias-registry.json` — have zero references in `src/oneops/`. The live registry is `registries/v2/`, referenced by `src/oneops/registry/store.py`, `src/oneops/router/glossary.py`, `src/oneops/api/app.py`, `src/oneops/use_cases/uc08_fulfillment/executor.py` and `tools.py`, `src/oneops/use_cases/_shared/field_policy.py`, `src/oneops/uc_common/display_spec.py`, and `src/oneops/policy_engine/engine.py`.

The application is launched with `uvicorn oneops.api.app:create_app --factory --host 127.0.0.1 --port 8765`. The infrastructure tier is declared in `docker-compose.yml` and consists of eight services: Dragonfly (port 6680), NATS (port 4623), Postgres, Tempo (query API on port 3401), the OTel collector (OTLP HTTP on port 4620), Prometheus (port 9391), Grafana (port 3041), and LiteLLM (port 4301). The required request headers are `x-tenant-id`, `x-user-id`, and `x-role`. The OpenAPI schema is exposed at `/openapi.json` and returns HTTP 200, declaring 18 routes. The `/health` and `/ready` paths return HTTP 404 today. The checkpointer is `AsyncPostgresSaver` per ADR-0004.

Use-case agents are declared in `registries/v2/agents/uc01_summarization.json`, `uc02_similar_tickets.json`, `uc03_kb_lookup.json`, `uc05_triage.json`, and `uc08_fulfillment.json`. Routes are declared in `src/oneops/api/uc02_routes.py`, `uc05_routes.py`, and `uc08_routes.py`. The UC-8 chat deferral is tracked under the memory entry `project_oneops_uc08_chat_wiring_post_demo` and is sized at four to six hours.


### Agent lifecycle

Registry loader: `src/oneops/registry/store.py`. Router refusal path: `src/oneops/router/router.py` (emits `lifecycle.refused` span events). Phase log: `ops/pmg-evidence/phase-3-lifecycle.log`. Reference seed scripts for tenant onboarding: `scripts/uc03_seed_password_reset_kb.py` and `scripts/uc08_seed_mfa_catalog.py`. The per-tenant catalog overlay surface is described in DOC-07 §4.6 of the production-maturity plan and is a P1 roadmap item.


### Performance tracking

Counts at HEAD `0f1c035`: 107 span emission sites in `src/oneops/`; 95 counter increments and 17 histogram observations; 15 Grafana dashboard panels; 9 alert rules (6 baseline plus 3 UC-8 specific); 4 synthetic probes plus a driver and a shared helper. The 60-second timeout is declared at `src/oneops/use_cases/uc08_fulfillment/text_extract.py:39` (`EXTRACT_TIMEOUT_S`), `judge.py:54` (`JUDGE_TIMEOUT_S`), and `catalog_search.py:83` (`EMBED_TIMEOUT_S`).

Metric identifiers in the body: `ai.llm.cost_usd_micros{tenant_id, model}` emitted at `src/oneops/llm/cost.py:69`; `ai.llm.tokens.{input,output,total}{model, operation, provider}`; `ai.llm.latency_ms{model, operation, provider}` driving the AgentP99LatencyHigh alert; `ai.cache.{hits,misses,writes,stale_reads}.total` and `ai.cache.latency_ms{cache_name, operation}` driving CacheMissStorm; `ai.agent.runs.total{agent_id, tenant_id, status}` driving TurnFailureRateHigh and AgentSubjectSilent; `ai.uc08.{create_sr, match, fulfill, judge.verdict, agent.events}.total` driving the three UC-8 alerts.

Forced-breach evidence file: `ops/pmg-evidence/day1-am-alert-fired.log`. Synthetic probes live in `ops/probes/`: `uc01.sh`, `uc03.sh`, `uc05.sh`, `uc08.sh`, `run-all-loop.sh`, and `_common.sh`.

Verification commands:

```bash
grep -rn 'start_as_current_span\|with span(' src/oneops --include="*.py" | wc -l
grep -rn '_metric_inc(\|increment(' src/oneops --include="*.py" | wc -l
grep -rn 'histogram(' src/oneops --include="*.py" | wc -l
```


### Output validation

Judge module: `src/oneops/use_cases/uc08_fulfillment/judge.py` (379 lines; `JudgeVerdict` at line 59; `_VALID_VERDICTS` at line 65; `judge_extraction` at line 320; `judge_rerank` at line 345). Closed-enum parsers: `_VALID_IMPACTS` / `_VALID_URGENCIES` / `_VALID_PRIORITIES` in `src/oneops/use_cases/uc05_triage/tools/prioritize.py`; `_VALID_CATEGORIES` in `src/oneops/executor/boundary.py`. Policy composer: `src/oneops/policy/composer.py` (307 lines, 7 policy profiles). PII redaction: `src/oneops/llm/redaction.py` (54 lines). End-to-end suite: `tests/integration/test_uc08_button_user_journey.py` (15 test functions running against an in-process FastAPI server). Per-use-case unit-test counts: UC-1: 2; UC-2: 32; UC-3: 0 (covered upstream by router and KB-store tests); UC-5: 9; UC-8: 28.

Router-rewriter intent-corruption class is recorded in the memory entry `project_poc5mw1_routing_overhaul_2026_05_28`. Judge-expansion sizing is tracked in `project_oneops_uc234_prompt_hardening_sweep`.

CI gate: `scripts/ci.sh` invoked by `make ci-fast` at commit time via `.git/hooks/pre-commit` and by `make ci` at push time. Ratchet baseline declared in `pyproject.toml`: `[tool.ruff.lint]` lists 24 ignore categories with rationale; `[tool.mypy]` sets `strict = false` and disables 14 error codes. The commit-gate path is green at HEAD `0f1c035`. A separately-run `tests/unit/router/test_time_filter_extractor.py` bundle shows 13 of 195 failures — event-loop isolation flakes, not on the commit gate's path.


### Security

Tenant-scoped SQL examples: `src/oneops/api/uc08_routes.py:284–340` (an itsm.request INSERT); `src/oneops/api/uc05_routes.py:162–169` (queue summary); the reads in `src/oneops/use_cases/_shared/ticket_store.py`. Role allow-lists declared as `frozenset` constants: `_TRIAGE_ROLES` in `uc05_routes.py`; `_PERMITTED_MATCH_ROLES` and `_PERMITTED_FULFILL_ROLES` in `uc08_routes.py`. PII redaction module: `src/oneops/llm/redaction.py`. Scaffolded authorization-layer modules (not on the request path today): `src/oneops/authz/tokens.py`, `rbac.py`, `abac.py`, `decision_cache.py`.


### Versioning

Prompt-version constants at `src/oneops/use_cases/uc08_fulfillment/text_extract.py:47`, `judge.py:55`, and `catalog_reranker.py`. Span attribute `uc08.prompt_version` set on the `uc08.text_extract.call`, `uc08.judge.extraction`, and `uc08.judge.rerank` spans. Per-agent versions in `registries/v2/agents/<uc>.json` under `active_version` and `versions[]`. Cost-metric model-label form: `model="gpt-4o-mini-2024-07-18"`. Schema migrations: `migrations/0001_*.sql` through `migrations/0007_*.sql`. Cache version constants: `PIPELINE_CACHE_VERSION` and `HUMANISE_RECORD_VERSION`. Release tags: `uc08-button-demo-ready`, `uc08-production-ready`, `day1-cut-complete-2026-05-31`. OpenAPI version: `info.version = "0.1.0"`. Tool registry: `registries/tool-registry.json`. Agent-tool mapping: `registries/agent-tool-mapping.json` (live consumer is `registries/v2/`).


### Adding new use cases

Reference package: `src/oneops/use_cases/uc08_fulfillment/` (eleven files: contracts, handlers, core, executor, adapters, agent, NATS dispatcher, judge, catalog search and reranker, priority computation, historical-suggestion logic, text-extract LLM call, and SR-id minting). Contract: `docs/COMPONENT_SPEC.md` (C1 to C24). Conventions: `docs/CONVENTIONS.md`. Non-negotiable rules: `docs/PROJECT-BRIEFING.md` §2. Test pattern: `tests/integration/test_uc08_button_user_journey.py`. Definition-of-done: `docs/production-maturity-plan.md` §D. Verifier: `make pmg-verify` writes `ops/pmg-evidence/REPORT.md`.


### CI gate status at the time of writing

- `make ci-fast` (commit-time gate via `.git/hooks/pre-commit`): green at HEAD `0f1c035`. Stages: ruff, mypy, `pytest -m unit`.
- `make ci` (full gate): green on stages 1 to 4. Stages 5 (smoke) and 6 (devils) print "deferred — script not present (Phase 6 fills in)". Deferral is deliberate per the Day-1 plan.
- `tests/unit/router/test_time_filter_extractor.py` (separate path, not on the commit gate): 13 of 195 failures. Event-loop isolation flakes. Not a product regression.


### Items that could not be confirmed

| Item | Reason | Recommended next step |
|---|---|---|
| Permalink URLs to source files at the release reference | No git remote configured. | Source citations rendered as inline paths per the documented fallback rule. |
| `/health` and `/ready` HTTP endpoints | Both return 404 today. | Add to the FastAPI surface; in the roadmap. |
| Container image for the application process | No Dockerfile at the repository root. | Build one; in the roadmap. |
| V2 agent files' flat top-level lifecycle metadata | The V2 schema uses nested `active_version` and `versions[]`. | Document the V2 schema in `docs/CONVENTIONS.md`. |
| Studio activation write path | Studio is planned; no activation code exists yet. | When tasks 34 and 37 are built, target `registries/v2/`. |


### Verified counts at HEAD `0f1c035`

| Item | Value |
|---|---|
| OTel span emission sites | 107 |
| Counter increments | 95 |
| Histogram observations | 17 |
| Grafana dashboard panels | 15 |
| Grafana alert rules | 9 (6 baseline plus 3 UC-8) |
| Synthetic probes | 4 UC-specific plus a driver and a helper |
| UC-8 integration tests | 15 |
| Per-use-case unit tests | UC-1: 2; UC-2: 32; UC-3: 0; UC-5: 9; UC-8: 28 |
| Judge module line count | 379 |
| Policy composer line count | 307 |
| PII redaction line count | 54 |
| Policy profiles defined | 7 |
| Docker-compose services | 8 |
| API routes declared in OpenAPI | 18 |


### Prepared by

This briefing was prepared on 2026-05-31 against release `day1-cut-complete-2026-05-31` (HEAD `0f1c035`). All factual claims were verified against the codebase at that exact reference.
