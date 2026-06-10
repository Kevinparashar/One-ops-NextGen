# OneOps AI Service v4 — Architecture Overview

## What we're building

A production-grade ITSM AI service that:
- Runs **multiple use cases** (UC-1 through UC-8) from **a single user chat**
- Routes queries via an **LLM-driven DAG decomposer**, not phrase lists or regexes
- Executes UCs **in parallel or sequentially** based on inter-UC dependencies
- Deploys as **microservices** OR runs **in-process (FaaS)** behind the same interface
- Emits **OpenTelemetry** traces end-to-end (no custom audit layer)
- Routes all model calls through a **central LLM Gateway** (one place for retries, rate limits, cost tracking, prompt versioning)
- Encodes inter-service messages with a **stable codec** (protobuf/msgpack)
- Carries state across services on **NATS** (request/reply + JetStream for durable workflows)

## The 8 use cases (target — single-chat orchestration)

| UC | What it does | Status in POC3 |
|---|---|---|
| UC-1 Ticket Summary | Summarize ticket / asset / CI; field-read follow-ups | 95.7% stress |
| UC-2 Similar Tickets | Vector-search similar tickets, duplicate detection | not built |
| UC-3 KB Lookup | KB search, article fetch, ticket-context KB | 95.8% stress |
| UC-4 Sentiment Detection | Per-comment sentiment, trajectory, escalation | not built |
| UC-5 Triage & Route | Classify, prioritize, dedup, assign | not built |
| UC-6 Conversational Create | Slot-filling new-ticket flow | not built |
| UC-7 Resolution Suggestion | Synthesize from UC-2 + UC-3 + work notes | not built |
| UC-8 Catalog Fulfillment | Multi-phase fulfillment workflow | not built |

**One chat, many UCs.** A single user message can fan out into multiple UC invocations — sequential when one depends on another, parallel when independent. The decomposer LLM produces the DAG; the executor runs it.

Example queries that span UCs:
- `summarize INC0001001 and find KB articles for it` → UC-1 → UC-3 (sequential, UC-3 needs entity context)
- `summarize INC0001001 and CHG0004007` → UC-1 × 2 (parallel)
- `for this incident, find similar tickets and suggest a resolution` → UC-2 + UC-3 → UC-7 (UC-7 aggregates)
- `create a ticket for VPN issue, but first check if there's already an open one` → UC-6 (slot-fill) waits on UC-2 (duplicate scan)

## Architectural principles (non-negotiable)

These carry forward from `CLAUDE.md` and strengthen:

1. **No static approaches anywhere.** No hardcoded keyword lists, service nouns, field names, tool names, or intent regexes in any code path that decides routing or execution. Decisions consult registries; phrasings consult the LLM.
2. **No common file overloading.** Common infrastructure (graph builder, dispatcher, session store, gateway client) never references a specific UC. Adding a new UC = drop files in its folder. Zero changes to common code.
3. **Registry-driven, schema-described.** Operation cards, field catalogs, tool manifests are data files. Their LLM-readable descriptions are the contract; routing is semantic over those descriptions.
4. **UC isolation, both runtime and deployment.** UC-1 failing cannot affect UC-3 requests. Any UC can be deployed as its own service or run in-process — same interface either way.
5. **LLM is the decision-maker; deterministic layers only short-circuit when certain.** BM25 / cache lookups only fire on unambiguous signal. Borderline cases always reach the LLM.
6. **RBAC + tenant isolation at the tool boundary.** Never at the LLM layer, never at the graph layer. Tools enforce.
7. **Observability is OTEL, not custom audit.** Every node, every tool, every LLM call is a span. Trace context propagates through NATS headers when crossing service boundaries.
8. **Production hygiene.** Feature flags, snapshot-before-edit, smoke-after-fix-point, no big-bang refactors.

## The stack

| Layer | Component | Purpose |
|---|---|---|
| Orchestration | **LangGraph** | StateGraph for the DAG. Decomposer + UC nodes + aggregator. Native parallel/sequential edges, conditional routing, checkpointing. |
| Messaging | **NATS** (JetStream optional) | Inter-service req/reply for UC microservices. Durable streams for long-running workflows (UC-8 fulfillment). |
| Encoding | **Codec** (protobuf or msgpack) | Stable wire format for inter-service messages. Versioned schemas. |
| Tracing | **OpenTelemetry** | Every node + tool + LLM call is a span. Trace context propagated via NATS headers. Backend: Tempo/Jaeger/Datadog — pluggable. |
| Model routing | **LLM Gateway** | Single egress for all LLM calls. Retries, rate limits, fallback models, cost tracking, prompt-version pinning, replay-cache. |
| State | **Dragonfly** | Session state (focus, history, canonical_state). Response cache. Single-flight locks. |
| Data | **Postgres + pgvector** | Tickets, KB, embeddings. Per-UC repository pattern. |

Nothing here is novel individually. The composition is what matters.

## Microservices + Function-as-a-Service

The same UC node code runs in two deployment modes:

### Mode A — In-process (FaaS-style)
Local function call. Zero network hop. Used for:
- Single-instance dev / small deployments
- Hot path UCs where latency matters
- Cases where the graph and the UC live in the same trust boundary

### Mode B — Microservice (NATS req/reply)
UC runs as its own service, reachable over NATS. Used for:
- Independent scaling per UC
- Independent deployment cadence per UC
- Cross-team boundaries (UC-8 fulfillment owned by a different team)
- Resource isolation (UC with heavy ML deploys on GPU nodes)

**The graph code doesn't know which mode is active.** A `UCInvoker` interface has two implementations: `LocalInvoker` (direct call) and `NATSInvoker` (request/reply). Configuration decides.

```python
# Inside a LangGraph node — never knows about transport
result = await uc_invoker.invoke(
    uc_id="uc01_summarization",
    intent="summary",
    params={"entity_id": "INC0001001", "service_id": "incident"},
    context=trace_ctx,  # OTEL trace propagation
)
```

Operational implication: you can start everything in-process (1 binary, 1 deploy), profile, then peel off heavy UCs into their own services as scale demands. No code change in the graph layer.

## What's NOT in this architecture (compared to POC3)

- ❌ Custom preprocessor + LLM classifier pipeline → replaced by LangGraph entry node + decomposer
- ❌ M+ shortlister + reranker → replaced by decomposer over operation cards
- ❌ Bridge packs / planner packs / preprocessor packs (5-pack model) → each UC is one node implementation + tools + registry data
- ❌ Phrase-list regexes (`_LINKED_ENTITY_QUESTION_RE`, `_SOLUTION_VERB_RE`, etc.) → semantic resolution
- ❌ Alias lists in field catalog → schema descriptions; LLM maps user vocab to fields
- ❌ Custom audit emission → OpenTelemetry spans
- ❌ Custom session store implementations per UC → one platform-level adapter
- ❌ Direct OpenAI client calls scattered across UCs → all calls through LLM Gateway

## Where the architecture goes longer-term (out of scope for v1)

- **UC marketplace.** Third-party UCs published as services + operation cards. Decomposer routes to them without code change.
- **Tenant-specific UC enablement.** Per-tenant flags in the registry control which UCs the decomposer sees.
- **Long-running workflows.** UC-8 (fulfillment) and UC-5 (multi-stage triage) live on JetStream durable streams. Workflow state survives restarts.
- **Replay-first development.** Every production trace is replayable in dev. Combine OTEL spans + LLM Gateway replay-cache + Dragonfly session snapshot.
- **Multi-region.** NATS clusters per region; session store partitioned by tenant.

## Document map

| Doc | Topic |
|---|---|
| `docs/guides/00_overview.md` | This file |
| `docs/guides/01_stack_components.md` | LangGraph, NATS, OTEL, codec, LLM gateway — deeper detail |
| `docs/guides/02_uc_pack_structure.md` | What a UC folder contains. No common file bloat. |
| `03_state_and_session.md` | State schema. Session store. Multi-turn focus. |
| `04_decomposer_and_dag.md` | How single-chat multi-UC routing works |
| `05_microservices_and_faas.md` | Deployment model + UCInvoker interface |
| `06_observability_otel.md` | Span layout, trace propagation, what to instrument |
| `docs/guides/07_starting_steps.md` | Concrete order — from empty folder to working UC-1 |
| `08_migration_from_poc3.md` | What to port, what to drop, what to rewrite |
