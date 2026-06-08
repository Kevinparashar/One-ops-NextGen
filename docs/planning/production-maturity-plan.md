---
title: POC-5-MW-1 Production-Maturity Plan
prepared_by: NextGen AI Platform
date: 2026-05-29
status: Active — driving PMG sign-off
---

# POC-5-MW-1 Production-Maturity Plan

## Purpose

Validate to Product Management (PMG) that POC-5-MW-1 is production-mature across the six axes the manager named: **agent lifecycle**, **performance tracking**, **output validation**, **security**, **new-capability developer experience**, and **end-to-end pipeline automation**.

This document is the single source of truth for: target requirements, current state, gap matrix, definition of done, and prioritized roadmap.

---

## A. Target architecture requirements (from manager's documents)

### 1. Agent lifecycle management
- 6-phase lifecycle: Register → Test → Activate → Monitor → Deprecate → Retire (DOC-03, CLAUDE.md).
- Semantic versioning in `AGENT_CATALOG`; multiple versions coexist via `agent_id@version`; rollback = re-point reference.
- A/B traffic split via Istio; quality-gated promotion tied to DOC-05 quality scoring; auto-rollback on quality regression.
- `AgentManifest` export/import + registry watcher.
- Per-tenant catalog overlay (DOC-07 §4.6) so an agent can be activated/deactivated per tenant.

### 2. Performance tracking at every point
- OTel spans on every phase, every tool call, every LLM call. Required attributes: `request_id`, `tenant_id`, `agent_id`, `agent_version`, `trigger_type`, `confidence_score`, `autonomy_level`, `platform_service` (DOC-05).
- SLOs: fast-path p95 < 2 s, standard p95 < 6 s, complex p95 < 12 s, multi-turn p95 < 3 s; AWS API GW 29 s hard cap with `REQUEST_TIMEOUT_SECONDS=25`.
- Sampling policy: 100% dev/staging, 10% prod, 100% errors, tail-based >p99 (DOC-05 §4.1.6).
- Metrics catalog: per-agent latency, per-tool error rate, per-tenant cost, router top-K hit rate, LLM token spend, idempotency hit rate, circuit-breaker state, cache hit/miss.
- Per-tenant cost attribution via LiteLLM, surfaced in dashboards; per-tenant daily budget tiers (Free $5 / Pro $50 / Enterprise custom) with 80%-alert + 100%-throttle actions (DOC-05 §4.3.4).
- Quality scoring pipeline (4 weighted dimensions — relevance 0.35 / factuality 0.30 / format 0.15 / completeness 0.20) feeding back into agent lifecycle gates; per-agent thresholds (DOC-05 §4.5.2).
- **Misclassification Detector** with 4 rules (intent-mismatch, confidence-low-but-act, autonomy-overshoot, drift) emitting on NATS subject `ai.misclassification.{tenant_id}` with correction processor that closes the loop within one minute (DOC-05 §4.2.8 + DOC-06 §4.4). **Status: deferred to Workstream 3.1; not yet shipped.**

### 3. Output validation / behavioural correctness
- Structured outputs as the contract; every routing/classification LLM is schema-validated.
- AI testing pyramid (DOC-12): unit → **integration** → agent-evaluation → prompt-regression → load → chaos. (Integration tests run the full pipeline with mock LLM; contract tests sit inside the integration layer.)
- Prompt regression suite required on every prompt edit; drift detection via per-capability quality score trend; block-merge on >5% quality drop (DOC-12).
- Faithfulness / groundedness checks on RAG.
- Explainability traces opt-in per tenant, fail-open, stored against `request_id` in ClickHouse (DOC-04 §10.3.4, DOC-05).
- Behavioural CI: adversarial probes vs paraphrase coverage; 500+ prompt-injection patterns nightly (DOC-04).

### 4. Security touchpoints
- 3-layer auth: L1 Envoy RBAC → L2 Bridge Service (full JWT + OPA) → L3 ABAC middleware (DOC-04).
- 3-tier auth cache: FreeCache 60 s → Dragonfly 5 min → Postgres+OPA.
- Per-component fail-mode policy in the SECURITY_PIPELINE: `fail_mode: closed | open_with_alert` declared per stage (DOC-04 §3.3). Tier-1 mutation paths fail-closed; Tier-2 read paths may fail-open-with-alert.
- Tenant isolation by construction — `tenant_id` required parameter on every repo method; tenant-prefixed Dragonfly keys; tenant-scoped NATS subjects.
- **Immutable `TenantContext`** propagated through every phase, validated at construction (DOC-07); tier ladder is data, not code: rate limits 10 / 60 / 300 RPM × Free / Standard / Enterprise; daily token budgets 100K / 2M / 50M; conversation retention 7 / 30 / 90 days; audit retention 30 / 90 / 365 days (DOC-07 §3 + §4.4). **Status: TenantContext not yet immutable across phases — deferred to Workstream 3.4.**
- Multi-layer prompt-injection defense: regex + DistilBERT + LLM judge (DOC-04).
- **PII handling via reversible token store** with TTL + detokenization on egress (DOC-04 §6.1, §6.4). Replaces the current irreversible outbound scrub. **Status: not yet shipped — deferred to Workstream 3.2; gated on §G #8 manager decision.**
- Mutation authorization risk-scored low/med/high/critical; critical mutations require manual approval (DOC-04 §7).
- Secrets via AWS Secrets Manager + ESO; mTLS via Istio; signed service JWTs on internal calls.
- Immutable audit trail (hash-chained) for compliance grade.

### 5. Building new capabilities (DevEx)
- Registry-driven: a new UC = one `AGENT_CATALOG` entry + `AGENT_TOOL_MAPPING` entries.
- Generic factory `load_agent()`; tools registered via `@register_tool` decorator.
- Activation conditions declared as data; deps/exclusions in the agent record.
- Tool allowlist scoped per `(agent_id, service_id)`; Executor validates every plan step before execution.
- Contract-first Pydantic schemas; `AgentManifest` export/import for CI promotion.
- **Formal 3-level intent ontology** (`domain.category.action`) covering all 29 agents, ~50 leaf nodes, parent-child fallback chain on low confidence; embedding-based agent discovery replacing substring matching; 4-phase classification (keyword → embedding → DistilBERT → context-aware) (DOC-10 §3.3, CLAUDE.md). **Status: surfaced as §G #6 open question; the ontology shape (closed taxonomy vs analytical-only labelling layer) is the manager's decision because a closed taxonomy conflicts with codebase rule §2.1 "no phrase catalogs". Workstream 5b is unscoped until the question is answered.**
- `DATA_PRODUCT_REGISTRY` + agent `data_products` declaration + Planner data-product health check for degradation (CLAUDE.md, DOC-03). **Status: not shipped.**

### 6. End-to-end pipeline automation
- CI/CD pipeline: lint → unit → integration → contract → AI-eval gate → SAST + Trivy + secrets-scan (gitleaks/trufflehog) → pip-audit → build + SBOM → IaC plan → canary → promote (DOC-11 §8).
- GitOps with ArgoCD for EKS; SAM/CDK for Lambda.
- Environment promotion dev → staging → prod with quality + safety gates.
- IaC: Terraform for AWS infra, Helm for K8s, ArgoCD for app sync.
- Branching/release: trunk-based, feature flags for dark launches.
- Rollback: Istio VirtualService weights for EKS; CodeDeploy `Linear10PercentEvery5Minutes` for Lambda; auto-rollback on SLO breach.
- Performance-baseline-check gate (DOC-11 §8.2): p95 latency within 20% of baseline (warning) or 50% (block).
- DR drills, chaos engineering, load tests mandatory pre-prod.

### 7. Scoping commitments (what is explicitly NOT in this milestone)
This subsection makes deferred-but-known scope explicit so PMG can distinguish "missed by the engineer" from "deferred with rationale."

**Deferred to a later milestone (not in this plan's roadmap):**
- **DOC-08 integration surface** beyond REST: WebSocket-first chat over ALB, Bridge Service split-traffic, inbound + outbound webhooks, ChatOps (Slack / Teams), language SDKs (Python / JS / Go), URL + header API versioning with deprecation policy. **Reason:** scope question §G #2 — Bridge Service in/out of milestone is unresolved.
- **DOC-09 ITOM use cases** UC-9 (event correlation), UC-10 (alert triage), UC-11 (runbook automation with approval gates), UC-12 (change risk via graph), UC-13 (capacity planning), UC-14 (proactive discovery). **Reason:** scope question §G #4 — PMG sign-off scope for the 29-UC catalog is unresolved.
- **DOC-13A platform-service UCs** UC-15 to UC-29 (15 UCs spanning Graph / Discovery / ITSM-enhanced / Workflow / UX) and the 40+ associated tools (14 graph + 7 discovery + 10 ITSM enhanced + 5 workflow + 4 portal). **Reason:** same as §G #4.
- **OneOps Studio** (DOC-13A §7) — no-code agent author plane that compiles to the same registry records with twice-checked RBAC. **Reason:** scope question §G #3 — Studio is a POC-5-MW-1 addition; the target docs do not have it; PMG's stance is unconfirmed.
- **IT Operations Graph as a first-class backend / "the moat"** (`oneops-capabilities.md`). Current plan treats Graph as enrichment, not as a load-bearing data product. **Reason:** the Graph-as-moat framing was raised in DOC-13 capability-evaluation (scored 0/5 across docs); resolution depends on PMG's read of the competitive positioning.
- **DOC-06 LLM strategy** beyond single egress: per-capability model selection matrix, tenant BYOM with virtual keys + isolated budgets, fine-tuning pipeline, on-prem (vLLM / Ollama). **Reason:** P2 scale-time work; deferring is consistent with §E.
- **Long-term user memory + episodic memory layers** (CLAUDE.md). **Reason:** P2 scale-time work.
- **Customer-facing per-tenant cost / usage dashboard** (DOC-05). **Reason:** the per-tenant cost board in P0-#5 is operator-facing; the customer-facing version is P2.

**Scope-changing manager decisions blocking these (route to §G):**
- §G #1 — full EKS migration on PMG critical path or not? Gates the timing of DOC-08 / DOC-11 work.
- §G #2 — Bridge Service in / out? Gates DOC-08 work.
- §G #3 — Studio in / out of PMG ask? Gates DOC-13A §7.
- §G #4 — UC catalogue scope (UC-1+UC-3 only, or commitments for UC-4..UC-29)? Gates DOC-09 + DOC-13A capability work.
- §G #6 — intent ontology shape (closed taxonomy vs analytical-only labelling)? Gates Workstream 5b.
- §G #10 — dual-cluster NATS vs single? Gates DOC-11 infra work.

---

## B. Current state of POC-5-MW-1 (honest inventory)

### Shipped and verified
- **Routing pipeline** — focus-aware control gate + LLM disambiguator + embedding field matcher; `update_focus` LangGraph node + `focus_entity_id` state channel; all 6 router stages emit OTel spans (verified end-to-end on 2026-05-29).
- **Hybrid RAG (UC-3)** — FTS+vector RRF + per-article relevance gate at 0.50 cosine + top-K=3 + degraded-mode bypass to deterministic composer; linked-to KB also gated by relevance scorer.
- **Observability** — Tempo at :3001, Prometheus, Grafana; 26–40 spans per request; W3C `traceparent` propagation across every NATS hop; per-trace cost tracking via LiteLLM gateway.
- **Policy layer** — 41 reusable safety blocks across 8 profiles in `src/oneops/policy/`; every LLM call composes through `compose(Profile.X, ...)`. No raw system prompts anywhere.
- **Tenant scoping** — `tenant_id` is first SQL predicate everywhere; Dragonfly keys prefixed; NATS subjects scoped; OTel labels carry `tenant_id`.
- **LiteLLM single egress** — every LLM/embedding call routes through `:4001`; per-tenant cost recorded on every call.
- **JSON registries** at `registries/` — agent-catalog, agent-registry, agent-tool-mapping, capability, role-permission, router-alias, tool, service, service-schema. Loaded + validated at boot in `src/oneops/registry/`.

### Stubbed / designed only
- Agent lifecycle: registries lack `version` / `status` / `lifecycle_stage`. No state machine. No A/B. No `AgentManifest`. No per-tenant overlay.
- Authentication at door: PMG Doc 4 documents that we trust the customer's IdP today; JWT verification is not enforced at the front door.
- RBAC: descriptors + decision cache exist, but `(role × tool)` matrix is not materialized; runtime tool-runner validates, author-time gate does not exist.
- PII: outbound scrub at LLM gateway only; inbound classification, pre-embedding scrub, cache scrub not built.
- Audit: append-only Postgres log; not hash-chained; no RTBF endpoint.
- Eval: smoke scripts exist (81/84, 40/40, 11/11); no CI eval gate, no drift detector, no faithfulness check enforced in CI.
- Perf tracking: spans are real; SLO alert rules and per-tenant cost dashboard not wired.
- CI/CD: no `.github/workflows/`. No Terraform / Helm / ArgoCD. No canary. No DR drill. No chaos.
- Secrets: dev-grade `.env`.

---

## C. Gap matrix

| Concern | Required (target) | Have (POC-5-MW-1) | Gap | Risk |
|---|---|---|---|---|
| Lifecycle | 6-phase state machine, semver, A/B split, quality-gated promotion, manifest export/import, per-tenant overlay | JSON registries, manual edits | No version field enforced, no state machine, no A/B, no quality gate, no manifest, no overlay | **P0** |
| Perf tracking | Spans + SLO alerts + error budgets + synthetic probes + per-tenant cost dashboards | Spans + cost recorded; Grafana boards live | No alert rules, no SLO codification, no synthetic probes, no customer-facing dashboards | **P1** |
| Output validation | Prompt-regression CI gate, drift detector, faithfulness checks, explainability store, adversarial probe suite | Ad-hoc smoke scripts, RAG relevance gate | No CI eval gate, no drift detection, no explainability store, no adversarial CI | **P0** |
| Security — AuthN | 3-layer auth, Envoy RBAC, Bridge JWT, ABAC L3, signed internal JWTs | ABAC + RBAC code present; JWT verification not enforced at door | Front-door JWT, internal service JWT signing, Envoy/Bridge layer | **P0** |
| Security — AuthZ matrix | Materialised `(role × tool) → allow/deny`, twice-enforced | Descriptors + decision cache, enforcement at data layer | RBAC matrix not materialised; author-time gate absent | **P0** |
| Security — PII / Audit | Inbound + outbound + cache + vector PII scrub; hash-chained audit; per-tenant retention; RTBF | Outbound scrub only; append-only log | Inbound classification, pre-embedding scrub, immutable audit, RTBF endpoint | **P1** |
| Tenant isolation | Construction-level, every layer | Done | One-shot per-tenant delete + adversarial cross-tenant CI | **P1** |
| DevEx for new UC | Registry entry only + manifest + scaffolding | Folder-per-UC + registry + shared utils | No scaffolding CLI, no manifest, no Studio compile path | **P1** |
| CI/CD | Lint → unit → contract → eval gate → build → IaC → canary → promote | Makefile, docker-compose | No CI pipeline in repo, no IaC, no canary, no promotion | **P0** |
| Infra / scale | EKS + Istio + Lambda hybrid + Dragonfly cluster + 3-node NATS + DR | docker-compose dev stack | No K8s, no Istio, no Lambda, no DR, no chaos | **P1** |
| Operational security | Secret manager, image signing, SBOM, dep scanning, egress allow-list, pentest, IR runbook | Dev-grade `.env` | All of the above | **P1** |

---

## D. Definition of done — PMG-demonstrable checklist (30 items)

1. Every agent in the registry has `version`, `status`, `owner`, `activation_condition`, `lifecycle_stage` fields populated and validated at boot.
2. `AgentManifest` export and import command exists and round-trips a UC end-to-end.
3. Agent lifecycle state machine enforced in code: only `active` agents route; `deprecated` agents log a deprecation warning span attribute; `retired` agents 404.
4. Quality-gated promotion: a quality score below threshold blocks `active`-stage transition; rollback is one ref-point change.
5. Per-tenant catalog overlay: tenant X can disable agent Y without redeploy.
6. Every span carries required attributes (`request_id`, `tenant_id`, `agent_id`, `agent_version`, `confidence_score`, `autonomy_level`); CI fails on missing-attribute span samples.
7. SLO Prometheus rules + Grafana alerts wired for the 4 per-request-type p95s; synthetic probe runs every minute per UC and reports green/red.
8. Per-tenant cost dashboard demonstrable in Grafana; daily budget enforcement provably blocks at threshold.
9. CI runs prompt-regression suite on every PR touching a prompt; fails the merge on any regression.
10. Drift detector job emits a weekly per-UC quality-score delta with a paged alert on >X% drop.
11. RAG faithfulness check on every UC-3 turn — ungrounded answer → refusal path, traced as `degraded.no_grounding`.
12. Explainability trace stored against `request_id` and retrievable from the trace store.
13. JWT verification enforced at the front door; unauthenticated requests get 401 before reaching NATS.
14. Materialized RBAC `(role × tool)` matrix loaded at boot; runtime tool-runner refuses out-of-scope calls and emits an audit event.
15. Cross-tenant adversarial CI suite (1000 attempts) shows 0 leakage.
16. PII detection runs on inbound user text; redaction emits a span event with PII class counts (no raw values).
17. Hash-chained immutable audit log for Tier-1 mutations; verification command demonstrable.
18. Right-to-be-forgotten endpoint operational; one tenant + one user can be wiped on demand.
19. Secrets sourced from AWS Secrets Manager (or vault) in non-dev environments; no `.env` in prod images.
20. Image signing + SBOM + dep scanning in CI; failing scan blocks merge.
21. New-UC scaffolding command (`oneops scaffold uc <id>`) produces a working stub passing CI in <10 min.
22. UC author checklist auto-attached to the PR template; PR cannot merge unless ticked.
23. CI/CD pipeline (lint, unit, contract, eval gate, build, IaC plan, canary, promote, auto-rollback on SLO breach) shipped in `.github/workflows/` or equivalent.
24. IaC: Terraform for AWS infra, Helm for K8s app, ArgoCD app-of-apps; `terraform plan` clean in CI.
25. Canary deploy demonstrable: shift 5% → 50% → 100% with auto-rollback on quality/SLO regression.
26. DR backup + restore drill executed and timed against RPO/RTO targets.
27. Chaos suite (kill a worker, drop NATS node, lag Postgres) runs nightly in staging.
28. Adversarial prompt-injection probe suite green; results signed and dated.
29. Customer-facing usage + cost dashboard available read-only per tenant.
30. UC-1 + UC-3 pass all of the above with zero regression vs the current 96.4% routing pass rate.

---

## E. Prioritized roadmap (multi-week, what production-mature actually takes)

### P0 — PMG sign-off blockers (3–4 weeks at production grade)
| # | Item | Size | Depends on |
|---|---|---|---|
| 1 | Agent lifecycle state machine + `AgentManifest` export/import | M | — |
| 2 | Front-door JWT verification + signed internal service JWTs | M | — |
| 3 | Materialized RBAC `(role × tool)` matrix + twice-enforced | M | (1) |
| 4 | Prompt-regression CI gate + adversarial probe suite + RAG faithfulness enforcement | M | — |
| 5 | SLO alert rules + per-tenant cost Grafana board + synthetic probes per UC | S | observability shipped |
| 6 | Cross-tenant adversarial CI (1000 attempts) + one-shot per-tenant delete | S | — |
| 7 | CI/CD pipeline scaffold (lint → unit → contract → eval gate → build) | M | (4) |

### P1 — Should-have (4–10 weeks)
- Inbound PII detection + pre-embedding scrub + cache scrub
- Hash-chained immutable audit log + RTBF endpoint + per-tenant retention
- Drift detector + per-UC quality-score rollup + explainability trace store
- Per-tenant catalog overlay + feature-flag system
- Scaffolding CLI `oneops scaffold uc` + PR-template UC checklist
- IaC (Terraform AWS, Helm chart) + ArgoCD app-of-apps
- Canary deploy + auto-rollback on SLO / quality breach
- Operational security: secret manager, image signing, SBOM, dep scan, egress allow-list

### P2 — Scale-time
- EKS + Istio + Lambda hybrid migration
- Chaos engineering nightly suite
- DR multi-region + drilled RTO/RPO
- OneOps Studio (author plane + compiler)
- Customer-facing cost/usage dashboard
- Multi-model routing in LLM gateway
- Long-term user memory + episodic memory layers
- Action UC + agent-to-agent autonomy activation

---

## F-LOCKED. Day-1 cut — Option B (locked 2026-05-29)

**Scope locked at: UC-5 Triage + UC-8 Fulfillment + lifecycle enforcement + SLO/cost/probes + local CI gate + evidence folder + manager decision package.**

Discovered 2026-05-29 that the codebase already contains: 9-agent registry (UC-5 + UC-8 entries pre-registered), `src/oneops/executor/graph.py` with `Send` + `wave⇄run_step` + `interrupt()` orchestration substrate, `src/oneops/llm/gateway.py` with `fallback_model` failover, `src/oneops/authz/{tokens,rbac,abac,decision_cache}.py` (internal service JWT + RBAC + ABAC), `src/oneops/llm/redaction.py` (structural PII scrub). This unblocks UC-8 — it plugs into existing orchestration substrate rather than building it.

**Expected Day-1 coverage of 22 docs: ~80%.** The remaining ~20% (hash-chained audit, reversible PII token store, materialized RBAC matrix, front-door JWT, DOC-08 WebSocket/Bridge/webhooks/ChatOps/SDKs, DOC-09 ITOM, DOC-10 intent ontology, DOC-11 EKS/Istio/Lambda/IaC, DOC-13A platform UCs UC-15..UC-29 + Studio) remains named-but-deferred in `§A.7 Scoping commitments` with rationale and roadmap weeks.

**Day-1 execution order (locked):**
1. (0.5 h) Scaffolding — `ops/pmg-evidence/` + `verify-all.sh` skeleton + `scripts/ci.sh` skeleton. Evidence has a fixed home before any deliverable starts.
2. (2.5–3 h) UC-5 Triage handler at `src/oneops/use_cases/uc05_triage/`. Registry entry `triage_agent` already exists; this adds the handler module + tests. Real classification through policy composer + LLM gateway, structured `TriageDecision` output, full span coverage. Tests + Tempo trace IDs captured to `ops/pmg-evidence/uc05-routing.log`.
3. (1 h) Lifecycle state machine — add `version`/`status`/`lifecycle_stage`/`owner` fields to `registries/agent-catalog-registry.json` schema, boot-time validation, router refuses non-`active` agents with `lifecycle.refused` span. Mark UC-1, UC-3, UC-5, UC-8 as `active`; one synthetic `deprecated` agent for the demo. Evidence to `ops/pmg-evidence/lifecycle.log`.
4. (4–5 h) UC-8 Fulfillment handler at `src/oneops/use_cases/uc08_fulfillment/`. Registry entry `fulfillment_agent` already exists; this adds the handler module + tests. Plugs into existing `Send` + `wave⇄run_step` + `interrupt()` substrate. Demo flow: catalog_item → task DAG → parallel wave → interrupt approval gate → resume → sequential wave → final notification. Tempo trace captured to `ops/pmg-evidence/uc08-fulfillment.log`.
5. (1 h) SLO alerts + per-tenant cost dashboard + synthetic probes for UC-1 / UC-3 / UC-5 / UC-8. Force a threshold breach to prove the alert fires; capture to `ops/pmg-evidence/slo-alert.log` + dashboard screenshot.
6. (1 h) Local CI gate — `scripts/ci.sh` running ruff → mypy → pytest unit → pytest integration → smoke → devil's-play. `Makefile` target `make ci`. Pre-commit hook calls `scripts/ci.sh --fast`. RUNBOOK entry. Capture clean run and a deliberately-broken-probe block to `ops/pmg-evidence/ci-gate.log`.
7. (0.5 h) Manager decision package + PMG demo runbook + run `verify-all.sh` to produce the final evidence report. `docs/briefings/manager-decision-package.md` (10 §G questions, recommended answers + cost-of-alternative + downstream gates). `docs/runbooks/pmg-demo-runbook.md` (script of the meeting).

**Quality guardrails (non-negotiable):** every deliverable lands with tests + an evidence log + a RUNBOOK entry; no stubs, no mocks where real implementation is the contract; if any step is truly stub-grade after its hour budget, it moves to Day 2 — not shipped as a fake; both the existing smoke (81/84) and devil's-play (11/11) stay green; the CI gate proves it.

---

## F. The 2-day cut (honest scope, production-grade, no shortcuts)

The full P0 list is 3–4 weeks of focused work to do **at production grade with full tests, docs, and CI integration**. Two days at production grade = three P0 items shipped fully, not seven shipped halfway.

### Selection rationale
The three picked maximise PMG-visible delta per hour, depend on nothing else, and convert existing platform investments into demo-able artefacts.

### Day 1
- **Morning (4 hr) — P0-#5: SLO alert rules + per-tenant cost Grafana board + synthetic probes.**
  Smallest P0, highest visibility. Converts the existing 26–40-span tracing into a real perf-tracking story.
  Deliverables: 4 Prometheus alert rules (fast-path / standard / complex / multi-turn p95s), 1 Grafana dashboard JSON committed to `ops/grafana/dashboards/`, 1 per-UC synthetic probe script in `ops/probes/`, runbook entry.
  **PMG evidence:** `ops/pmg-evidence/day1-am-slo-probes.log` capturing the synthetic probe running every minute against UC-1 + UC-3 with measured latencies; `ops/pmg-evidence/day1-am-cost-dashboard.png` of the Grafana board with real LiteLLM data; `ops/pmg-evidence/day1-am-alert-fired.log` showing a forced threshold breach firing the alert.

- **Afternoon (4 hr) — P0-#1: Agent lifecycle state machine + `AgentManifest` export/import (part 1).**
  Add `version`, `status` (`draft`/`active`/`deprecated`/`retired`), `lifecycle_stage`, `owner` fields to `registries/agent-catalog-registry.json` schema. Enforce at boot. Router refuses non-`active` agents with a `lifecycle.refused` span event. Unit tests + integration test that a `deprecated` agent emits a deprecation attribute and a `retired` agent 404s.
  **PMG evidence:** `ops/pmg-evidence/day1-pm-lifecycle.log` capturing pytest output + boot-time schema validation log + Tempo trace ID for a deprecated-agent route attempt showing the `lifecycle.refused` span.

### Day 2
- **Morning (4 hr) — P0-#1 (part 2): `AgentManifest` export/import command.**
  CLI `oneops manifest export <agent_id>` produces a signed JSON bundle (catalog row + tool mappings + policy profile + UC code reference). `oneops manifest import <path>` validates and writes. Round-trip test for UC-1 and UC-3.
  **PMG evidence:** `ops/pmg-evidence/day2-am-manifest-roundtrip.log` capturing a UC-1 + UC-3 export → wipe → import → routing-still-works flow; the signed JSON bundles saved to `ops/pmg-evidence/manifests/`.

- **Afternoon (4 hr) — Local CI gate (replaces GitHub Actions; no GitHub access at present) + P0-#4 (eval gate, scoped) + manager decision package.**
  - **Local CI gate (2h):** `scripts/ci.sh` runs ruff → mypy → pytest unit → pytest integration → smoke (81/84) → devil's-play (11/11), fail-fast. `Makefile` target `make ci` invokes it. Pre-commit hook at `.git/hooks/pre-commit` calls `scripts/ci.sh --fast` (skips integration to keep commit time bounded). RUNBOOK entry documents "run `make ci` before push". When GitHub becomes available, the workflow YAML is a 10-line wrapper around `scripts/ci.sh` — logic is portable, not platform-locked.
    **PMG evidence:** `ops/pmg-evidence/day2-pm-ci-gate.log` capturing `make ci` running end-to-end and exit 0; `ops/pmg-evidence/day2-pm-ci-blocks-bad-merge.log` capturing a deliberately broken probe being rejected.
  - **Manager decision package (2h):** `docs/briefings/manager-decision-package.md` framing each of the 10 §G open questions as: question, recommended answer + rationale, cost of the alternative, downstream gates. Hand-deliverable to manager for binding answers.
    **PMG evidence:** the doc itself is the artefact.

### Explicitly NOT in the 2-day cut (would compromise quality if forced)
- **P0-#2 JWT verification at door** — needs IdP integration spec, JWKS rotation, signed internal JWT scheme. A half-done JWT layer is a security regression, not progress. Sized 2–3 days alone.
- **P0-#3 Materialized RBAC matrix** — requires generation from `role-permission-registry.json`, twice-enforcement plumbing, and migration. Sized 2–3 days alone.
- **P0-#4 (full)** drift detector + faithfulness in CI — the eval gate ships Day 2 PM; drift detector + per-UC quality scoring is a separate ~2-day job.
- **P0-#6** cross-tenant adversarial CI — requires harness + 1000-attempt probe corpus. Day 3 work.
- **Workstream 3 audit omissions** (Misclassification Detector, reversible PII token store, Token Budget Enforcer, TenantContext immutable + tier ladder) — each is ~3 days at production grade.
- **Workstream 4 infrastructure port** (EKS + Istio + Lambda + Bridge + ALB WS + ClickHouse + Dragonfly cluster + dual-NATS + ArgoCD + multi-region DR) — 8–12 weeks.
- **Workstream 5 capability expansion** (UC-2..UC-29 + intent ontology + ITOM + platform-service UCs + Studio) — 12–24 weeks.

The full deferred list, with deferral rationale, is in `§A.7` and `§G`.

### Quality guardrails for the 2-day push (non-negotiable)
- **No quality compromise.** No stubs, no mocks where real implementation is the contract. If an item is truly stub-grade after 4 focused hours, it is moved to Day 3 — not shipped as a fake.
- **PMG-demonstrable evidence per deliverable.** Each item produces a log file (or screenshot) under `ops/pmg-evidence/` showing the working behaviour. A master `ops/pmg-evidence/verify-all.sh` runs every verification and produces a one-page markdown report linking every claim to a log file.
- No keyword phrasebooks in any new code path ([[feedback_descriptions_principle_not_phrases]]).
- Every new LLM call (if any) composes through the policy layer.
- Every new span carries `tenant_id` + `request_id` minimum.
- No silent failures — every new failure mode emits an OTel event and returns a typed error.
- No new files outside the existing module boundaries unless justified in this doc.
- Every Day 1 + Day 2 deliverable lands with tests and a `docs/runbooks/RUNBOOK.md` entry.
- No `git push --force`, no skipped pre-commit hooks, no `--no-verify`.
- Both days end with the existing smoke suite (81/84) and 11-probe devil's-play green; the CI gate proves it.

---

## G. Open questions for the manager (block scoping until answered)

1. **Scope of "production"**: target docs describe full AWS EKS + Istio + Lambda + multi-region DR. POC is on docker-compose. Does PMG sign-off require the EKS migration, or sign-off on the two live UCs at current dev/staging infra + a credible roadmap to EKS?
2. **JWT verification at door**: PMG Doc 4 says we trust customer IdP today; target DOC-04 says Envoy RBAC + Bridge Service do it. Is the Bridge Service in-scope for this milestone or deferred?
3. **Studio (PMG Doc 7)**: target docs do not have an equivalent — Studio is a POC-5-MW-1 invention. On the PMG agenda or kept separate?
4. **Platform-services UCs (UC-15 to UC-29)**: POC has UC-1 + UC-3 only. Is PMG sign-off scoped to live UCs, or does the manager expect coverage commitments for the 29-UC catalog?
5. **Audit store**: target says ClickHouse for immutable audit + explainability. POC uses Postgres append-only. Adopt ClickHouse now or stay on Postgres until volume forces it?
6. **Intent ontology (CLAUDE.md `INTENT_ONTOLOGY` 3-level)**: POC routing is two-layer LLM (control gate + disambiguator) over registry descriptions — explicitly no phrase catalogues per the descriptions-are-semantic-principles thumb rule. If the target ontology means a closed taxonomy customers must map into, it **conflicts** with that thumb rule and will regress on new phrasings. Confirm: ontology = analytical/labelling layer over LLM decisions, not a hardcoded routing key.
7. **Agent-to-agent autonomy**: transport live, orchestration not. Is activation gated on the Action UC as PMG Doc 4 says, or earlier?
8. **DOC-04 §6 PII token store** vs POC's outbound-scrub-at-gateway: target wants reversible detokenization; POC redacts irreversibly. Confirm direction.
9. **Per-request-type SLOs in CLAUDE.md** (fast-path < 2 s, etc.) — contractual or aspirational? Drives the alert thresholds in P0-#5.
10. **Conflict between target DOC-11 (single 3-node NATS) and CLAUDE.md (dual-cluster `nats-ops` + `nats-obs`)** — which is canonical for POC sizing?

---

## H. Bottom line

POC-5-MW-1 is **demo-mature for UC-1 + UC-3**. It is **not production-mature** against the target on five of six manager axes. The 2-day cut in §F flips three P0 columns from amber to green with full production-grade rigour. Reaching full PMG sign-off needs the remaining four P0 items, sized at ~2 more weeks of focused work. The roadmap in §E does not require the EKS migration to be on the PMG-sign-off critical path; that decision is open question #1.
