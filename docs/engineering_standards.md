# AI-service — Engineering Standards

The standards every change is judged against. They extend the existing canonical rules (`docs/briefings/PROJECT-BRIEFING.md §2`) with patterns extracted from the reference repositories (`docs/reference_repo_analysis.md`). Each standard cites its source so the *why* is traceable. **Rule 0 (production-grade, root-cause-only) governs all of them.**

---

## 1. Folder structure

**Standard.** Organize by **bounded context (domain), not file type.** Each use case is a package owning its router, schemas, service logic, dependencies, and exceptions. Keep the FastAPI entrypoint thin.

```
src/oneops/
  api/
    app.py            # app construction ONLY (target: « 500 LOC)
    lifespan.py       # all startup wiring (gateway, session, cache, checkpointer)
    interrupt.py      # ONE interrupt capture/persist/resume helper (both paths)
    routers/          # per-domain routers: chat.py, fast.py, uc02.py, uc08_approval.py …
  router/             # the 4-stage funnel (decompose/rewrite/retrieve/filter/disambiguate)
  executor/           # LangGraph graph, nodes, step_runner, state, entity_elicitation
  registry/           # agents/tools as DATA (loader, service, store)
  toolrunner/         # handler_ref → callable resolver + runner
  llm/                # gateway (the Model/Provider port), models, transport
  policy/ policy_engine/ authz/   # RBAC/ABAC, redaction, guardrails
  session/            # durable conversation memory (cold log + hot window)
  db/                 # the DAL boundary (port + adapter) — single data seam
  observability/      # telemetry_handler.py (the ONE emission boundary), logger, metrics
  adapters/           # NATS, integration adapters (IntegrationAdapter Protocol)
  use_cases/          # uc01/uc02/uc03/uc05/uc08 + _shared
  config/             # per-concern typed settings (gateway, cache, nats, db, otel)
```

- **`app.py` is for app construction only** — wiring belongs in `lifespan.py`, routes in `routers/` (FastAPI guide §4). The current 2,570-line `app.py` is the explicit anti-pattern to retire (roadmap C1).
- **`run.py`/entrypoints stay wiring-only**; real logic lives in named internal modules (OpenAI SDK `AGENTS.md` §2.1).
- **Database operations** live under `database/` as per-service vertical slices (existing convention) and are reached at runtime only through the `db/` DAL boundary.
- **No deep cross-domain imports** (`from src.a.b.c.d import …`); import the module, not internals (FastAPI guide §4).

---

## 2. Naming conventions

| Thing | Convention | Source |
|---|---|---|
| Python modules/functions | `lower_snake_case` | ruff/PEP8 |
| DB tables | singular `lower_snake`, module-prefixed (`request_item`), `_at`/`_date` time suffixes, same FK column name everywhere | FastAPI guide §4 |
| Span names | `gen_ai.*` semconv: `"{operation} {model}"` for LLM, `"execute_tool {name}"`, `"invoke_agent {name}"`; keep custom stage spans (`router.stage4`) as scaffold | OTel semconv §3.1 |
| Metric names | `ai.<area>.<thing>.<unit>` (e.g. `ai.llm.token.usage`); two GenAI histograms, no metric-per-thing | OTel §3.1 |
| Agent/tool ids | stable `id` (dispatch key) + human `name` (label); never reuse an id | Agno teams §1.6 |
| Intent tokens | closed enum (`summary/field_read/similar_search/kb_search/action/…`) | structured outputs §2.4 |
| Env vars | unchanged across refactors (contract); grouped by concern | FastAPI §4 |
| Feature flags | `ONEOPS_<FEATURE>_ENABLED`, default-off for new, read per-call via `config._parse_flag` | existing convention |

**Identical path-variable names across routes** (`ticket_id` everywhere) so shared dependencies compose (FastAPI guide §4).

---

## 3. Service boundaries

**Each boundary is a narrow port with a single owner. Cross only through the port.**

| Boundary | Port | Rule |
|---|---|---|
| LLM | `llm/gateway.py` — `Model`/`ModelProvider` shape (`get_response`/`stream_response`/`get_retry_advice`) | **Single egress.** No code calls a provider directly. Policy mandatory per call. Retry-advice encapsulated on the model. (OpenAI SDK §2.3; rule §2.5) |
| Data | `db/dal.py` — one injected boundary | **All reads/writes go through it.** It enforces tenant scope, redaction, query metrics, caching. No raw asyncpg in UC handlers. (FastAPI §4; roadmap C2) |
| Telemetry | `observability/telemetry_handler.py` | **All spans/metrics/events emitted here.** Call sites use `handler.start_*()`, never `tracer`/`meter`. (OTel §3.1) |
| Tools | `toolrunner` resolver — `(context, json_args) → output` | Uniform invoker; identity in `context`, never from LLM-chosen args. (OpenAI SDK §2.1; rule §2.4) |
| HITL | `api/interrupt.py` + `executor/nodes` interrupt helpers | One place owns capture/persist/resume for both interrupt paths. (Temporal §5; gap #8) |
| Async work | NATS + pgmq + worker | Durable queue for retriable seconds-to-minutes work; never `BackgroundTasks` for anything you'd page on. (FastAPI §4) |

**Identity rides in runtime context, not graph state and not LLM-supplied args** — a tenant-isolation boundary (LangGraph/Agno §1.4).

---

## 4. Agent implementation standards

Agents are **data, not code** (rule §2.1; OpenAI SDK §2.1; Agno §1).

1. **An agent is a registry record:** instructions + tool allowlist + activation card (`use_when`/`not_when`/conditions) + model + abac/role gates. No bespoke per-agent Python.
2. **Tools are typed handlers; the schema is derived, not hand-written.** Generate `params_json_schema` from the handler's typed signature + docstring; the docstring `Args` block *is* the LLM contract (OpenAI SDK §2.1). Pydantic `Field` constraints flow into validation.
3. **Tool surface is filtered by role at run start** (RBAC/tenant/feature), not post-hoc rejection (OpenAI SDK §2.1 B4; rule §2.1).
4. **Structured outputs / closed enums** for any LLM result that feeds deterministic code (routers emit a `Literal`; planners emit a typed plan) (OpenAI SDK §2.4).
5. **Side effects only after `interrupt()`**, every action tool **idempotent** (deterministic key) and isolated for replay safety (LangGraph §1.2; Temporal §5). **This is non-negotiable** for any interrupting tool.
6. **Fail-closed write-gates:** an approval/confirmation defaults to `cancel`; never auto-approve (rule §2.7; §1.2).
7. **No keyword routing, no phrase catalogs, no hardcoded ids** anywhere on the routing or resolution path (rule §2.1/§2.2). Resolution against real data + LLM judgment, not lookup tables.
8. **Retrieve-then-LLM-decide** for routing; keep shortlists tight (≈<10 candidates per disambiguation) (OpenAI SDK §2.1 B6).

---

## 5. Prompt design standards

1. **Principle-based, not phrase catalogs.** A prompt states *how to decide* (dimensions/criteria), never a `phrase → answer` table (rule §2.1). The slot-filling picker prompt is the reference example.
2. **Closed-output contracts.** Strict JSON / enum outputs with explicit "when unsure, return null/none" — fail to a safe value, never guess (rule §2.7; §2.4).
3. **Stable system prompt for cache.** Keep the cacheable system message stable; put per-turn dynamic anchors (today's date, candidates) in the user message (LangGraph prompt-cache; OTel hash-dedupe §3.1).
4. **Grounded, no fabrication.** Decisions made against injected real data; reject any id/value outside the grounded set (rule §2.7).
5. **Versioned.** Prompts carry a version; the version is recorded on the LLM span so regressions are attributable (Langfuse §3.2; roadmap E3).
6. **No hardcoded business data** (ticket ids, tenants) in prompts — examples are format hints or category anchors only (rule §2.1).
7. **`nostream` internal calls** (router/planner/judge) when streaming ships (LangGraph §1.5).

---

## 6. Observability requirements

Every change that adds a code path must be observable.

1. **Emit through the one `TelemetryHandler`** — never raw `tracer`/`meter` at call sites (OTel §3.1).
2. **Cardinality firewall (mandatory):** identity (`tenant_id`, `session_id`, `ticket_id`) goes on **spans only**; metrics are labeled only by bounded enums (operation, provider, model, status, error.type) (OTel §3.1). Reviewers reject any high-cardinality metric label.
3. **GenAI semconv on LLM/tool/agent legs** (`gen_ai.request.*`, `gen_ai.response.*`, `gen_ai.usage.*` incl. **cache + reasoning tokens**), values from the semconv enum module, not string literals (OTel §3.1; rule never-hardcode).
4. **Context propagation across async + NATS:** contextvar attach/detach around manual spans; inject/extract `traceparent` on NATS messages (OTel §3.1).
5. **Cost = tokens on the span, dollars derived downstream** from a versioned price table; output tokens include thinking tokens (OTel/Langfuse §3.1/§3.2).
6. **PII discipline:** spans carry refs not bodies (content capture default-off in prod, externalized); checkpoints encrypted (LangGraph §1.3, OTel §3.1).
7. **Telemetry never throws into the request path** — every emit site exception-isolated (OTel §3.1 J2).
8. **Session linkage:** stamp `session_id` + `conversation.id` + tenant/user on the root trace for free multi-turn replay (Langfuse §3.2).
9. **Metrics independent of span sampling** — record cost/token metrics even when a trace is sampled out (OTel §3.1).

---

## 7. Testing requirements

Production-grade testing on every change (rule §2.9).

1. **Deterministic LLM tests via `FakeGateway`.** All LLM calls go through the gateway port; tests inject a fake that scripts per-turn outputs and captures `last_turn_args`. A pytest marker fails any accidental real-model call (OpenAI SDK §2.4; roadmap E1).
2. **Resume-double-execution test per interrupting flow:** run → interrupt → resume → assert each side effect happened **exactly once** (LangGraph §1.2; the single most important reliability regression).
3. **Idempotency tests:** redeliver/retry a fulfillment task → assert no double-apply (Temporal §5).
4. **Compensation tests:** inject a mid-wave failure → assert reverse-order compensation + call counts (Temporal §5).
5. **Real DB in integration tests**, not mocks; tests read **real seeded state**, never hand-mirrored expectations (FastAPI §4; rule §2.9).
6. **Override dependencies in tests** (`app.dependency_overrides`), don't monkeypatch internals (FastAPI §4).
7. **Time-skipping for timers** (SLA/retry-backoff/approval-wait paths) so they're tested in milliseconds (Temporal §5).
8. **Structural assertions over text:** assert routing enums, state-history node sequences, and stream events — not fuzzy prose (LangGraph §1.7; OpenAI SDK §2.4).
9. **Replayer CI gate** for executor-state-shape changes (Temporal §5).
10. **Six-gate DONE rule** (existing): build + smoke + unit + integration + devils-play + edge before a change is "done."
11. **CI order:** `format → lint → typecheck → tests` (ruff check+format, mypy, pytest); the `ci.sh --fast` gate is the contract (OpenAI SDK §2.4; existing).

---

## 8. Deployment requirements

1. **Stateless app replicas:** all durable state in Postgres (checkpointer + business + session cold log), Dragonfly (caches + hot window), NATS — so the FastAPI app scales horizontally.
2. **Config via typed per-concern settings + an `ENVIRONMENT` enum**; secrets via env-loaded settings; never hardcoded (FastAPI §4; rule never-hardcode).
3. **Workers are separate processes** (`python database/<service>/worker.py`), not started by the API; scale independently.
4. **Feature flags default-off for new features**, flipped per environment via config; behavior with the flag off must be byte-for-byte unchanged.
5. **`/docs` (OpenAPI) env-gated** — exposed only in dev/staging (FastAPI §4).
6. **Checkpointer is Postgres in prod** (auditability), in-memory only in dev; guard at boot that interrupts require a checkpointer (LangGraph §1.3; error taxonomy §1.7).
7. **Durable-state versioning:** any serialized paused/approval payload carries a schema version with migration notes; deploy with the replayer gate green (OpenAI SDK §2.3; Temporal §5).
8. **Observability stack required in every environment:** OTel exporter, Langfuse keys, Grafana dashboards; cost/token metrics flowing per tenant. A deploy without telemetry is not production.
9. **Migrations:** descriptive, reversible, dated/numbered slugs; additive-first (triggers fire on INSERT *and* UPDATE; existing convention). No destructive schema change without a back-out.
10. **Rollback posture:** every feature on its own flag + commit; the dead-code/cleanup and feature commits stay separable and revertible (demonstrated in the current branch).

---

## 9. Review checklist (apply to every PR)

- [ ] Behavior/contract unchanged unless the PR is explicitly a feature; new behavior is flag-gated default-off.
- [ ] No hardcoded rules/ids/maps/phrases on any routing/resolution/prompt path (rule §2.1).
- [ ] All LLM calls through the gateway port; no direct provider calls (rule §2.5).
- [ ] All data access through the DAL; no raw asyncpg in handlers (roadmap C2).
- [ ] Side effects only after any `interrupt()`; action tools idempotent (LangGraph §1.2).
- [ ] Telemetry through the handler; no tenant/session on metric labels; semconv on LLM legs.
- [ ] Tests: FakeGateway-deterministic, resume-double-execution where relevant, real-seed integration; six-gate DONE.
- [ ] Identity from runtime context, never from LLM-supplied args.
- [ ] PII not added to checkpoints/traces unencrypted/inline.
- [ ] One owner per boundary touched; no new cross-domain deep imports; no new mirrored constants.

> These standards are the steady-state target. The `docs/refactor_roadmap.md` sequences the migration from today's state to here, without behavior changes.
