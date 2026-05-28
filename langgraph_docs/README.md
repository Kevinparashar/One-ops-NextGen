# LangGraph Documentation

> Fetched from https://langchain-ai.github.io/langgraph/ and https://docs.langchain.com/oss/python/langgraph/
> Stored: April 2, 2026

LangGraph is a low-level orchestration framework for building stateful, multi-actor LLM applications. It is an alternative to Agno and focuses on **graph-based** agent orchestration with durable execution, streaming, and human-in-the-loop support.

---

## Files in This Folder

| File | Topic | Key Concepts |
|---|---|---|
| `01_overview.md` | LangGraph Overview | Install, core benefits, ecosystem |
| `02_graph_api.md` | Graph API | State, Nodes, Edges, Command, Send, Runtime Context |
| `03_persistence.md` | Persistence & Checkpointing | Threads, checkpoints, memory store, checkpointer libraries |
| `04_streaming.md` | Streaming | Stream modes (values, updates, messages, custom), v2 format |
| `05_interrupts_human_in_loop.md` | Interrupts / HITL | `interrupt()`, `Command(resume=...)`, approval workflows, parallel interrupts |
| `06_memory.md` | Memory | Short-term (thread), long-term (store), Postgres/Redis/MongoDB backends |
| `07_workflows_agents.md` | Workflows & Agents | Prompt chaining, parallelization, routing, orchestrator-worker, evaluator-optimizer, ReAct agent |
| `08_durable_execution.md` | Durable Execution | Determinism, replay safety, durability modes (exit/async/sync), `@task` wrapping |
| `09_subgraphs.md` | Subgraphs | Composable agents, namespace isolation, `Command.PARENT`, multi-agent architecture |
| `10_functional_api.md` | Functional API | `@entrypoint`, `@task`, sequential + parallel, pitfalls |
| `11_observability.md` | Observability | LangSmith tracing, PII masking, error codes, metadata/tags |

---

## Quick Reference: LangGraph vs Agno

| Capability | LangGraph | Agno |
|---|---|---|
| **Graph model** | Explicit StateGraph (nodes + edges) | Agent/Team classes with modes |
| **State** | TypedDict / dataclass / Pydantic | Pydantic models |
| **Routing** | Conditional edges / `Command.goto` | Team modes: coordinate, route, broadcast |
| **Parallelism** | Multiple outgoing edges from a node | `broadcast` mode or parallel tasks |
| **Memory** | Checkpointer (short) + Store (long) | Session (short) + Memory (long) |
| **HITL** | `interrupt()` + `Command(resume=...)` | Custom guardrails / hooks |
| **Streaming** | 7 stream modes, v2 unified format | Built-in stream support |
| **Observability** | LangSmith (first-class integration) | External tracing |
| **Deployment** | LangSmith Agent Server | Custom deployment |

---

## Mapping to OneOps AI Service Architecture

| OneOps Concept | LangGraph Equivalent |
|---|---|
| `session_id` | `thread_id` in `configurable` |
| `PlanningRequest` / `ExecutionPlan` | `State` TypedDict |
| Planner Agent | Orchestrator node with routing logic |
| Specialist Agents | Subgraphs (compiled `StateGraph` instances) |
| Tool execution | Nodes calling `@task`-wrapped functions |
| `RunContext` (tenant_id, user_id) | `Runtime[Context]` with `context_schema` |
| ITSM write operations (assign, resolve) | Nodes with `interrupt()` approval gate |
| Cache check + generate + cache put | Sequential `@task` chain in `@entrypoint` |
| `router-alias-registry.json` | `add_conditional_edges` with routing function |
| `agent-tool-mapping.json` | Tool binding per subgraph |

---

## Installation

```bash
pip install -U langgraph

# Production checkpointers
pip install langgraph-checkpoint-postgres
pip install langgraph-checkpoint-redis

# Observability
pip install langsmith
```

---

## Key Environment Variables

```bash
# LangSmith tracing
export LANGSMITH_TRACING=true
export LANGSMITH_API_KEY=<your-api-key>
export LANGSMITH_PROJECT=oneops-ai-service

# Checkpointer encryption
export LANGGRAPH_AES_KEY=<your-aes-key>
```
