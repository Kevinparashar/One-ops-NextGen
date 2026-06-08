# Starting Steps — From Empty Folder to Working UC-1

This is the concrete order. Each step has a definition-of-done that you can verify before moving on.

## Pre-work: stack decisions to lock down NOW

Before writing code, lock these decisions. They're hard to change later.

| Decision | Recommendation | Why |
|---|---|---|
| Codec | **protobuf** | Stable wire format, schema-evolution rules, language-agnostic if a UC ever needs Go/Rust. msgpack is fine for prototype, protobuf for production. |
| LLM Gateway | **LiteLLM** (open source) or build thin in-process | LiteLLM gives multi-provider routing, retries, rate limits out of the box. Wrap with your own metrics + prompt-version pinning. |
| OTEL backend | **Tempo + Grafana** locally; vendor flexible in prod | Free, fast to spin up. Production swap to Datadog/Honeycomb is one config change. |
| NATS deployment | **Single instance** for dev; **3-node cluster** for prod | Don't over-engineer the dev environment. |
| LangGraph state store | **In-memory** for dev; **Postgres checkpointer** for prod | Postgres checkpointer is what unlocks resumable workflows. |
| Python version | **3.12** | LangGraph + LangChain are well-tested here. |

## Phase 0 — Project scaffolding (half day)

### 0.1 Directory structure

```bash
cd "/home/kevin-parashar/AI-services/POC copy 4"
mkdir -p \
  src/oneops/{state,graph,nodes,tools,adapters,gateway,registries,proto} \
  src/oneops/use_cases \
  tests/{unit,integration,stress} \
  docs/03_nodes \
  config \
  ops/{nats,otel}
touch src/oneops/__init__.py
```

### 0.2 `pyproject.toml`

```toml
[project]
name = "oneops"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    # Orchestration
    "langgraph>=0.2.50",
    "langchain-core>=0.3",
    "langgraph-checkpoint-postgres>=2.0",
    # LLM clients (through gateway)
    "litellm>=1.50",
    "openai>=1.50",
    # Messaging
    "nats-py>=2.9",
    # Observability
    "opentelemetry-api>=1.27",
    "opentelemetry-sdk>=1.27",
    "opentelemetry-instrumentation>=0.48b0",
    "opentelemetry-exporter-otlp>=1.27",
    # Encoding
    "protobuf>=5.28",
    "grpcio-tools>=1.66",  # for proto compilation
    # Data
    "pydantic>=2.7",
    "redis>=5.0",                  # Dragonfly client
    "psycopg[binary]>=3.2",
    # Config
    "python-dotenv",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
test = ["pytest>=8", "pytest-asyncio", "pytest-cov", "httpx"]
dev = ["ruff", "mypy", "pre-commit"]
```

### 0.3 `.env` template

```bash
# .env
OPENAI_API_KEY=...
LLM_GATEWAY_URL=http://localhost:4000      # LiteLLM proxy
NATS_URL=nats://localhost:4222
DRAGONFLY_URL=redis://localhost:6379/0
POSTGRES_URL=postgresql://...
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_SERVICE_NAME=oneops-graph
```

### 0.4 `docker-compose.yml` for local stack

```yaml
services:
  dragonfly:
    image: docker.dragonflydb.io/dragonflydb/dragonfly
    ports: ["6379:6379"]
  nats:
    image: nats:latest
    command: "-js"   # enable JetStream
    ports: ["4222:4222", "8222:8222"]
  tempo:
    image: grafana/tempo:latest
    ports: ["4318:4318", "3200:3200"]
  litellm:
    image: ghcr.io/berriai/litellm:main-latest
    ports: ["4000:4000"]
    environment:
      - OPENAI_API_KEY=${OPENAI_API_KEY}
```

**Done when:** `docker compose up`, `pip install -e .`, `python -c "import oneops"` works.

## Phase 1 — Foundational adapters (1-2 days)

These are the platform primitives every node will use. Build them first; everything else depends on them.

### 1.1 LLM Gateway client (`src/oneops/gateway/llm_client.py`)

Single class, async, OTEL-instrumented, replay-aware:

```python
class LLMGateway:
    async def call(
        self,
        prompt: list[dict],
        model: str = "gpt-4o-mini",
        response_format: dict | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        replay_key: str | None = None,
    ) -> str: ...
```

Behind the scenes:
- POST to LiteLLM proxy (or direct OpenAI if not deployed yet)
- OTEL span per call (`llm.call`)
- Replay-cache lookup keyed by `(prompt_hash, model)` when `replay_key` set
- Retry with exponential backoff on transient errors

Every LLM call in the codebase goes through this. No exceptions.

### 1.2 Session store adapter (`src/oneops/adapters/session_store.py`)

Three keyspaces in Dragonfly:
- `session:{session_id}` — conversation history (list of turns)
- `focus:{session_id}` — active_subject, mentioned_subject, anchor_subject, pending_clarification
- `canonical:{session_id}` — last_successful_use_case, turn_index, last_tool_results

Read at graph entry, write at graph exit. Atomic update operations (`update_focus`, `append_history`).

### 1.3 Tool registry (`src/oneops/tools/registry.py`)

Decorator-based registration, LangChain-compatible:

```python
@tool(uc_id="uc01_summarization", audience={"service_desk_agent", "employee"})
async def get_ticket_details(ticket_id: str, service_id: str) -> dict:
    """Fetch ticket record from Postgres. Returns dict of fields."""
    ...
```

Registry tracks: which UC owns each tool, audience/role allowlist, async-or-sync, schema. The graph never iterates tools; the UC node loads its own tools at registration time.

### 1.4 OTEL setup (`src/oneops/observability/otel.py`)

One-time init at process start:
- Set service name (`oneops-graph` or `oneops-uc01` if separated)
- Configure OTLP exporter pointing to Tempo
- Auto-instrument: HTTP clients (httpx), Redis, Postgres, OpenAI client

After this, `with tracer.start_as_current_span("decomposer"):` is all you need anywhere.

### 1.5 NATS client (`src/oneops/adapters/nats_client.py`)

Thin wrapper:
```python
class NATSClient:
    async def request(self, subject: str, payload: bytes, timeout: float = 30) -> bytes: ...
    async def subscribe(self, subject: str, handler: Callable) -> None: ...
```

Propagates OTEL trace context via NATS headers (`traceparent`).

**Done when:** unit tests for each adapter pass. LLM gateway can call OpenAI, session store can round-trip, OTEL spans show in Tempo UI.

## Phase 2 — State schema + stub graph (1 day)

### 2.1 State schema (`src/oneops/state/schema.py`)

The typed graph state. This is the contract between every node — design it carefully:

```python
from typing import Annotated, TypedDict, Optional, Literal
from operator import add

class FocusState(TypedDict):
    active_subject_id: Optional[str]
    active_subject_service: Optional[str]
    mentioned_subject_id: Optional[str]
    anchor_subject_id: Optional[str]
    pending_clarification: Optional[dict]
    captured_at_turn: int

class DAGNode(TypedDict):
    node_id: str
    uc_id: str                       # uc01_summarization, uc03_kb_lookup, ...
    intent: str                      # summary, kb_search, field_read, ...
    params: dict                     # entity_id, query, etc.
    depends_on: list[str]            # other node_ids; empty = root

class NodeResult(TypedDict):
    node_id: str
    uc_id: str
    intent: str
    status: Literal["executed", "no_match", "clarification_required", "failed"]
    output: dict                     # uc_payload + tool results
    user_response: str               # display text
    entity_id: Optional[str]
    service_id: Optional[str]
    executed_tools: list[str]
    error: Optional[str]
    latency_ms: int

class OneOpsState(TypedDict):
    # Request envelope
    request_id: str
    session_id: str
    tenant_id: str
    user_id: str
    role: str
    locale: str
    message: str
    turn_index: int
    # Loaded from session store
    conversation_history: list[dict]
    focus: FocusState
    # Decomposer output
    dag: list[DAGNode]
    decomposer_reasoning: str
    # Execution accumulator
    node_results: Annotated[list[NodeResult], add]
    # Final response
    final_response: Optional[str]
    final_status: Optional[str]
```

### 2.2 Stub graph (`src/oneops/graph/builder.py`)

3-node hello-world: load → stub_decomposer → stub_uc → aggregator → END.
All nodes return hardcoded data. Verifies LangGraph plumbing.

```python
def build_graph():
    g = StateGraph(OneOpsState)
    g.add_node("load_session", load_session_node)
    g.add_node("decomposer", stub_decomposer_node)
    g.add_node("uc1_summarize", stub_uc1_node)
    g.add_node("aggregator", stub_aggregator_node)
    
    g.set_entry_point("load_session")
    g.add_edge("load_session", "decomposer")
    g.add_conditional_edges(
        "decomposer",
        lambda s: [n["uc_id"] for n in s["dag"]] or ["aggregator"],
        {"uc01_summarization": "uc1_summarize", "aggregator": "aggregator"},
    )
    g.add_edge("uc1_summarize", "aggregator")
    g.add_edge("aggregator", END)
    return g.compile()
```

**Done when:** `graph.ainvoke({"message": "test"})` runs end-to-end with stub data.

## Phase 3 — First real node: decomposer (2 days)

### 3.1 Operation cards registry (`src/oneops/registries/operations/`)

Data files (YAML). One per operation, not per UC:

```yaml
# operations/uc01_summarization.entity_summary.yaml
operation_id: uc01_summarization.entity_summary
uc_id: uc01_summarization
execution_type: read
semantic_description: |
  Summarize an ITSM entity (incident, request, problem, change, asset, CMDB CI).
  Requires a grounded entity reference. Returns structured summary with status,
  what happened, key updates, pending actions, linked records.
handles_examples:
  - "summarize INC0001001"
  - "give me a summary of incident 1001"
  - "show details of CHG0002001"
does_not_handle_examples:
  - "find KB articles for INC0001001"
  - "VPN keeps dropping"
required_params: [entity_id, service_id]
optional_params: [summary_length]
audience: [service_desk_agent, employee, manager]
```

Loader reads `operations/*.yaml` at startup. Validates against pydantic schema. Available to decomposer at runtime.

### 3.2 Decomposer node (`src/oneops/nodes/decomposer.py`)

```python
async def decomposer_node(state: OneOpsState) -> dict:
    cards = operation_cards.list_visible(role=state["role"])
    prompt = build_decomposer_prompt(
        message=state["message"],
        focus=state["focus"],
        history=state["conversation_history"][-4:],
        operation_cards=cards,
    )
    raw = await llm_gateway.call(
        prompt=prompt,
        response_format={"type": "json_object"},
        max_tokens=512,
    )
    dag = parse_dag(raw)  # validates: known UC, valid intent, deps reference existing nodes
    return {"dag": dag, "decomposer_reasoning": raw.get("reasoning", "")}
```

Decomposer prompt structure:
- System: rules (output is a DAG; refuse out-of-domain; resolve pronouns via focus)
- Operation cards: name + semantic_description + examples
- Focus state + recent history
- User message
- Output schema: `{"dag": [{node_id, uc_id, intent, params, depends_on}], "reasoning": "..."}`

### 3.3 Few-shot examples (separate file)

`src/oneops/nodes/prompts/decomposer_examples.py` — examples of:
- Single UC, simple
- Single UC with focus inheritance ("priority?" after summary)
- Two UCs parallel
- Two UCs sequential with deps
- Refusal (out of domain)
- Refusal (boundary — UC-3 won't synthesize solutions)

**Done when:** decomposer correctly emits DAGs for 15+ test queries spanning the patterns above. Validation: golden-file tests against a curated query corpus.

## Phase 4 — UC-1 summarize node (3 days)

### 4.1 UC folder structure

```
src/oneops/use_cases/uc01_summarization/
├── node.py              # async def uc1_node(state) — the LangGraph node
├── tools.py             # ported from POC3 itsm_tools (summarize_entity, get_ticket_details, ...)
├── prompts/
│   ├── full_summary.md  # the agent instruction block
│   └── field_read.md
├── catalog.yaml         # field catalog (schema-described, no aliases)
└── operations/          # YAML cards for this UC's operations
    ├── entity_summary.yaml
    └── field_read.yaml
```

### 4.2 Node implementation pattern

```python
async def uc1_node(state: OneOpsState) -> dict:
    my_nodes = [n for n in state["dag"] if n["uc_id"] == "uc01_summarization"]
    results = []
    
    for node in my_nodes:
        # Wait for dependencies (LangGraph schedules siblings in parallel naturally)
        with tracer.start_as_current_span(
            f"uc01.{node['intent']}",
            attributes={"node_id": node["node_id"], "entity_id": node["params"].get("entity_id")}
        ):
            try:
                if node["intent"] == "summary":
                    result = await execute_summary(node, state)
                elif node["intent"] == "field_read":
                    result = await execute_field_read(node, state)
                else:
                    result = make_failed_result(node, f"unknown intent {node['intent']}")
                results.append(result)
            except Exception as e:
                results.append(make_failed_result(node, str(e)))
    
    return {"node_results": results}
```

### 4.3 Tools (ported from POC3)

Wrap each existing tool with `@tool` decorator. Add OTEL spans. Add audience checks. That's it — the SQL/business logic is unchanged.

### 4.4 Agent invocation

For the summary intent, use `create_react_agent` from LangGraph or hand-roll an OpenAI tool-call loop. Either way, model calls go through `LLMGateway`.

### 4.5 Field catalog — schema-described, no aliases

```yaml
# uc01_summarization/catalog.yaml
fields:
  priority:
    description: |
      How critical the ticket is. ITIL P1-P4 scale where P1 is highest.
      Users commonly call this priority, urgency, criticality, importance, or P-level.
      Distinct from severity (technical magnitude) and impact (business scope).
    db_column: priority
    source_tool: get_ticket_details
    type: scalar
  
  related_incidents:
    description: |
      Incident IDs linked to this problem or change. List type.
      Users ask about this as related incidents, linked tickets, caused by.
    db_column: related_incidents
    source_tool: get_ticket_links
    type: list
```

Field resolution is semantic (LLM over descriptions), not alias matching.

**Done when:** `summarize INC0001001` returns a clean structured summary; `priority?` follow-up returns priority via field-read; trace shows full span tree (decomposer → uc1 → llm calls → tool calls).

## Phase 5 — UCInvoker abstraction (1 day)

Introduce the in-process vs NATS split:

```python
# src/oneops/invoker/base.py
class UCInvoker(Protocol):
    async def invoke(self, uc_id: str, intent: str, params: dict, ctx: TraceContext) -> NodeResult: ...

# src/oneops/invoker/local.py
class LocalInvoker:
    """Direct in-process call. For dev + small deployments."""
    
# src/oneops/invoker/nats.py
class NATSInvoker:
    """NATS req/reply. For microservice deployments."""
```

The LangGraph UC nodes are now thin: they just call `invoker.invoke(...)`. The actual UC logic can be local or remote. Configuration in `.env` decides.

**Done when:** same test passes with `INVOKER=local` and `INVOKER=nats` (when a UC-1 service runs alongside).

## Phase 6 — Add UC-3 KB lookup (3 days)

Same pattern as UC-1:
- Folder: `src/oneops/use_cases/uc03_kb_lookup/`
- Port tools from POC3 (search_kb, get_kb_article, find_kb_articles_for_ticket)
- Write operation cards
- Write field catalog (no aliases)
- Decomposer prompt examples updated

**Done when:** standalone KB search works (`how do I reset MFA?`).

## Phase 7 — Multi-UC parallel + sequential (2 days)

The real test of the architecture:

| Query | Expected DAG |
|---|---|
| `summarize INC0001001 and find KB articles for it` | `[uc1:summary(INC0001001)] → [uc3:ticket_kb(INC0001001)]` (sequential) |
| `summarize INC0001001 and CHG0004007` | `[uc1:summary(INC0001001), uc1:summary(CHG0004007)]` (parallel) |
| `summarize INC0001001, find similar tickets, suggest a fix` | `[uc1] → [uc2] → [uc7]` (chain, UC-7 needs both) |

LangGraph's conditional edges + parallel-node execution handle this natively. The decomposer is what determines the shape; the executor follows the deps.

**Done when:** the 10+ multi-UC test queries in the corpus all pass.

## Phase 8 — Production hardening (1 week)

In this order:

1. **RBAC adapter** — tool filtering per role × tenant at registry level. Tools refuse if caller's role isn't in `audience`.
2. **Cache wiring** — response cache + LLM Gateway replay cache. Cache key includes prompt-version hash.
3. **Boundary enforcement** — UC operation cards declare `excluded_intents` (UC-3 doesn't synthesize solutions). Decomposer prompt knows this; refuses to emit forbidden nodes.
4. **Stress test corpus** — port the 117+72 = 189 stress probes from POC3 as integration tests. Add 30+ multi-UC probes.
5. **OTEL dashboards** — Grafana dashboards for: request latency p50/p95/p99 per UC, DAG node counts, decomposer success rate, tool error rate.
6. **Health endpoints** — `/healthz`, `/readyz`, `/metrics` (Prometheus).
7. **Deployment manifests** — Dockerfile per service, k8s manifests, NATS-bus deployment.

**Done when:** stress suite passes ≥95%, OTEL traces complete end-to-end, services deploy independently.

## What NOT to do during the rebuild

| Anti-pattern | Why it's wrong | Right move |
|---|---|---|
| Port `_LINKED_ENTITY_QUESTION_RE` and similar regexes | Phrase lists fail on plurals + synonyms + multilingual | Semantic resolution via catalog descriptions |
| Port alias lists in field catalog | "user says severity, schema says priority" never works | Description-based + LLM mapping |
| Recreate 5-pack-per-UC pattern | Bridge/planner/preprocessor packs were workarounds for a missing decomposer | One UC folder = node + tools + prompts + cards + catalog |
| Hardcode UC names in graph builder | Common file overloading; violates CLAUDE.md rule #1 | Conditional edges read from `state.dag[*].uc_id`; node names match UC IDs |
| Skip OTEL "for now" | Custom audit is what we're moving AWAY from | OTEL from day 1; spans are cheap |
| Direct `openai.OpenAI()` calls | Bypasses gateway → no central rate limits, no replay, no cost tracking | All LLM calls through `LLMGateway` |
| Inline NATS calls inside UC logic | Couples UC to transport; breaks FaaS mode | Always through `UCInvoker` interface |

## Timeline

| Phase | Duration | Cumulative |
|---|---|---|
| 0 — Scaffolding | 0.5d | 0.5d |
| 1 — Adapters | 2d | 2.5d |
| 2 — State + stub graph | 1d | 3.5d |
| 3 — Decomposer | 2d | 5.5d |
| 4 — UC-1 node | 3d | 8.5d |
| 5 — UCInvoker | 1d | 9.5d |
| 6 — UC-3 node | 3d | 12.5d |
| 7 — Multi-UC DAG | 2d | 14.5d |
| 8 — Production hardening | 5-7d | ~20d |

**~4 weeks from empty folder to production-grade UC-1 + UC-3 with multi-UC orchestration on the new stack.**

## Start NOW

```bash
cd "/home/kevin-parashar/AI-services/POC copy 4"

# Step 0.1: scaffold
mkdir -p src/oneops/{state,graph,nodes,tools,adapters,gateway,registries,proto,observability,invoker,use_cases} tests/{unit,integration,stress} ops/{nats,otel} config docs/03_nodes
touch src/oneops/__init__.py

# Step 0.2: pyproject.toml — copy the template above into this file
nano pyproject.toml

# Step 0.3: virtualenv + install
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[test,dev]"

# Step 0.4: bring up local stack
nano docker-compose.yml      # copy the compose template above
docker compose up -d

# Step 0.5: verify
python -c "import langgraph; import openai; import nats; import opentelemetry; print('ok')"
```

After this 30-minute setup, write Phase 1 adapters. That's the right starting point. Don't write UC code until Phase 1 is done — it makes everything else trivial.
