# Stack Components — Detail

## LangGraph — orchestration

### What it owns
- Defining the DAG (StateGraph)
- Scheduling nodes (sequential + parallel)
- Routing decisions via conditional edges
- State propagation between nodes (typed `OneOpsState`)
- Checkpointing (Postgres backend for resumable workflows)
- Streaming intermediate node outputs to the client

### What it does NOT own
- Tool calling inside a node (that's the agent's job — `create_react_agent` or hand-rolled tool loop)
- LLM call ergonomics (that's the LLM Gateway)
- Inter-service communication (that's NATS)

### Conditional edges — the key pattern

Decomposer emits `state.dag`. Conditional edge function maps DAG node UCs to graph node names:

```python
graph.add_conditional_edges(
    "decomposer",
    lambda s: list({n["uc_id"] for n in s["dag"]}),  # set of UCs to invoke
    {
        "uc01_summarization": "uc1_node",
        "uc02_similar_tickets": "uc2_node",
        "uc03_kb_lookup": "uc3_node",
        # ... no new edge needed when UC is added; graph builder reads UC manifest
    },
)
```

Parallel UCs in the same set fan out automatically.

### Checkpointing

```python
from langgraph.checkpoint.postgres import PostgresSaver

checkpointer = PostgresSaver.from_conn_string(POSTGRES_URL)
graph = builder.compile(checkpointer=checkpointer)
```

What this enables:
- Long-running workflows (UC-8 fulfillment) survive process restart
- Time-travel debugging (replay any node from any prior state)
- Streaming intermediate results to UI while later nodes still execute

## NATS — messaging fabric

### When it's used
NATS is used when UCs are deployed as **separate services**. In single-process mode, NATS is dormant.

| Pattern | When |
|---|---|
| Request/Reply | Synchronous UC invocation (UC-1, UC-3, UC-7) |
| JetStream durable streams | Long-running workflows (UC-8 fulfillment) where state must survive restarts |
| Subject hierarchy | `oneops.uc.<uc_id>.<intent>` — e.g. `oneops.uc.uc01_summarization.entity_summary` |

### Trace context propagation

OTEL `traceparent` header attached to every NATS message:
```python
headers = {}
trace_context.inject(headers)  # OTEL propagator
await nats.publish(subject, payload, headers=headers)
```

Receiver extracts and continues the trace. End-to-end visibility across service hops.

### Service registration

Each UC service registers itself on NATS at startup:
```python
await nats.subscribe(f"oneops.uc.{uc_id}.>", handler=handle_uc_request)
```

Multiple replicas of the same UC = NATS queue group → load-balanced.

### Codec on the wire

NATS payloads are protobuf-encoded. Messages have:
- Trace context (in headers)
- Schema version (in payload)
- Request envelope (uc_id, intent, params, role, tenant_id)
- Response (status, output, error)

Schema evolution: protobuf field numbers are stable forever. Adding optional fields is safe. Removing or renumbering = breaking; bump major version.

## OpenTelemetry — observability

### What gets instrumented

| Layer | Span name | Attributes |
|---|---|---|
| Request entry | `oneops.request` | request_id, session_id, tenant_id, role, message_length |
| Graph node | `oneops.node.<node_name>` | node_id, uc_id, intent |
| LLM call | `llm.call` | model, prompt_hash, max_tokens, cache_hit, latency, tokens_in/out |
| Tool call | `tool.<tool_name>` | uc_id, params_hash, rows_returned, latency |
| Cache | `cache.<get|set>` | key_pattern, hit, ttl |
| DB query | `db.query` | table, operation, rows |
| NATS request | `nats.request` | subject, payload_size |

Auto-instrumentation handles HTTP clients, Redis, Postgres. Custom spans only for application-level events.

### Sampling

- Dev: 100% sampling (see everything)
- Prod: 100% on errors, 10% on success (head-based sampling)
- Long traces (UC-8 workflows): always sampled

### Backends

Pluggable via OTLP. Dev uses Tempo + Grafana (local). Prod can swap to Datadog, Honeycomb, New Relic — config change only.

### Replacing the custom audit layer

POC3's `_emit_audit` and per-stage timing become OTEL spans. The "audit log" is just a query against the OTEL backend. No separate audit pipeline.

## LLM Gateway — model routing

### Why centralize

Today every UC pack instantiates its own OpenAI client. Result:
- Rate limits hit per-process, not globally
- No central retry policy
- No central replay-cache
- No central cost tracking
- Prompt versions drift per UC
- Model upgrades require touching every UC

The gateway fixes all of this with one egress point.

### Implementation: LiteLLM (start here)

LiteLLM is an open-source LLM proxy. Run it as a sidecar:

```yaml
# docker-compose.yml
litellm:
  image: ghcr.io/berriai/litellm:main-latest
  ports: ["4000:4000"]
  command: --config /config.yaml
  volumes: ["./ops/litellm.yaml:/config.yaml"]
```

```yaml
# ops/litellm.yaml
model_list:
  - model_name: gpt-4o-mini
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY
  - model_name: gpt-4o
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY
router_settings:
  routing_strategy: simple-shuffle
  num_retries: 3
  timeout: 60
general_settings:
  master_key: sk-...
```

### Application client

`LLMGateway` is a thin wrapper around the LiteLLM endpoint:

```python
class LLMGateway:
    def __init__(self, base_url: str, api_key: str):
        self._client = httpx.AsyncClient(base_url=base_url, headers={...})
        self._replay = ReplayCache()  # local Dragonfly
    
    async def call(self, prompt, model, **kwargs) -> str:
        with tracer.start_as_current_span("llm.call", attrs={"model": model}):
            # Replay-cache check (for tests + cost reduction in dev)
            key = hash_prompt(prompt, model, kwargs)
            if cached := await self._replay.get(key):
                return cached
            resp = await self._client.post("/chat/completions", json={...})
            await self._replay.set(key, resp.text)
            return resp.text
```

### Prompt versioning

Every prompt template has a version stamp:
```python
PROMPT_VERSION = "uc01.summary.v3.2026-05-14"
prompt = render_template(template, version=PROMPT_VERSION, ...)
```

Gateway logs `(prompt_version, model, response_hash)` to OTEL attributes. Replay-cache keyed by version. Rolling a new prompt is an explicit decision visible in dashboards.

### Cost tracking

LiteLLM emits token usage per call. Stream to OTEL → dashboard per UC × per tenant. Surfaces who's expensive before the bill arrives.

### Replay-cache for tests

`LLM_REPLAY_MODE=record` first run captures all LLM responses. `LLM_REPLAY_MODE=replay` reproduces them deterministically. No API key needed in CI.

## Codec — wire format

### Choice: protobuf

For inter-service messages (NATS payloads, possibly gRPC later). Schema in `proto/oneops/v1/*.proto`:

```protobuf
// proto/oneops/v1/uc_request.proto
syntax = "proto3";
package oneops.v1;

message UCRequest {
  string request_id = 1;
  string session_id = 2;
  string tenant_id = 3;
  string user_id = 4;
  string role = 5;
  string uc_id = 6;
  string intent = 7;
  map<string, string> params = 8;     // simple cases
  bytes params_extended = 9;          // typed params per intent (separate sub-messages)
  string trace_context = 10;          // W3C traceparent
}

message UCResponse {
  string node_id = 1;
  string status = 2;                  // executed | no_match | failed | clarification_required
  string user_response = 3;
  bytes output_extended = 4;          // typed output per intent
  string error = 5;
  int64 latency_ms = 6;
  repeated string executed_tools = 7;
}
```

Generate Python bindings: `protoc --python_out=src/oneops/proto proto/oneops/v1/*.proto`.

### Why not just JSON

- Schema evolution rules (protobuf field numbers don't drift; JSON keys break silently)
- Smaller wire size at scale
- Language-agnostic if a UC is ever written in Go/Rust
- Forced type discipline (no implicit any)

JSON is fine inside Python (LangGraph state), but on the wire = protobuf.

## Dragonfly — session + cache

### Why Dragonfly (vs Redis)

Drop-in Redis API but:
- Multi-threaded (Redis is single-threaded per shard)
- Higher throughput per node
- Lower memory footprint

For OneOps's hot-path workload (session reads/writes, cache lookups), Dragonfly handles it without a cluster.

### Keyspaces

| Pattern | Purpose | TTL |
|---|---|---|
| `session:{session_id}` | Conversation history (list of turns, JSON-encoded) | 1h sliding |
| `focus:{session_id}` | Active/mentioned/anchor subjects, pending_clarification | 1h sliding |
| `canonical:{session_id}` | last_successful_use_case, turn_index, last_tool_results | 1h sliding |
| `cache:{fingerprint}` | Response cache | 5min default, per-UC override |
| `lock:{key}` | Single-flight locks | 120s |
| `replay:{prompt_hash}:{model}` | LLM replay cache | infinite (manual flush) |

### Cache fingerprint

```python
fingerprint = sha256(":".join([
    tenant_id, role, message_normalized, focus_active_subject_id,
    prompt_version,   # invalidate when prompts change
]))
```

Tying cache key to `prompt_version` solves POC3's pre-warm cache hangover problem.

## Postgres — durable data

### Per-UC repository pattern (preserved from POC3)

```
src/oneops/repositories/
├── base.py                # async pool, _fetchone, _fetchall
├── itsm.py                # UC-1, UC-5, UC-6 shared ITSM queries
├── kb.py                  # UC-3 only
├── embeddings.py          # UC-2, UC-3 vector search
└── audit_archive.py       # for cold-storage OTEL traces (optional)
```

### pgvector for UC-2, UC-3, UC-7

Embeddings stored in pgvector columns. Hybrid search (BM25 + cosine) at the repository layer. Embedding model called via LLM Gateway.

### Checkpointer (LangGraph)

`langgraph_checkpoints` table in same Postgres. Stores graph state snapshots. Enables resume + time-travel.

## How they compose (the request lifecycle)

```
1. HTTP POST /chat  →  FastAPI endpoint  →  bridge.run(request)
                       │
                       ▼
2. bridge.run()        OTEL span: oneops.request
                       │
                       ├── 2a. Load session state from Dragonfly
                       │       OTEL span: cache.get(session/focus/canonical)
                       │
                       ├── 2b. Build LangGraph state
                       │
                       └── 2c. graph.ainvoke(state)
                              │
                              ▼
3. Graph: decomposer node
                       │   OTEL span: oneops.node.decomposer
                       │   │
                       │   └── LLM Gateway call (gpt-4o-mini)
                       │           │ OTEL span: llm.call
                       │           │ httpx → LiteLLM proxy → OpenAI
                       │           ▼
                       │       returns DAG (JSON)
                       │
                       ▼ Conditional edge: which UC(s)?
4. Graph: uc1_node (parallel with uc3_node if DAG has both)
                       │   OTEL span: oneops.node.uc1_summarize
                       │   │
                       │   ├── Local mode: direct tool calls
                       │   │       OTEL spans: tool.get_ticket_details, ...
                       │   │
                       │   └── Microservice mode: NATSInvoker
                       │           OTEL span: nats.request
                       │           │ NATS request to oneops.uc.uc01.summary
                       │           │ trace_context in headers
                       │           ▼
                       │       remote uc1-service runs the same node code
                       │
                       ▼
5. Graph: aggregator node
                       │   OTEL span: oneops.node.aggregator
                       │   │
                       │   └── (if needed) LLM Gateway call to stitch outputs
                       │
                       ▼
6. bridge.run() exit
                       │
                       ├── 6a. Write session state back to Dragonfly
                       ├── 6b. Emit final OTEL span attributes
                       │
                       └── Return FinalResponse to client
```

Every hop has a span. Trace propagates through Dragonfly, LiteLLM, NATS, OpenAI. End-to-end visibility for free.

## Cost summary

| Component | Why it costs (and when) |
|---|---|
| LangGraph | Free (open source) |
| NATS | Free; small infra (3-node cluster fits on cheap VMs) |
| OTEL | Free SDK; backend cost depends on vendor (Tempo is free) |
| LiteLLM | Free (open source); proxies OpenAI/Anthropic/etc. — same per-token cost |
| Protobuf | Free |
| Dragonfly | Free (BSL license); single instance is fine for most loads |
| Postgres | Free (open source) |

Total stack cost beyond LLM API spend: near-zero. The architecture pays for itself by reducing LLM cost (replay cache, prompt versioning, dedup via fingerprinting).
