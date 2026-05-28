# RUNBOOK.md — OneOps AI Engine, on-call procedures

Operational procedures for the POC-5-MW engine. Companion to `ARCHITECTURE.md`
(what it is) and `BUILD_STATUS.md` (what is built).

---

## 1. Find a trace

Every turn is one trace rooted at the `oneops.request` span; every node, tool,
LLM, and policy decision is a child span — no orphans (verified, P9).

- Each structured log line carries `trace_id` + `span_id`. Grep the log
  aggregator for the `trace_id`, then open it in the OTel backend
  (Tempo/Jaeger) pointed to by `OTEL_EXPORTER_OTLP_ENDPOINT`.
- A turn with no `OTEL_EXPORTER_OTLP_ENDPOINT` set still records spans
  in-process but ships nothing — set the endpoint to see traces.
- Raw user text is **never** in a span. `oneops.message_hash` correlates a
  trace to a message without exposing it. `OTEL_CAPTURE_TEXT=true` opens a
  bounded debug window — dev tenants only, never production.

## 2. Spot a runaway tenant (cost)

- `CostTracker` records per-tenant per-model spend; it emits the
  `ai.llm.cost_usd_micros{tenant_id,model}` and `ai.llm.tokens.total` counters.
- Dashboard on those by tenant. A tenant spiking → check the
  `ai.requests.total{tenant_id}` rate and `ai.router.outcome.total`.
- Hard backstop: `QuotaGuard` — set a per-tenant limit with
  `set_tenant_limit(tenant_id, n)`; over-budget calls raise
  `QuotaExceededError` and the gateway refuses them.

## 3. Roll back an agent or tool version

The registry is versioned; rollback is data, not a deploy.

- Each agent/tool has versions; one is `active`. To roll back:
  `RegistryService.agents.activate(agent_id, prior_version)` — it re-activates
  the older version and demotes the current one to `retired`.
- Old versions are never deleted until explicitly retired, so rollback is
  always available. No redeploy, no code change.

## 4. Change policy without a redeploy

The policy engine is data-driven (ADR-0003).

- Edit `registries/v2/policy_rules.json` (bump its `version`).
- Signal a reload — `PolicyEngine.reload()` re-reads the file and the new
  rules take effect immediately. No code change, no restart.
- Verify: the next `policy.evaluate` span carries the new `policy.version`.
- A `canned`-effect rule's `canned_response` is served verbatim at the
  matching touchpoint — that is how compliance wording is changed safely.

## 5. Rotate the LLM gateway / provider key

- Provider keys live only in the LiteLLM proxy (and the secret manager) —
  the application never holds them (CI gate `test_no_direct_provider`).
- Rotate the key in the secret manager, restart the LiteLLM proxy. The
  application's `LlmGateway` is unaffected — it speaks HTTP to the proxy.
- `AUTHZ_JWT_SECRET` (internal service tokens) rotates the same way: update
  the secret manager, roll the services. Tokens are short-lived (5 min
  default) so the cutover window is small.

## 6. Drain a NATS subject (microservice mode)

- Subjects are `oneops.<tenant>.uc.<agent>.<op>`. To drain one agent's
  traffic: stop that agent service's consumers (the NATS queue group); NATS
  buffers / the producers see no consumer.
- JetStream durable streams (action work) retain messages — a new consumer
  picks up where the last left off; idempotency keys (Dragonfly) make
  re-delivery safe.

## 7. Common failure signatures

| Symptom | Where to look | Likely cause |
|---|---|---|
| Turns return `final_status=failed` | `executor.run_step` spans, `step_results[].error` | a tool handler or dependency is down |
| Turns return `clarification` for valid asks | `executor.route` span, router diagnostics | retrieval/disambiguation miss — check the registry catalog |
| `policy_denied` outcomes spike | `policy.decision` logs | a policy rule change — check `policy_rules.json` |
| A tool times out | `toolrunner.run` spans (`tool.status=timeout`) | slow downstream — raise the tool's `timeout_ms` or fix the dependency |
| `QuotaExceededError` | `llm.quota_exceeded` logs | a tenant over budget — raise the limit or investigate the spike |
| Whole turn raises | should not happen — chaos drills prove degrade-not-crash | file a bug; the pipeline is exception-contained by design |

## 8. Resume a stuck / interrupted run

- Action steps pause on `interrupt()` for approval. A turn waiting for
  approval is resumed with `Command(resume={"approved": true|false})` on the
  same `thread_id`.
- A crashed run resumes from the last checkpoint on the next invoke with the
  same `thread_id` (= `session_id`). In production the checkpointer is the
  dedicated Postgres database (ADR-0004) — never the shared app DB.

## 9. Known post-build follow-ons (not yet done)

- **UC tool-handler porting** — UC-1 / UC-3 tool *logic* still lives in the
  old `use_cases/` (`@tool`-decorated). Port each to the new
  `(args, ctx) -> result` shape and register it via `HandlerResolver`. Until
  then the executor runs on `EchoStepExecutor` / `EchoTransport`.
- **Old-code removal** — once the handlers are ported, delete the superseded
  old `tools/`, `use_cases/`, `gateway/`.
- **Live-infra validation** — load/chaos at 10x scale against real NATS /
  Postgres / LLM, and the soak test, are the operator's pre-prod gate.
