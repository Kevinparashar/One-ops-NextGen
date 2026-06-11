# AI-service — Architecture Review

A qualitative evaluation across six axes, each graded **A–E** with reasoning, strengths, weaknesses, and references to the patterns in `docs/reference_repo_analysis.md` and the findings in `docs/production_gap_analysis.md`. The review assumes a production system serving enterprise customers.

**Overall posture.** AI-service is a *principled* platform whose architecture is well above the median for its stage on **design intent** (agents-as-data, semantic routing, fail-closed approvals, single LLM egress, config-as-code, real observability). Its risk is concentrated in **durable-execution correctness under resume**, **distributed-systems hygiene**, and **structural maintainability of a few hub modules** — i.e., engineering-discipline gaps, not product or conceptual gaps.

| Axis | Grade | One-line |
|---|---|---|
| Maintainability | **B−** | Excellent principles + registry-as-data; dragged by a 2,570-line `app.py`, fragmented data access, mirrored constants |
| Scalability | **B+** | Retrieve-then-decide routing scales to 100+ agents; subgraph path available; metric cardinality + DB locality are the ceilings |
| Reliability | **C+** | Strong checkpointer/retry foundations; resume-side-effect + idempotency + compensation correctness unproven and undertested |
| Security | **B** | Structural tenant scope + single egress + fail-closed approvals; PII-in-checkpoints/traces + raw-SQL bypass + prompt-injection surface |
| Observability | **B** | Real OTel span tree + Langfuse + per-tenant cost; NATS trace propagation + cardinality + cost-token-completeness gaps |
| Performance | **B** | Aggressive multi-layer caching + measured RCA; LLM-call count + remote-DB locality dominate cold-turn latency |

---

## 1. Maintainability — **B−**

### Code organization
- **Strength:** clear top-level package boundaries (`router`, `executor`, `registry`, `toolrunner`, `llm`, `policy`, `session`, `use_cases`, `observability`). The **registry-as-data** model (agents/tools as JSON → DB → embeddings) is the production form of the OpenAI SDK's agent dataclass (`reference §2.1`) and the strongest single maintainability asset — adding an agent is a data change, not code.
- **Weakness:** `api/app.py` at **2,570 LOC** is a coupling hub (startup wiring + routes + static frontend + executor boot + fast-path + two interrupt paths). This is the classic FastAPI anti-pattern the guide warns against (`reference §4.2`; gap #8). `nodes.py` (1,509) and `uc08 tools.py` (1,181) are also large.
- **Weakness:** **mirrored constant sets** (`_ENTITY_FIELD_NAMES` / `_ENTITY_SHAPED_PARAMS`) kept in sync by comment (gap #16) — a drift hazard.

### Module boundaries
- **Strength:** the `handler_ref → callable` resolver is a clean uniform invoker boundary (matches the SDK's `(ctx,json)→output` contract, `reference §2.1`); the LLM gateway is a real single-egress seam.
- **Weakness:** **data access has no owner** — split between `_shared/ticket_store` and ~10 raw-SQL UC sites (gap #9). The DAL (the intended boundary) is deferred. This is the most consequential boundary gap.
- **Weakness:** **HITL semantics are split** across `executor/nodes` (helpers), `step_runner` (gate), and `api/app.py` (capture/resume × 2). No single module owns the interrupt protocol end-to-end (`reference §5` shows Temporal naming these as first-class primitives).

### Ownership clarity
- Strong for routing (the funnel) and registry. **Unclear for:** data access, prompt assets (in code/JSON, no registry), telemetry emission (hand-rolled at call sites; `reference §3.1` argues for one handler).

**Verdict:** the *ideas* are A-grade; execution is dragged to B− by a few oversized hub modules and the deferred data boundary. None are conceptual — all are extract-and-consolidate refactors.

---

## 2. Scalability — **B+**

### Routing scalability
- **Strength (notable):** **retrieve-then-LLM-decide** (pgvector shortlist → disambiguate) is the correct answer to the "inject-all collapses past ~30-50 tools" problem and is **provider-neutral via LiteLLM** — ahead of the OpenAI SDK's hosted tool-search (OpenAI-only) (`reference §2.5`). Route decisions are cached (identical for everyone). This scales to 100+ agents by design.
- **Tuning:** adopt the SDK's **<10-functions-per-namespace** shortlist-size heuristic to keep the disambiguation prompt tight (`reference §2.1 B6`).

### Workflow scalability
- **Strength:** the `wave`/`run_step`/`dispatch_wave` loop is LangGraph super-step parallelism; the **subgraph path** (specialist = compiled subgraph) is available for scaling capability domains independently (`reference §1.6`).
- **Watch:** the dynamic planner is paid even for deterministic UCs; classifying UCs into chains vs orchestrator-worker (`reference §1.1`) caps LLM-call growth.

### Retrieval scalability
- pgvector + per-service `ai.embeddings_<service>` with trigger→pgmq→worker refresh is a sound, horizontally-scalable substrate. Workers are separate processes (good).

### Horizontal scaling
- The FastAPI app is largely stateless per request (state in Postgres checkpointer + session store + Dragonfly), so app replicas scale horizontally. **Two ceilings:** (1) **metric cardinality** — tenant/session on metrics caps how many tenants Prometheus survives (gap #5); (2) **remote-Tokyo-DB locality** — the 2.7 s/turn DB cost is a per-turn tax that scales linearly with traffic (a placement, not architecture, issue the team has chosen to keep).

**Verdict:** B+ — the hard part (routing at scale) is solved well; the ceilings are observability cardinality and DB locality, both addressable.

---

## 3. Reliability — **C+**

### Retries
- **Strength:** LLM nodes carry `retry_policy`; NATS has a resilience adapter; `invoke(None)` resume + pending-writes give partial-failure recovery (`reference §1.2/§1.7`).
- **Gap:** retry is not a **per-task-type taxonomy** (best-effort vs critical) and timeouts conflate attempt/budget/liveness (gap #12; `reference §5`). Retry rules aren't expressed as replay-safe data (`reference §2.3`), risking re-issue of non-idempotent action tools.

### Failures & recovery
- **Strength:** Postgres checkpointer + thread=session = durable pause/resume; fail-closed approvals never auto-approve.
- **Critical gap:** **resume re-runs node code** (`reference §1.2`), so any side effect before an `interrupt()` double-executes, and NATS at-least-once without idempotency keys re-applies tasks (gaps #1, #2). These are the two correctness landmines.
- **Gap:** **saga compensation** on parallel waves (`gather` sibling-cancel, compensate-on-true-cancel) is unproven (gap #10), and its **test surface is stale** (gap #11) — so the failure paths ship unverified.

### Timeouts
- Per-tool timeout exists; needs the taxonomy split (gap #12).

### Recovery
- Checkpointer + state-history give strong *mechanism*; the missing piece is a **replayer-as-CI-gate** so a state-shape change doesn't break in-flight paused sessions (gap #15).

**Verdict:** C+ — the *foundations* are right (durable checkpointer, retries, fail-closed), but the **correctness of resume/idempotency/compensation is unproven and undertested**, and that is precisely where an enterprise incident would originate. Findings 1/2/10/11 are the single most important workstream in this review.

---

## 4. Security — **B**

### Tenant isolation
- **Strength:** identity is **server-derived from headers/JWT, not request body**; stores take `tenant_id`; embeddings filter on `tenant_id`; cache keys include tenant; checkpointer namespaced by session (`reference §1.4 identity-in-context`).
- **Gap:** the **~10 raw-SQL sites** depend on each call remembering the tenant predicate — one omission is a cross-tenant leak (gap #9). Identity should ride in runtime context and never be settable from LLM-chosen tool args (verify) (`reference §1.4`).

### Access control
- **Strength:** RBAC (role-permission registry) + per-tool tier `authz_recheck` (read step ≠ write perm) + policy redaction. This is genuinely above-median.
- **Enhancement:** add the **tool-guardrail tier** (validate/redact every DAL/action call, before+after) and a cheap **pre-LLM guardrail** node to block destructive/cross-tenant/injection inputs before any LLM spend (`reference §2.2`).

### Prompt-injection risk
- The router/handlers feed ticket text (attacker-controllable) into LLM context. Mitigations present: closed-enum structured outputs (limits action surface), policy layer, fail-closed approvals (a model can't self-approve). **Gaps:** no explicit injection guardrail; tool-args could in principle carry model-influenced tenant/id values (must verify identity-from-context). Recommend a pre-route injection/destructive-intent guard (`reference §2.2`).

### Data leakage
- **Gap (compliance):** PII in checkpoints and traces unencrypted/inline (gap #3) — the highest-severity security item for regulated tenants. Fix via `EncryptedSerializer` + trace body externalization (`reference §1.3/§3.1`).

**Verdict:** B — strong structural isolation + fail-closed approvals; held from A by PII-at-rest in checkpoints/traces, the raw-SQL bypass, and the missing injection guardrail.

---

## 5. Observability — **B**

### Traces
- **Strength:** stage-granular OTel spans over the funnel + executor, rendered as a Tempo tree; Langfuse for LLM I/O. This is real, not aspirational.
- **Gaps:** (1) **NATS legs likely not context-propagated** → disconnected fulfillment traces (gap #4) — the most actionable fix; (2) LLM/tool legs should use `gen_ai.*` **semconv op-names** so Langfuse auto-classifies (`reference §3.1`); (3) span/metric emission is **hand-rolled** with no central handler → attribute drift (gap #19).

### Metrics
- **Strength:** `ai.agent.runs.total`, DB query duration, per-tenant cost; Grafana dashboards.
- **Gap:** **cardinality firewall** not enforced — tenant/session on metrics is a scaling landmine (gap #5); histogram buckets should be LLM-appropriate (`reference §3.1`).

### Logs
- structlog + OTel trace_id/span_id correlation processor — good.

### Debugging capability
- **Strength:** checkpointer state-history is a powerful replay/audit tool (under-leveraged).
- **Gaps:** no **eval/score** substrate (gap #13) and no **prompt versioning** (gap #14), so "why did routing/answer quality regress" can't be attributed to a prompt rev or scored systematically. Cost may under-count cache/reasoning tokens (gap #6).

**Verdict:** B — a genuine observability stack with three concrete, high-value gaps (NATS propagation, cardinality, cost-token completeness) and two quality substrates absent (evals, prompt versioning).

---

## 6. Performance — **B**

### LLM calls
- **Strength:** measured RCA shows the cost model (network floor × N calls + per-output-token + remote DB); the team is collapsing the router's multiple LLM waves toward fewer calls and stripping reasoning/rationale from intermediate outputs.
- **Lever (from references):** `tool_use_behavior` short-circuit for deterministic read tools (skip the second LLM hop, `reference §2.1 B5`); classify deterministic UCs as chains to avoid the planner (`reference §1.1`); family-aware low reasoning effort for latency-sensitive UCs (`reference §2.3`).

### Retrieval latency
- pgvector shortlist is efficient; the dominant cost is **remote-Tokyo-DB locality** (~2.7 s/turn), a deliberate placement choice. Within the app/router/transport envelope the team has (correctly) pursued router-collapse, streaming, and caching rather than moving the DB.

### Orchestration overhead
- LangGraph super-steps are cheap; the overhead is the LLM calls inside nodes, not the graph. Node-caching deterministic fetch nodes (`reference §1.7`) would remove repeated cost on resume/retry (gap #22).

### Caching opportunities
- **Strength (notable):** multi-layer Dragonfly caching — route-decision cache, semantic chat-turn cache (1600× speedup measured), fast-path edge cache, session hot window — with version-stamped keys that auto-invalidate on render changes. This is more sophisticated than the FastAPI guide prescribes (which leaves app caching out of scope) and is a genuine strength.
- **Future:** when streaming ships, tag internal LLM calls `nostream` to cut on-wire token cost and improve perceived latency toward the ~1 s target (gap #20).

**Verdict:** B — strong caching and a measured, honest latency model; bounded primarily by LLM-call count (being worked) and remote-DB locality (a chosen constraint).

---

## Synthesis

**What to protect:** agents-as-data + registry, retrieve-then-decide routing, single LLM egress, fail-closed approvals, the multi-layer cache, the measured-RCA engineering culture. These are differentiators and should not be compromised by any refactor.

**What to fix, in order of consequence:**
1. **Durable-execution correctness** (resume side effects, idempotency, compensation) + its **test surface** — Findings 1/2/10/11. This is the difference between "works in the demo" and "safe for enterprise fulfillment."
2. **Multi-tenant telemetry + PII discipline** — cardinality firewall (#5), NATS propagation (#4), PII-in-checkpoints/traces (#3). These determine whether the platform is observable and compliant *at scale*.
3. **Structural maintainability** — decompose `app.py`, unify interrupt paths, land the DAL boundary (#8/#9). These compound velocity and reduce change-failure rate.
4. **Quality & testing leverage** — `FakeGateway` deterministic tests (#7), eval/score substrate (#13), prompt versioning (#14). These convert "verified live" into "verified in CI."

The platform's *principles* would earn an A; the *production-hardening* earns a B-to-C in the reliability/correctness band. Closing the Finding-1/2/10/11 workstream is the highest-ROI move available.

> Grades are relative to enterprise-production expectations, not to typical AI-prototype maturity (against which AI-service would grade markedly higher). Roadmap in `docs/refactor_roadmap.md`.
