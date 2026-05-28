# LangGraph Persistence & Checkpointing

> Source: https://docs.langchain.com/oss/python/langgraph/persistence

LangGraph has a built-in persistence layer that saves graph state as **checkpoints**. When compiled with a checkpointer, a snapshot is saved at every step, organized into **threads**.

## Why Use Persistence?

| Feature | Description |
|---|---|
| **Human-in-the-loop** | Inspect, interrupt, and approve graph steps |
| **Memory** | Retain context across turns in a conversation thread |
| **Time travel** | Replay/fork prior graph executions for debugging |
| **Fault-tolerance** | Resume from last successful step after failure |
| **Pending writes** | Completed nodes in a failed super-step are not re-run |

---

## Core Concepts

### Threads

A **thread** is a unique ID (`thread_id`) tied to a sequence of runs. Required when invoking:

```python
config = {"configurable": {"thread_id": "1"}}
graph.invoke({"foo": "", "bar": []}, config)
```

### Checkpoints

A **checkpoint** = snapshot of graph state at a super-step boundary. Represented by `StateSnapshot`:

| Field | Description |
|---|---|
| `values` | State channel values at this checkpoint |
| `next` | Node names to execute next (empty = graph complete) |
| `config` | Contains thread_id, checkpoint_ns, checkpoint_id |
| `metadata` | source, writes (node outputs), step counter |
| `created_at` | ISO 8601 timestamp |
| `parent_config` | Config of previous checkpoint |
| `tasks` | Tasks to execute (includes subgraph snapshots) |

### Get State

```python
config = {"configurable": {"thread_id": "1"}}
graph.get_state(config)  # Latest state

# Specific checkpoint:
config = {"configurable": {"thread_id": "1", "checkpoint_id": "abc123"}}
graph.get_state(config)
```

### Get State History

```python
list(graph.get_state_history(config))
```

### Update State

```python
graph.update_state(config, {"foo": "updated_value"}, as_node="node_a")
```

---

## Memory Store (Cross-Thread)

The `Store` retains information **across threads** (e.g., user preferences across conversations).

```python
from langgraph.store.memory import InMemoryStore

store = InMemoryStore()
graph = builder.compile(checkpointer=checkpointer, store=store)
```

### Store Usage in Nodes

```python
from langgraph.runtime import Runtime
from dataclasses import dataclass

@dataclass
class Context:
    user_id: str

async def update_memory(state: MessagesState, runtime: Runtime[Context]):
    user_id = runtime.context.user_id
    namespace = (user_id, "memories")
    memory_id = str(uuid.uuid4())
    await runtime.store.aput(namespace, memory_id, {"memory": "User prefers dark mode"})
```

### Semantic Search in Store

```python
from langchain.embeddings import init_embeddings

store = InMemoryStore(
    index={
        "embed": init_embeddings("openai:text-embedding-3-small"),
        "dims": 1536,
        "fields": ["$"]
    }
)

memories = store.search(namespace, query="What does the user like?", limit=3)
```

---

## Checkpointer Libraries

| Library | Backend | Use Case |
|---|---|---|
| `langgraph-checkpoint` | In-memory (`InMemorySaver`) | Dev/testing |
| `langgraph-checkpoint-sqlite` | SQLite | Local workflows |
| `langgraph-checkpoint-postgres` | PostgreSQL | Production |
| `langgraph-checkpoint-cosmosdb` | Azure Cosmos DB | Production (Azure) |

### Production Setup (Postgres)

```python
from langgraph.checkpoint.postgres import PostgresSaver

with PostgresSaver.from_conn_string("postgresql://...") as checkpointer:
    graph = builder.compile(checkpointer=checkpointer)
```

### Encryption

```python
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.postgres import PostgresSaver

serde = EncryptedSerializer.from_pycryptodome_aes()  # reads LANGGRAPH_AES_KEY
checkpointer = PostgresSaver.from_conn_string("postgresql://...", serde=serde)
```

---

## Relevance to OneOps AI Service

- **Thread = Session**: OneOps' `session_id` maps directly to LangGraph's `thread_id` — one thread per user conversation
- **Checkpointer** must be configured with a production-grade store (Postgres/Redis) for OneOps enterprise scale
- **Store** (cross-thread) maps to OneOps' need to persist user preferences, RBAC roles, and past resolution patterns across conversations
- **State history + time travel** is critical for OneOps audit trails — every step of an AI-driven action is checkpointed
- **Fault tolerance** handles LLM provider outages gracefully — resumes from last checkpoint rather than restarting workflows from scratch
- **Encryption** (`LANGGRAPH_AES_KEY`) is essential for PII protection in ITSM ticket data (names, emails, descriptions)
