# Codebase Understanding — Oneops-NextGen V1

> Status: 2026-06-04. Produced from a read-only repo-wide scan. This complements
> (does not replace) the existing `ARCHITECTURE.md`, `PROJECT-BRIEFING.md`,
> `COMPONENT_SPEC.md`. Read those for the *target* contract; read this for the
> *as-built* state and where it diverges.

## 1. What the system is

An AI-services platform for ITSM/ITOM use cases, built on **LangGraph** with a
planner → executor → worker shape, exposed over **FastAPI** (HTTP + WebSocket)
and optionally fanned out over **NATS**. Working end-to-end today across five use
cases (UC-1 summarization, UC-2 similar-tickets, UC-3 KB lookup, UC-5 triage,
UC-8 fulfillment).

## 2. Service boundaries (`src/oneops/`)

| Layer | Packages | Responsibility |
|---|---|---|
| API / ingress | `api/` (`app.py`, `uc02_routes.py`, `uc05_routes.py`, `uc08_routes.py`, `streaming.py`, `nats_invoker.py`) | HTTP/WS routes, request envelope, cache check, response wrapping |
| Routing | `router/` (`decompose.py`, `plan.py`, `retrieval.py`, `intent_classifier.py`, `time_filter_extractor.py`, `conditions.py`, `disambiguation.py`) | decompose → retrieve → filter → disambiguate → assemble plan |
| Execution | `executor/` (`graph.py`, `nodes.py`, `step_runner.py`, `state.py`, `boundary.py`, `nats_step_executor.py`) | LangGraph state machine, wave dispatch, handler invocation, aggregate, persist |
| Workers | `workers/` (`graph_worker.py`, `agent_worker.py`) | NATS subscribers for graph + per-agent execution |
| Use cases | `use_cases/uc0*/` + `_shared/` + `uc_common/` | per-UC domain logic, tools, handlers, contracts |
| Infra adapters | `adapters/` (`postgres.py`, `dragonfly.py`, `nats_client.py`, `nats_resilience.py`, `session_store.py`) | external clients, pooling, resilience |
| LLM | `llm/` (`gateway.py`, `transport.py`, `cost.py`) | single egress, retry/fallback, cost, redaction |
| Cross-cutting | `config.py`, `observability/`, `errors/`, `authz/`, `tenancy/`, `policy/`, `policy_engine/`, `registry/`, `session/`, `embeddings/`, `codec/`, `conversation/`, `toolrunner/` | settings, OTel/structlog, typed errors, RBAC, registries, session memory, embedding worker |

## 3. Entry points (CONTRACTS — see audit §"contracts to preserve")

**HTTP/WS** (`api/app.py` unless noted):
`POST /api/chat`, `/api/chat/stream`, `WS /ws/chat`; `POST /api/fast/{uc_id}`,
`/api/fast/{uc_id}/stream`, `GET /api/fast/{uc_id}/spec`; `GET /`, `/api/health`,
`/api/config`, `/api/identity-options`; session CRUD `POST|GET|DELETE /api/sessions*`,
`GET /api/session/{id}/history`; UC routes: `POST /api/uc02/similar-tickets(+/stream)`,
`GET /api/uc05/queue-summary|queue`, `POST /api/uc05/propose(+/stream)|/decide`,
`POST /api/uc08/create-sr|match(+/stream)|fulfill`, `GET /api/uc08/status/{ritm_id}`.

**NATS subjects**: `oneops.request.chat` (queue `oneops-graph`),
`oneops.agent.<agent_id>` (queue `oneops-agent-<agent_id>`),
`oneops.uc05.triage.propose`, `oneops.uc05.triage.decide`,
`oneops.uc08.fulfill.execute`; reply inboxes `oneops.response.<request_id>`.

**App factory**: `create_app()` → `build_app()` (`app.py`), lifespan `_lifespan`
boots registry/graph/routers/LLM gateway/session stores/NATS workers.

## 4. Request flow (chat turn)

`POST /api/chat` → principal from headers → request/session id → **chat-turn cache
check** → envelope → `_run()`:
- `UC_INVOKER_MODE=nats` → `nats_invoke()` publishes to `oneops.request.chat`,
  awaits reply (60s); `GraphWorker` runs the turn and replies.
- else (default `local`) → `run_turn(graph, envelope)` → LangGraph:
  `load_session → update_focus → control_gate → route → wave⇄run_step (Send fan-out)
  → aggregate → persist`.
- Result wrapped in `TurnResponse(door, final_status, final_response, step_results,
  session_id, request_id, trace_id, latency_ms)` → **cache write** → 200.

Fast-path (`/api/fast/{uc_id}`) pre-routes a plan and sets `entry_mode="fast_path"`,
skipping the `route` node. WebSocket and stream variants reuse the same `_run`.

## 5. AI/LLM flow

Single egress `LlmGateway.call()/.embed()` (`llm/gateway.py`): quota → PII redaction
→ transport with bounded retry (default 2) on `(LLMTimeoutError, LLMRateLimitError,
LLMUpstreamError)` → optional fallback model → cost accounting per (tenant, model)
→ `LlmResponse`. Per-UC fallbacks exist (UC-1 deterministic summary; UC-3
deterministic composer; UC-5 graceful skip). Output validation is per-UC and
uneven (UC-1 strong; UC-5 assumes schema).

## 6. Messaging, cache, DB

- **NATS** (`adapters/nats_client.py`, `nats_resilience.py`): singleton conn, W3C
  traceparent headers, bounded retry + per-(subject,tenant) circuit breaker, all
  env-tunable (`NATS_RETRY_*`, `NATS_BREAKER_*`).
- **Cache** (Dragonfly, `adapters/dragonfly.py`, `dragonfly_ops.py`): per-loop
  singleton, 256 KiB value cap, single-flight stampede guard, full metric set,
  graceful degradation. Session keys `session:{sid}`/`focus:{sid}`/`canonical:{sid}`
  and `oneops:session:*:{tenant}:{...}`; chat-turn cache keyed by
  tenant+user+role+session+message hash, versioned by `PIPELINE_CACHE_VERSION`.
- **Postgres** (`adapters/postgres.py`): per-loop singleton pool, `command_timeout=30s`,
  statement cache off (pgbouncer-safe), `BaseRepository._fetchone/_fetchall/_execute`.
  Migrations `0001..0009`. Checkpointer via `LANGGRAPH_POSTGRES_URL`.

## 7. Data-access shape (critical for the DAL track)

- **Behind shared ports**: UC-1 (`TicketStore`), UC-3 (`KbStore`) — `use_cases/_shared/`.
- **Raw SQL / own pool (bypass)**: UC-2 (`uc02_similar_tickets/core.py`,
  `similarity_search.py`), UC-5 (`uc05_triage/retrieval/similarity_search.py`),
  UC-8 (`uc08_fulfillment/db.py`, ~15 queries). All parameterized + tenant-scoped.
- This split is the **known structural gap**: production routes all DB access through
  a system-wide **Data Access Layer (DAL)**. See `docs/risk-register.md` and the
  memory note — DAL work is **deferred until its contract is confirmed**; do not
  consolidate raw SQL behind ports until then.

## 8. Config, observability, errors, security (as-built)

- **Config**: typed `Settings` (pydantic-settings, `config.py`) is the source of
  truth, BUT ~114 `os.getenv` calls are scattered across ~32 files (UC modules,
  executor, api). Some are intentional runtime flags (e.g. focus-migration).
- **Observability**: structlog + OpenTelemetry fully wired; trace/span ids in every
  log; `observability/safe_attrs.py` hashes text by default (PII-safe).
- **Errors**: 24 typed exceptions under `OneOpsError` (`errors/`), each with a
  `.code`; ~72 `raise ... from exc`; ~60 broad `except Exception` (mostly graceful
  degradation, `# noqa: BLE001`).
- **Security**: no hardcoded prod secrets in `src/`; `.env` is gitignored. Open
  items: a real key sits in the local (untracked) `.env`; `AUTHZ_JWT_SECRET` has no
  fail-fast; a few endpoints echo `{exc}` to clients.

## 9. Testing & runtime (as-built)

- pytest (`asyncio_mode=auto`), markers `unit/integration/stress/slow`, cov fail_under 70%.
- **Marker gap**: only ~19 tests carry `@pytest.mark.unit`, so `pytest -m unit`
  (the CI gate's unit stage) runs ~1% of the ~1591 tests; the full suite runs by
  directory (`pytest tests/unit/`). See testing-strategy + audit.
- Run: `uvicorn` via app factory; `docker-compose.yml` (8 services) and the isolated
  `docker-compose.v1.yml` on shifted ports. Local CI gate `scripts/ci.sh` (6 stages;
  smoke+devils are placeholders). No Dockerfile/k8s yet.

## 10. Critical files (touch with care)

`api/app.py` (ingress + lifespan, large), `executor/graph.py` + `nodes.py` +
`step_runner.py` (orchestration + reducers), `router/decompose.py` + `plan.py`
(routing + data-flow binding), `llm/gateway.py` (single egress), `adapters/*`
(shared clients), `config.py` (settings), `registry/*` (agents-as-data).
