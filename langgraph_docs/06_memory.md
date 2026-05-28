# LangGraph Memory

> Source: https://docs.langchain.com/oss/python/langgraph/add-memory

LangGraph supports two types of memory:

| Type | Scope | Mechanism |
|---|---|---|
| **Short-term** | Within a thread (conversation) | Checkpointer (InMemorySaver / PostgresSaver) |
| **Long-term** | Across threads (sessions) | Store (InMemoryStore / PostgresStore) |

---

## Short-Term Memory (Thread-level Persistence)

Enables agents to track multi-turn conversations.

```python
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph

checkpointer = InMemorySaver()

builder = StateGraph(...)
graph = builder.compile(checkpointer=checkpointer)

# First turn
graph.invoke(
    {"messages": [{"role": "user", "content": "hi! I'm Bob"}]},
    {"configurable": {"thread_id": "1"}},
)

# Second turn — bot remembers "Bob"
graph.invoke(
    {"messages": [{"role": "user", "content": "what's my name?"}]},
    {"configurable": {"thread_id": "1"}},
)
```

### Production: Postgres Checkpointer

```python
from langgraph.checkpoint.postgres import PostgresSaver

DB_URI = "postgresql://postgres:postgres@localhost:5442/postgres?sslmode=disable"
with PostgresSaver.from_conn_string(DB_URI) as checkpointer:
    # checkpointer.setup()  # call once on first use
    graph = builder.compile(checkpointer=checkpointer)
```

### Production: MongoDB Checkpointer

```bash
pip install -U pymongo langgraph langgraph-checkpoint-mongodb
```

```python
from langgraph.checkpoint.mongodb import MongoDBSaver

with MongoDBSaver.from_conn_string("localhost:27017") as checkpointer:
    graph = builder.compile(checkpointer=checkpointer)
```

### Production: Redis Checkpointer

```bash
pip install -U langgraph langgraph-checkpoint-redis
```

```python
from langgraph.checkpoint.redis import RedisSaver

with RedisSaver.from_conn_string("redis://localhost:6379") as checkpointer:
    # checkpointer.setup()  # call once on first use
    graph = builder.compile(checkpointer=checkpointer)
```

### Subgraphs

Only provide checkpointer to the parent graph — LangGraph propagates it to subgraphs:

```python
graph = parent_builder.compile(checkpointer=checkpointer)
```

For subgraph-specific checkpointing (e.g., interrupt support):

```python
subgraph = subgraph_builder.compile(checkpointer=True)
```

---

## Long-Term Memory (Cross-Thread Store)

Stores user-specific or application-specific data **across sessions**.

```python
from langgraph.store.memory import InMemoryStore

store = InMemoryStore()
graph = builder.compile(store=store)
```

### Access Store in Nodes via Runtime

```python
from dataclasses import dataclass
from langgraph.runtime import Runtime
from langgraph.graph import StateGraph, MessagesState, START
import uuid

@dataclass
class Context:
    user_id: str

async def call_model(state: MessagesState, runtime: Runtime[Context]):
    user_id = runtime.context.user_id
    namespace = (user_id, "memories")

    # Search for relevant memories
    memories = await runtime.store.asearch(
        namespace, query=state["messages"][-1].content, limit=3
    )
    info = "\n".join([d.value["data"] for d in memories])

    # Store a new memory
    if "remember" in state["messages"][-1].content.lower():
        await runtime.store.aput(
            namespace, str(uuid.uuid4()), {"data": "User prefers dark mode"}
        )

builder = StateGraph(MessagesState, context_schema=Context)
builder.add_node(call_model)
builder.add_edge(START, "call_model")
graph = builder.compile(store=store)
```

### Invoke with Context

```python
graph.invoke(
    {"messages": [{"role": "user", "content": "hi"}]},
    {"configurable": {"thread_id": "1"}},
    context=Context(user_id="user_123"),
)
```

### Cross-Thread Access (Same user_id, different thread)

```python
# New thread — same user_id still accesses the same memories
config = {"configurable": {"thread_id": "2"}}
graph.stream(
    {"messages": [{"role": "user", "content": "hi, tell me about my memories"}]},
    config,
    context=Context(user_id="1"),
)
```

### Production: Postgres Store

```python
from langgraph.store.postgres.aio import AsyncPostgresStore

DB_URI = "postgresql://postgres:postgres@localhost:5442/postgres?sslmode=disable"
async with AsyncPostgresStore.from_conn_string(DB_URI) as store:
    # await store.setup()  # call once on first use
    graph = builder.compile(store=store)
```

---

## Semantic Search in the Store

Configure the store with an embedding model for meaning-based memory retrieval:

```python
from langchain.embeddings import init_embeddings

store = InMemoryStore(
    index={
        "embed": init_embeddings("openai:text-embedding-3-small"),
        "dims": 1536,
        "fields": ["$"]
    }
)

memories = store.search(
    namespace_for_memory,
    query="What does the user like to eat?",
    limit=3
)
```

In `langgraph.json` for deployment:

```json
{
    "store": {
        "index": {
            "embed": "openai:text-embeddings-3-small",
            "dims": 1536,
            "fields": ["$"]
        }
    }
}
```

---

## Relevance to OneOps AI Service

- **Short-term (checkpointer)**: Maps to OneOps' session memory for multi-turn ticket creation (UC-6), resolution approval (UC-7), and follow-up conversations
- **Long-term (store)**: Maps to OneOps' cross-session user profile (preferred language, notification settings, RBAC role) persisted across conversations
- **Namespace pattern** `(user_id, "memories")` mirrors OneOps' `tenant_id + user_id` context model
- **Semantic search in store**: Enables OneOps to retrieve relevant past tickets or resolutions for a user based on natural language similarity
- **Production checkpointer choice**: Redis (lowest latency) for session state, Postgres (auditability) for ticket-level execution history
