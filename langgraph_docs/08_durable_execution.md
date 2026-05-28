# LangGraph Durable Execution

> Source: https://docs.langchain.com/oss/python/langgraph/durable-execution

Durable execution saves progress at key points, allowing a process to pause and **resume exactly where it left off** — even after a significant delay (e.g., a week later). Critical for human-in-the-loop and long-running task recovery.

LangGraph's built-in persistence layer provides durable execution automatically when you use a checkpointer.

---

## Requirements

1. Enable [persistence](https://docs.langchain.com/oss/python/langgraph/persistence) by configuring a checkpointer
2. Specify a `thread_id` when executing — tracks execution history for that instance
3. Wrap non-deterministic operations and side effects inside **tasks** to ensure consistent replay

---

## Determinism and Consistent Replay

When resuming a workflow, code does NOT resume from the same line it stopped — it **replays from an appropriate starting point**.

### Rules

- **Avoid repeating work**: Wrap each side effect (API calls, file writes) in a separate `@task`
- **Encapsulate non-deterministic ops**: Wrap random number generation inside tasks
- **Use idempotent operations**: Ensure side effects produce the same result if retried

### Before (problematic — side effects re-run on replay)

```python
def call_api(state: State):
    result = requests.get(state['url']).text[:100]  # re-runs on every replay!
    return {"result": result}
```

### After (correct — side effect wrapped in task)

```python
from langgraph.func import task

@task
def _make_request(url: str):
    return requests.get(url).text[:100]

def call_api(state: State):
    request = _make_request(state['url'])
    return {"result": request.result()}
```

---

## Durability Modes

Configure the tradeoff between performance and data safety:

```python
graph.stream(
    {"input": "test"},
    durability="sync"  # or "exit" or "async"
)
```

| Mode | Behavior | Use Case |
|---|---|---|
| `"exit"` | Persists only when graph execution exits (success, error, or interrupt) | Best performance; no mid-run recovery |
| `"async"` | Persists asynchronously while next step runs | Good performance + durability; small crash risk |
| `"sync"` | Persists synchronously before next step starts | Max durability; some performance overhead |

---

## Starting Points for Resuming

| API | Starting point on resume |
|---|---|
| `StateGraph` | Beginning of the **node** where execution stopped |
| Subgraph call inside a node | Beginning of the **parent node** that called the subgraph |
| `Functional API` | Beginning of the **entrypoint** where execution stopped |

---

## Resuming Workflows

### After an interrupt

```python
from langgraph.types import Command

# Resume after interrupt
graph.invoke(Command(resume="approved"), config)
```

### After a failure

```python
# Resume from last successful checkpoint by passing None as input
graph.invoke(None, config)
```

---

## Relevance to OneOps AI Service

- **Multi-step ticket workflows**: The summarization pipeline (5 tools in sequence) benefits from durable execution — if the LLM call in `summarize_entity` times out, the execution resumes from that step without re-fetching ticket data
- **Human-in-the-loop flows**: Ticket creation confirmation (UC-6) may have a delay between interrupt and resume (user steps away) — durable execution keeps the state alive
- **Idempotent tools**: OneOps tools like `assign_entity` and `resolve_entity` must be idempotent since they may be retried after a failure checkpoint
- **Durability mode selection**: Use `"async"` for most OneOps flows (good tradeoff), `"sync"` for critical write operations like `resolve_entity` to guarantee checkpointing before the next node runs
- **`@task` wrapping**: All external API calls in OneOps tool implementations should be wrapped in `@task` to enable safe replay
