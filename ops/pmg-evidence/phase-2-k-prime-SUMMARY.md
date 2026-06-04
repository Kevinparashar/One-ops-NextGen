# UC-5 Production Wiring — Final Summary

**Date:** 2026-05-29
**Scope:** Wire UC-5 Triage onto the full production substrate (LiteLLM proxy + Policy composer + OTel tracing + NATS agent-to-agent + LangGraph orchestration). Preserve the JSON-store demo pattern; zero new DB writes.

---

## Verdict

**UC-5 Triage is production-grade and live.** All 8 phases complete, 387 UC-5 unit tests green, full end-to-end demo cycle verified against running infrastructure, zero regressions to UC-1, UC-3, multi-turn chat, or any other system module.

---

## Phase scorecard

| Phase | Title | Items shipped | Devil's-play probes | Status |
|---|---|---|---|---|
| 1 | Substrate adapters + policy | 4 factories, FEATURE_AGENT / FEATURE_AGENT_JSON profiles | 9 | ✅ |
| 2 | Observability + traceparent | spans on 7 boundaries + W3C helper | 6 | ✅ |
| 3 | LangGraph orchestration + runner | state, graph, runner builder | 8 | ✅ |
| 4 | NATS agent-to-agent | TriageAgent, dispatcher, subjects locked | 5 | ✅ |
| 5 | Integrated-stack devil's-play | 10 cross-layer adversarial probes | 10 | ✅ |
| 6 | Live e2e + Tempo + Grafana | runner wired in app lifespan; curl validates the chain | 10 | ✅ |
| 7 | Full no-regression sweep | UC-1, UC-3, chat, router, multi-turn | live + unit | ✅ |
| 8 | Evidence + handoff | this doc + runbook + per-phase logs | — | ✅ |

**Total devil's-play probes:** 48 (all green).
**Total UC-5 + UC-5 API tests:** 387.
**Full pytest sweep:** 1227 passing, 10 pre-existing failures unchanged.

---

## Architecture (production stack, live)

```
Browser (http://127.0.0.1:8765/)
  │ headers: x-tenant-id, x-user-id, x-role
  ▼
FastAPI app (oneops-uc05)
  │ span: ai.request   ┌─────────────┐
  │                    │ /api/uc05/* │
  ▼                    └─────────────┘
RBAC dependency
  │ technician_l1 / technician_l2 / triage_desk / admin
  ▼
TicketStore (JsonFixtureStore — demo, zero DB writes)
  │ span: uc05.store.get_ticket / uc05.store.apply
  ▼
LangGraph (UC-5 build_runner)
  │ span: uc05.runner.invoke
  ▼
  ├─ uc05.tool.check_duplicates
  │     ├─ uc05.adapter.tiebreak → llm.call  (FEATURE_AGENT policy)
  │     ├─ uc05.adapter.tag      → llm.call  (FEATURE_AGENT_JSON policy)
  │     └─ retrieval substrate    → llm.embed (text-embedding-3-large)
  ├─ uc05.tool.recommend_assignment    ‖ parallel
  ├─ uc05.tool.prioritize              ‖
  │     └─ uc05.adapter.prioritize → llm.call (FEATURE_AGENT_JSON policy)
  └─ uc05.assembly
        ↓
   Proposal (16 fields)
        │
        ▼ (on Yes click → /decide)
   uc05.apply
        │ span: uc05.apply
        ▼
   store.apply → demo_tickets.json updated
        │
        ▼ (parallel NATS pub: oneops.uc05.triage.applied for SIEM)
   Outcome(applied)
```

All gateway calls inherit:
* Per-tenant cost tracking
* Retry + fallback model
* PII redaction (verified end-to-end)
* `llm.call` / `llm.embed` spans

All policy prompts include:
* `COMMON_SAFETY_RULES`
* `AGENT_FOCUS_DIRECTIVE`
* `REGISTRY_GROUNDING_POLICY`
* `OUTPUT_SCHEMA_POLICY` (JSON variants)

---

## Infra status (8 host ports — cleaned)

| Service | URL | Purpose |
|---|---|---|
| Frontend (UC-1 + UC-3 + UC-5 API) | http://127.0.0.1:8765/ | App |
| Grafana | http://localhost:3041/ | Dashboards (login: oneops/oneops) |
| Tempo query | http://localhost:3401/ | Trace search |
| LiteLLM proxy | http://localhost:4301/ | Single LLM egress |
| OTel collector | http://localhost:4620/ | OTLP HTTP receiver |
| NATS | nats://localhost:4623 | Agent broker |
| Postgres (local) | localhost:5735 | (LiteLLM optional) |
| Prometheus | http://localhost:9391/ | Metric queries |
| Dragonfly | localhost:6680 | Cache |

**Cleaned 4 dead host ports** during Phase 6: 4619 (Tempo OTLP duplicate), 4621 (OTel gRPC unused), 8689 (Prom scrape — internal only), 8623 (NATS monitor — internal only).

---

## Live trace evidence

* Tempo confirmed receiving spans: `service=oneops-uc05`, `span=ai.request`, durations 0-9154 ms.
* Span tree per propose call: `ai.request → uc05.tool.* → uc05.adapter.* → llm.call/llm.embed`.
* Span tree per decide call: `ai.request → uc05.apply → uc05.store.apply`.
* Prometheus scrape target `nextgen-otel-collector:8889/metrics` health=up.
* Grafana datasources `Prometheus` + `Tempo` wired to docker-internal hostnames.

---

## What is NOT in this round (and why)

* **Frontend (Section K)** — backend complete and verified via curl, but UC-5 buttons are not yet added to `static/index.html`. ~45 min to add when prioritised.
* **NATS durable propose/decide hops** — agent + dispatcher are wired but the API still calls the runner directly when `UC_INVOKER_MODE=local`. Setting `UC_INVOKER_MODE=nats` switches it on; the agent will pick up `oneops.uc05.triage.{propose,decide}` automatically.
* **DbStore swap** — JsonFixtureStore is the configured store. Swapping in DbStore is a one-line change in `app.py` lifespan when the production DB is ready.
* **Production model overrides** — defaults to `gpt-4o-mini` for chat + `text-embedding-3-large` for embed. Swap via the LiteLLM config or per-call `model=` overrides.

---

## Files added in K' (Phases 1-8)

| File | Phase | Lines |
|---|---|---|
| `src/oneops/use_cases/uc05_triage/adapters.py` | 1 | 252 |
| `src/oneops/use_cases/uc05_triage/traceparent.py` | 2 | 80 |
| `src/oneops/use_cases/uc05_triage/state.py` | 3 | 50 |
| `src/oneops/use_cases/uc05_triage/graph.py` | 3 | 130 |
| `src/oneops/use_cases/uc05_triage/runner.py` | 3 | 130 |
| `src/oneops/use_cases/uc05_triage/agent.py` | 4 | 150 |
| `src/oneops/use_cases/uc05_triage/nats_dispatcher.py` | 4 | 90 |
| Tests (5 new files) | 1-5 | ~900 |
| `src/oneops/api/app.py` (UC-5 lifespan wiring) | 6 | +50 |
| `docker-compose.yml` (port cleanup) | 6 | net -4 ports |
| `ops/otel/collector.yaml` (Tempo hostname) | 6 | 1 line |
| `ops/otel/prometheus.yaml` (scrape target hostname) | 6 | 1 line |
| `ops/grafana/provisioning/datasources/datasources.yaml` | 6 | 2 lines |

---

## Pre-existing baseline failures (unchanged across all 8 phases)

```
tests/unit/executor/test_handler_step_executor::test_agent_without_fast_path_fails_loud
tests/unit/router/test_entity_id::test_extract_catches_a_bare_prefix_with_no_number[summarize inc please]
tests/unit/router/test_retrieval_decompose_rewrite::test_retriever_skips_agents_with_no_overlap
tests/unit/test_policy_wiring::test_disambiguator_llm_call_carries_policy
tests/unit/use_cases/test_kb_store::test_postgres_backend_fails_loud_not_silent
tests/unit/use_cases/uc01_summarization/test_tools (4 tests)
tests/unit/use_cases/uc03_kb_lookup/test_handlers::test_get_kb_article_out_of_audience_is_not_found
```

These predate this round of work and remain on the team's backlog as task #18.

---

## Per-phase logs

| Phase | Log file |
|---|---|
| 1 | `ops/pmg-evidence/phase-2-k-prime-phase-1-adapters.log` |
| 1 dp | `ops/pmg-evidence/phase-2-k-prime-phase-1-devils-play.log` |
| 2 | `ops/pmg-evidence/phase-2-k-prime-phase-2-observability.log` |
| 3 | `ops/pmg-evidence/phase-2-k-prime-phase-3-langgraph.log` |
| 4 | `ops/pmg-evidence/phase-2-k-prime-phase-4-nats.log` |
| 5 | `ops/pmg-evidence/phase-2-k-prime-phase-5-integrated-devils-play.log` |
| 6 | `ops/pmg-evidence/phase-2-k-prime-phase-6-live-e2e.log` |
| 7 | `ops/pmg-evidence/phase-2-k-prime-phase-7-no-regression.log` |
