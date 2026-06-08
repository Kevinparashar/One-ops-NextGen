---
title: POC-5-MW-1 — Project Briefing for AI Collaborators
purpose: Self-contained context an external AI (or new engineer) needs to do production-grade work on this codebase without prior session history
date: 2026-05-29
status: Active source-of-truth for cross-AI handoff
---

# POC-5-MW-1 — Project Briefing for AI Collaborators

> **How to use this doc.** This is the single document to hand to any AI (or human) who has never seen this codebase before. It contains: what the project is, where to find things, the non-negotiable rules, the current state, how to run it, and what production-grade work means here. After reading this, plus the two cross-referenced docs (`docs/planning/production-maturity-plan.md` and `docs/architecture/ARCHITECTURE.md`), they should be able to execute production-grade work without breaking conventions.

---

## 1. What this project is

**OneOps** is an AI-native ITSM/ITOM assistant. It answers natural-language questions about IT records (incidents, requests, problems, changes, assets, CMDB) and the customer's knowledge base. Two use cases are live:

- **UC-1 — Record Summarization.** Multi-turn summaries of a focused record; field-level reads ("what is the RCA", "any solution"); 2-hop linked-record traversal.
- **UC-3 — Knowledge Lookup.** Hybrid retrieval (FTS + vector RRF) with a calibrated relevance gate. Also handles linked-to KB lookups for a focused ticket.

The product target is **1000 use cases on the same substrate**. Every architectural decision is evaluated against that target, not against the current count of 2. ("We only have 2 UCs so we can skip X" is an invalid argument.)

**Customer surface:** API chat endpoint. Multi-tenant from day 1. Customer brings their own IdP (today the IdP token is trusted; full JWT verification at the door is on the production-maturity P0 list).

**Status today:** Demo-mature for UC-1 + UC-3 (routing 96.4%, devil's-play 11/11, smoke 40/40, RAG with calibrated gate, full OTel). Not yet production-mature on five of six axes — see `docs/planning/production-maturity-plan.md`.

---

## 2. The non-negotiable rules (read before writing any code)

These are the rules the codebase is built on. Every PR is judged against them. Any AI working on this codebase **must** follow them or the change will be reverted.

### 2.1 Descriptions are SEMANTIC PRINCIPLES, not phrase catalogs
Never write a regex of trigger keywords or a hardcoded list of user phrasings to drive routing, intent, or field selection. The system survives novel phrasings because every classifier reads a *semantic description* (in prose) of what something IS and WHEN it applies. If a new phrasing fails, you rewrite the principle — you do not add another keyword. This applies to: agent descriptions, tool descriptions, field descriptions, prompt rules, policy blocks.

The ONLY allowed fixes when a routing/intent bug surfaces:
1. Add a sentence to a semantic-description corpus.
2. Improve LLM-prompt contrastive examples (semantic principles, not phrase lists).
3. Pass new structured context to an LLM that does not have it today.
4. Fix a regex if and only if it is matching *grammar* wrong (entity-ID format, pronoun shape) — never *meaning*.

### 2.2 LLM is the decision maker, prompts are dynamic
Every routing/classification/response decision goes through an LLM whose prompt is assembled at request time from live context (turns, role, focus, capabilities, locale, schema). No static templates. No code branching on user text content.

### 2.3 Policy layer is mandatory for every LLM call
`src/oneops/policy/` exposes 41 reusable safety blocks across 8 profiles. Every LLM call composes through `compose(Profile.X, ...)`. Never hand-craft a system prompt. The policy layer handles safety, RBAC framing, tenant guards, anti-fabrication.

### 2.4 Tenant isolation is structural, not advisory
`tenant_id` is the first SQL predicate everywhere. Dragonfly keys are tenant-prefixed. NATS subjects are tenant-scoped. OTel labels carry `tenant_id`. Any function that touches data takes `tenant_id` as a required argument.

### 2.5 Single egress through the LLM gateway
Every LLM and embedding call goes through `src/oneops/llm/gateway.py` → LiteLLM proxy at `:4001`. No raw OpenAI/Anthropic SDK calls anywhere else. The gateway enforces per-tenant cost, redaction, retries, timeouts. Grep for raw `openai.` or `anthropic.` outside `src/oneops/llm/` and the answer must be zero.

### 2.6 Observability is not optional
Every code path emits OTel spans. Required attributes on every span: `request_id`, `tenant_id`. Add `agent_id`, `agent_version`, `confidence_score`, `autonomy_level` where applicable. Never log raw user text outside a debug-only path; use `safe_attrs.py` to scrub. NATS hops must propagate W3C `traceparent` via `propagation.py`.

### 2.7 No silent failures
Every failure mode emits a typed error AND an OTel event. The system never returns an empty success on internal failure. Degraded modes are explicit (e.g., `degraded.no_grounding`).

### 2.8 LangGraph-first
For state machines, retries, caching, fan-out, interrupts, checkpointing — use LangGraph primitives (Send, reducers, RetryPolicy, CachePolicy, ToolNode, subgraphs, AsyncPostgresSaver) before writing custom orchestration. Read framework docs before proposing a fix that touches StateGraph nodes, reducers, or Command/Send.

### 2.9 Production-grade testing on every change
Every PR ends with: smoke suite green (81/84 routing baseline), 11-probe devil's-play green, new unit tests for the change, integration test where applicable. No `--no-verify`. No `git push --force`. No `--amend` on previous commits.

### 2.10 No file bloat, no premature abstraction
Edit existing files. Do not create new modules for one-shot helpers. Three similar lines is better than a premature abstraction. No docstrings explaining what the code does (names already do that); only comment the non-obvious WHY.

### 2.12 Don't ask, drive
Stop asking choice menus. Make the call, keep moving. The user will redirect if needed. Default action over deliberation.

### 2.13 Engineering principles
- Read `CLAUDE.md` and this doc → plan → wait for explicit go-ahead before large changes.
- No keyword catalogs anywhere on routing path (restatement of 2.1).
- Explicit fallbacks, strict prompts.
- Surface conflicts and gaps — do not paper over them.
- Devil's-advocate after every fix: "what else of this kind did I miss?"

---

## 3. Repository structure

### 3.1 Top-level
```
POC-5-MW-1/
├── docs/architecture/ARCHITECTURE.md           ← canonical architecture overview
├── docs/runbooks/RUNBOOK.md                ← operator commands & dev-env steps
├── docs/history/MIGRATION.md              ← migration history
├── docs/history/CLEANUP.md                ← cleanup tasks log
├── Makefile                  ← setup / up / test / lint / typecheck / proto
├── docker-compose.yml        ← local dev stack (8 services)
├── pyproject.toml            ← python 3.12, langgraph + langchain-openai + nats-py + asyncpg + redis + otel + protobuf
├── config/                   ← env-default snippets
├── contracts/                ← shared Pydantic schemas / API contracts
├── data/                     ← seed data for local dev
├── docs/decisions/                ← ADRs (architecture decision records)
├── docs/                     ← all human docs (see §3.3)
├── langgraph_docs/           ← vendored LangGraph reference for offline review
├── logs/                     ← runtime logs (gitignored)
├── migrations/               ← SQL migrations (0001 conversation_events, 0002 itsm_schema)
├── ops/                      ← OPS configs (litellm, otel/collector.yaml, otel/prometheus.yaml, otel/tempo.yaml)
├── proto/                    ← protobuf schemas
├── registries/               ← JSON registries (see §3.4)
├── database/                 ← per-service DB slices (schema/embeddings/data/worker) + _lib/ _utils/ _foundation/
├── scripts/                  ← eval / CI / smoke scripts
├── src/oneops/               ← application code (see §3.2)
├── tests/                    ← unit + integration tests
└── dev/                      ← build/codegen + dev helpers (gen_proto.sh, freeze_stopwords.py, migrate_registry_v2.py)
```

### 3.2 `src/oneops/` — application modules

| Module | Purpose |
|---|---|
| `api/` | FastAPI entrypoint + static UI; wires lifespan (registries → gateway → control gate → field embedder → router → executor → workers). |
| `adapters/` | External-system adapters (ticket store, kb store, ITSM connectors). |
| `authz/` | RBAC, ABAC, descriptors, decision cache, token verification, service-account JWT. |
| `codec/` | Protobuf envelopes + generated stubs for NATS payloads. |
| `conversation/` | The **focus-aware LLM control gate** — decides if a turn is in-domain or off-topic given active focus. |
| `errors/` | Typed error hierarchy + structured error responses. |
| `executor/` | LangGraph state graph (`graph.py`), state schema (`state.py`), nodes (`nodes.py` — `update_focus`, `control_gate`, `route`, `run_step`, persist), NATS-bridged step executor. |
| `llm/` | **Single LLM egress.** `gateway.py` is the only place that talks to LiteLLM. Plus `cost.py`, `quota.py`, `redaction.py`, `models.py`, `transport.py`. |
| `observability/` | OTel helpers — `span_helpers.py`, `metrics.py`, `safe_attrs.py`, `propagation.py` (W3C traceparent over NATS), `cache_event.py`. |
| `policy/` | The mandatory policy layer — `blocks.py` (41 reusable safety blocks), `composer.py` (per-profile assembler). |
| `policy_engine/` | OPA-style policy engine for fine-grained rules (separate from `policy/`). |
| `registry/` | Loads + validates JSON registries at boot; provides query API to other modules. |
| `router/` | 6-stage routing pipeline (see §5). Files: `decompose.py`, `rewrite.py`, `retrieval.py`, `conditions.py` (Stage 3 filter), `disambiguation.py` (Stage 4), `entity_id.py`, `signals.py`, `glossary.py`, `language.py`, `plan.py`, `fast_path.py`, `router.py` (orchestrator). |
| `session/` | Session persistence (Dragonfly for recent turns, Postgres for full audit). |
| `tenancy/` | Tenant-id propagation, namespace utilities. |
| `toolrunner/` | Validates and executes tool calls; enforces per-agent tool allowlist. |
| `uc_common/` | Cross-UC shared utilities (formatters, canonical response shapes). |
| `use_cases/uc01_summarization/` | UC-1 handlers, tools, `field_embedder.py` (semantic field matcher), `field_read.py`, `llm_summarizer.py`, `cache.py`. |
| `use_cases/uc03_kb_lookup/` | UC-3 handlers, `kb_embed.py` (hybrid retrieval + relevance scorer), `answer_composer.py`. |
| `use_cases/_shared/` | Shared types for UC modules. |
| `workers/` | Background agent workers (one per agent_id) subscribing on NATS subjects. |

### 3.3 `docs/`

- **`docs/planning/production-maturity-plan.md`** — **READ THIS SECOND.** Gap matrix vs target architecture, 30-item PMG-demonstrable definition of done, P0/P1/P2 roadmap, 2-day cut.
- `docs/pmg-validation/` — 6 PMG-validation docs (01 overview, 02 architecture explanation, 03 use-case deep dives, 04 status, 05 glossary, 06 architecture detailed, 07 Studio future plan).
- `docs/guides/00_overview.md` / `docs/guides/01_stack_components.md` / `docs/guides/02_uc_pack_structure.md` / `docs/guides/07_starting_steps.md` — onboarding sequence.
- `docs/product/BEHAVIOR_CORPUS.md` — the eval corpus of expected behaviours.
- `docs/planning/BUILD_STATUS.md` — phase-by-phase build tracker.
- `docs/architecture/COMPONENT_SPEC.md` — per-component contract.
- `docs/design/` — design notes for past architectural rounds.
- `docs/findings/` — investigation reports.
- `docs/issues/ISS-NNN-*.md` — issue ledger (one file per issue, lifecycle: draft → in-progress → resolved). New issues land here BEFORE fix-mode.
- `docs/observability/architecture_map.md` — the canonical span tree.
- `docs/policies/updated_policy_v2.md` — policy-layer reference.
- `docs/runbooks/` — operational runbooks.

### 3.4 `registries/` — data-driven configuration

These JSON files drive everything. New use cases are added by editing these files, not by writing new Python modules.

| File | Purpose |
|---|---|
| `agent-catalog-registry.json` | The canonical agent catalog. One entry per agent (UC). Fields: `agent_id`, `name`, `description` (semantic, not keywords), `capabilities`, `tools`, `activation_condition`. **Production-maturity P0:** add `version`, `status` (draft/active/deprecated/retired), `lifecycle_stage`, `owner`. |
| `agent-registry.json` | Agent runtime config (per-instance settings). |
| `agent-tool-mapping.json` | Per-`(agent_id, service_id)` tool allowlist. |
| `capability-registry.json` | Capability descriptors used by the router for disambiguation. |
| `role-permission-registry.json` | Role × permission matrix. **P0:** materialize to `(role × tool) → allow/deny` and enforce twice. |
| `router-alias-registry.json` | Alias-to-canonical-id mappings (entity grammar, never semantic). |
| `service-registry.json` | Service definitions (incident, request, problem, change, asset, cmdb, kb). |
| `service-schema.json` | Per-service field schemas (used by the unavailable-field fast-path). |
| `tool-registry.json` | Tool definitions: `tool_id`, `description` (semantic), input/output schemas, side-effect class, risk tier. |
| `v2/` | Next-gen registry format work-in-progress. |

### 3.5 `tests/`

- `tests/unit/` — module-by-module unit tests mirroring `src/oneops/` structure. 73 test files.
- `tests/integration/` — multi-component tests (require docker-compose stack up).
- `tests/fixtures/` — shared fixtures (registries, sample tickets, sample KB articles).

---

## 4. Runtime stack (docker-compose)

| Service | Container port | Purpose |
|---|---|---|
| `postgres` | 5432 | Durable state: tickets, KB, sessions, audit, embeddings. |
| `dragonfly` | 6379 | Cache: recent turns, control-gate decisions, summary cache, idempotency. |
| `nats` | 4222 | Messaging: API → graph_worker → agent_worker. Carries W3C traceparent. |
| `litellm` | 4001 | LLM proxy. Single egress for OpenAI/Anthropic/etc. Per-tenant cost recorded. Master key `sk-1234` in dev. |
| `otel-collector` | 4320 | OTel collector — receives spans from the app, fans out to Tempo + Prometheus. |
| `tempo` | 3201 (api), 4319 (otlp) | Trace store. |
| `prometheus` | 9090 | Metric store. |
| `grafana` | 3001 | Dashboards over Tempo + Prometheus. |

App runs on host port `8000`. Static UI served at `/`.

---

## 5. Routing pipeline (end-to-end request)

A user message hits `POST /api/chat` and flows through:

1. **API ingress (`api/app.py`)** — auth (today: trust token), session resolve, OTel root span.
2. **NATS hop → graph_worker** — W3C `traceparent` propagated via `observability/propagation.py`.
3. **LangGraph `executor/graph.py`** runs the StateGraph:
   1. `executor.load_session` — Dragonfly recent + Postgres tail.
   2. `executor.update_focus` — deterministic entity-ID extraction from current message + carry-forward of `focus_entity_id` / `focus_service_id` state channels.
   3. `conversation.control.classify` — **focus-aware LLM control gate**. Prompt includes "ACTIVE FOCUS RECORD" envelope so it can distinguish a legitimate follow-up ("any data on this") from an off-topic query ("how to fix bluetooth") when the same incident is focused. Refuses out-of-scope before routing runs.
   4. `router.route` — 6 stages, each emits a span:
      - `stage0a.decompose` — split into sub-queries (LLM).
      - `stage0b.rewrite` — focus-aware rewrite (LLM, knows the focused record).
      - `stage2.retrieve` — lexical shortlister over agent catalog.
      - `stage3.filter` — activation conditions + ABAC.
      - `stage3.4.preroute` — deterministic focus-bound shortcut (no LLM).
      - `stage4.disambiguate` — LLM picks the agent given focus + candidate catalog. Returns structured decision.
   5. `executor.run_step` — dispatches to agent_worker over NATS.
   6. UC handler runs:
      - UC-1: `uc01_summarization/field_embedder.py` (cosine match to semantic field descriptions, threshold 0.33) → `field_read.py` for targeted field, or `llm_summarizer.py` for full summary. Cache-aside via `cache.py`.
      - UC-3: `uc03_kb_lookup/kb_embed.py` (FTS + vector RRF + relevance gate at 0.50) → `answer_composer.py` (Python source + LLM-shaped sections, omit-absent).
   7. `executor.persist` — append to session, write trace.
4. Response streams back through NATS → API → client.

Per-trace observability: 26–40 spans, 6 NATS hops, ~10 LiteLLM calls. All visible in Grafana → Tempo at `:3001`.

---

## 6. How to run it (developer workflow)

```bash
cd /home/kevin-parashar/AI-services/POC-5-MW-1
make setup            # creates .venv, editable install
docker compose up -d  # brings up the 8-service stack
.venv/bin/python -m uvicorn oneops.api.app:app --host 0.0.0.0 --port 8000
```

**Critical:** the editable install marker `__editable__.oneops-0.1.0.pth` must point to **this** folder, not a neighbour project. Check with `cat .venv/lib/python3.12/site-packages/__editable__.oneops-0.1.0.pth`.

**Agent worker spawn env var is `AGENT_ID`** (not `ONEOPS_AGENT_ID`).

Tests:
```bash
make test-unit         # full unit suite
make test-integration  # integration (needs docker-compose up)
make lint              # ruff
make typecheck         # mypy
```

Smoke + devil's-play (the two harnesses every change must keep green):
```bash
.venv/bin/python scripts/smoke_routing.py        # baseline 81/84
.venv/bin/python scripts/devils_play.py          # 11-probe adversarial
```

---

## 7. Architecture decision records (ADRs)

`docs/decisions/`:
- **ADR-0001 Codec** — protobuf for NATS envelopes (one wire format, generated stubs).
- **ADR-0002 Vector store** — Postgres `pgvector` (single store, schema-bound), no external vector DB.
- **ADR-0003 Policy engine** — accepted; the mandatory policy layer at `src/oneops/policy/`.
- **ADR-0004 Checkpoint store** — dedicated Postgres for LangGraph `AsyncPostgresSaver` (driven by the 2026-05-16 Supabase data-loss incident; never run `AsyncPostgresSaver.setup()` against a shared schema).
- **ADR-0005 NATS topology** — single 3-node cluster (POC scale). Larger split is open question to manager.

When you make a decision that future engineers must respect, write a new ADR in this folder.

---

## 8. What "production-grade" means here

A change is production-grade if **all** of these hold:

1. Honours every non-negotiable rule in §2.
2. Has unit tests for the new logic.
3. Has at least one integration test if it crosses a process boundary.
4. Smoke suite still passes (81/84 baseline).
5. Devil's-play still passes (11/11 baseline).
6. New OTel spans carry `tenant_id` + `request_id`.
7. New failure modes emit typed errors + OTel events; no silent fallback.
8. `docs/runbooks/RUNBOOK.md` updated if there is an operator implication.
9. `docs/history/MIGRATION.md` updated if there is a migration.
10. New behaviour reflected in `docs/pmg-validation/` if customer-visible.
11. If this is a fix, the parallel issue file under `docs/issues/ISS-NNN-*.md` exists and is updated.
12. If this is a routing-path change, descriptions are semantic (rule 2.1).
13. If this is a new LLM call, it composes through the policy layer (rule 2.3).
14. If this is a new LLM call, it goes through the gateway (rule 2.5).
15. New SQL touches `tenant_id` as the first predicate (rule 2.4).
16. Diff does not include `--no-verify`, `--force`, or `--amend`.

A change is NOT production-grade if any of those fail, regardless of how clever or fast the code is.

---

## 9. What is currently shipped vs. what is on the production-maturity P0 list

**Shipped & verified (do NOT rewrite these):**
- Routing pipeline (6 stages, focus-aware control gate, LLM disambiguator, embedding field matcher)
- Hybrid RAG with calibrated relevance gate (UC-3)
- Full OTel coverage with W3C traceparent over NATS
- Policy layer (41 blocks × 8 profiles)
- Tenant scoping at every layer
- LiteLLM single egress + per-tenant cost recording
- JSON registries loaded & validated at boot
- LangGraph state graph with `focus_entity_id` / `focus_service_id` channels
- UC-1 field-read fast path + 2-hop linked-record traversal
- UC-3 linked-to KB relevance gate (added 2026-05-29)

**P0 — required for PMG sign-off (3-4 weeks at production grade):**
1. Agent lifecycle state machine + `AgentManifest` export/import
2. Front-door JWT verification + signed internal service JWTs
3. Materialized RBAC `(role × tool)` matrix, twice-enforced
4. Prompt-regression CI gate + adversarial probe suite + RAG faithfulness enforcement
5. SLO alert rules + per-tenant cost Grafana board + per-UC synthetic probes
6. Cross-tenant adversarial CI (1000 attempts) + one-shot per-tenant delete
7. CI/CD pipeline scaffold (`.github/workflows/`)

**P1 — should-have (4-10 weeks):** Inbound PII scrub, hash-chained audit, RTBF endpoint, drift detector + explainability store, per-tenant catalog overlay, scaffolding CLI, IaC (Terraform / Helm / ArgoCD), canary + auto-rollback, secret manager + image signing + SBOM.

**P2 — scale-time:** EKS + Istio + Lambda migration, chaos nightly, multi-region DR, Studio author plane, multi-model routing, agent-to-agent autonomy activation.

Full detail in `docs/planning/production-maturity-plan.md`.

---

## 10. How to add a new use case (the DevEx contract)

The platform is registry-driven. A new UC = **data additions + one handler module**, never a rewrite of routing.

1. **Register the agent** in `registries/agent-catalog-registry.json` with: `agent_id`, `name`, semantic `description`, `capabilities`, `version`, `status: draft`, `owner`. (`version` / `status` enforced once P0-#1 lands.)
2. **Map the tools** in `registries/agent-tool-mapping.json` and define each tool in `registries/tool-registry.json` with a semantic description, input/output schema, side-effect class, risk tier.
3. **Add the role permissions** in `registries/role-permission-registry.json`.
4. **Create the handler module** at `src/oneops/use_cases/ucNN_<name>/` with `handlers.py`, `tools.py`, and any UC-specific helpers. Mirror the structure of `uc01_summarization/` and `uc03_kb_lookup/`.
5. **Compose all LLM calls** through `policy.composer.compose(Profile.X, ...)`.
6. **Route all LLM/embedding** through `llm.gateway`.
7. **Span every operation** via `observability.span_helpers`.
8. **Tests:** unit tests under `tests/unit/use_cases/ucNN_<name>/`, an integration scenario under `tests/integration/`, plus updates to the smoke + devil's-play scripts.
9. **PMG doc:** add a new section to `docs/pmg-validation/03-use-case-deep-dives.md`.
10. **No new Python module outside `use_cases/ucNN_<name>/`** unless cross-UC shared code is justified — then it goes in `uc_common/` or `_shared/`.

When P1-#5 (`oneops scaffold uc` CLI) lands, steps 4 + 8 + 9 will be templated.

---

## 11. What to read in what order

1. **This document** (`docs/briefings/PROJECT-BRIEFING.md`) — context + rules.
2. **`docs/planning/production-maturity-plan.md`** — what production-mature means here, gap matrix, roadmap.
3. **`docs/architecture/ARCHITECTURE.md`** — canonical architecture.
4. **`docs/runbooks/RUNBOOK.md`** — operator commands.
5. **`docs/architecture/COMPONENT_SPEC.md`** — per-component contracts.
6. **`docs/pmg-validation/index.md`** then 01 → 07.
7. **`docs/decisions/ADR-*.md`** — why specific tech choices were made.
8. **`docs/policies/updated_policy_v2.md`** — policy layer reference.
9. **`docs/observability/architecture_map.md`** — span tree.

After steps 1-3, an external AI has enough context to do a production-grade change.

---

## 12. Open questions blocking scope (route to manager)

Captured in `docs/planning/production-maturity-plan.md` §G. The three that block scoping hardest:

1. Does PMG sign-off require EKS migration, or sign-off on current infra + roadmap?
2. Bridge Service + Envoy RBAC in scope for this milestone or deferred?
3. Intent ontology — closed taxonomy (violates rule 2.1) or labelling layer over LLM decisions?

---

## 13. Anti-patterns we have already paid for — do not repeat

- **Phrase catalogs as routing logic.** We deleted `_DOC_NOUN_RE`, the chit-chat phrasebook, and a 3-axis embedding classifier whose centroids were prototype sentences (a phrasebook in disguise). All three regressed on novel phrasings. Rule 2.1 is the lesson.
- **`AsyncPostgresSaver.setup()` against a shared schema.** 2026-05-16 incident: LiteLLM Prisma against shared schema dropped app tables. Recovered via backup. ADR-0004 enforces a dedicated checkpoint store.
- **Skipping the policy layer for "just one quick call".** Every regression that follows takes 4× the time to debug. Rule 2.3.
- **Per-message turn caps (`messages[-N:]`).** They break multi-turn intent. Use LangGraph `trim_messages` by token budget + `RemoveMessage` instead.
- **Hot fixes without devil's-advocate.** Every fix is followed by "what else of this kind did I miss?" before claiming done.

---

## 14. Communication conventions for AI collaborators

- One sentence updates at key moments. No running commentary.
- State results and decisions directly.
- File path + line reference when citing code: `src/oneops/router/router.py:142`.
- Cite ADR or rule number when invoking a non-negotiable: "Rule 2.5 (single egress) means…".
- For an exploratory question, 2-3 sentences with a recommendation and the main tradeoff — never a multi-section essay.
- Trust but verify: an agent's summary describes what it intended to do. Check actual diffs before claiming done.

---

**End of briefing.** With §2 internalised, §3 + §5 mapped, and §8 + §10 followed, an external AI can do production-grade work on this codebase from a cold start.
