# LangGraph Streaming

> Source: https://docs.langchain.com/oss/python/langgraph/streaming

LangGraph implements streaming to surface real-time updates, improving UX by displaying output progressively before a complete response is ready.

## Basic Usage

```python
for chunk in graph.stream(
    {"topic": "ice cream"},
    stream_mode=["updates", "custom"],
    version="v2",
):
    if chunk["type"] == "updates":
        for node_name, state in chunk["data"].items():
            print(f"Node {node_name} updated: {state}")
    elif chunk["type"] == "custom":
        print(f"Status: {chunk['data']['status']}")
```

## Stream Output Format (v2 — Recommended)

Requires LangGraph >= 1.1. Pass `version="v2"` to get a unified format:

```python
{
    "type": "values" | "updates" | "messages" | "custom" | "checkpoints" | "tasks" | "debug",
    "ns": (),      # namespace tuple (populated for subgraph events)
    "data": ...,   # payload (type varies by stream mode)
}
```

---

## Stream Modes

| Mode | Type | Description |
|---|---|---|
| `values` | `ValuesStreamPart` | Full state snapshot after each step |
| `updates` | `UpdatesStreamPart` | Only changed keys from each node |
| `messages` | `MessagesStreamPart` | LLM tokens as `(message_chunk, metadata)` tuples |
| `custom` | `CustomStreamPart` | Arbitrary data via `get_stream_writer()` |
| `checkpoints` | `CheckpointStreamPart` | Checkpoint events (requires checkpointer) |
| `tasks` | `TasksStreamPart` | Task start/finish events with results/errors |
| `debug` | `DebugStreamPart` | All info: checkpoints + tasks + extra metadata |

### values mode

```python
for chunk in graph.stream({"topic": "ice cream"}, stream_mode="values", version="v2"):
    if chunk["type"] == "values":
        print(f"topic: {chunk['data']['topic']}, joke: {chunk['data']['joke']}")
```

### updates mode

```python
for chunk in graph.stream({"topic": "ice cream"}, stream_mode="updates", version="v2"):
    if chunk["type"] == "updates":
        for node_name, state in chunk["data"].items():
            print(f"Node `{node_name}` updated: {state}")
```

### messages mode (LLM token streaming)

```python
for chunk in graph.stream(
    {"topic": "ice cream"},
    stream_mode="messages",
    version="v2",
):
    if chunk["type"] == "messages":
        message_chunk, metadata = chunk["data"]
        if message_chunk.content:
            print(message_chunk.content, end="|", flush=True)
```

### custom mode

```python
from langgraph.config import get_stream_writer

def generate_joke(state: State):
    writer = get_stream_writer()
    writer({"status": "thinking of a joke..."})
    return {"joke": f"Why did the {state['topic']} go to school?"}
```

### Filter by LLM Tag

```python
joke_model = init_chat_model(model="gpt-4.1-mini", tags=["joke"])

async for chunk in graph.astream({"topic": "cats"}, stream_mode="messages", version="v2"):
    if chunk["type"] == "messages":
        msg, metadata = chunk["data"]
        if metadata["tags"] == ["joke"]:
            print(msg.content, end="|", flush=True)
```

### Omit messages from stream (nostream tag)

```python
internal_model = ChatAnthropic(model_name="claude-3-haiku").with_config(
    {"tags": ["nostream"]}
)
```

---

## Async Streaming

```python
async for chunk in graph.astream(
    {"messages": [{"role": "user", "content": "hi!"}]},
    stream_mode="updates",
    version="v2",
):
    ...
```

> For Python < 3.11, explicitly pass `RunnableConfig` to async calls.

---

## Relevance to OneOps AI Service

- **`updates` mode** is ideal for OneOps' step-by-step execution feedback — e.g., showing "Running triage agent..." as each agent processes a ticket
- **`messages` mode** enables real-time token streaming for agent-generated summaries and KB article responses
- **`custom` mode** supports OneOps' progress indicators (e.g., "Fetching ticket timeline...", "Generating summary...")
- **`checkpoints` mode** exposes every state snapshot — useful for OneOps audit trails and debugging
- **Tag filtering** lets OneOps separate "internal reasoning" LLM calls (nostream) from "user-facing response" calls (streamed)
