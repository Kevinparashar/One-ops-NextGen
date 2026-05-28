# LangGraph Functional API

> Source: https://docs.langchain.com/oss/python/langgraph/functional-api

The Functional API is an alternative to the `StateGraph` (Graph API) that lets you define workflows using **regular Python functions** with `@entrypoint` and `@task` decorators — without explicit graph construction.

---

## When to Use Functional API vs Graph API

| Aspect | Graph API (`StateGraph`) | Functional API (`@entrypoint`) |
|---|---|---|
| Style | Declarative (define nodes + edges) | Imperative (regular Python code) |
| Visualization | Graph visualization via Mermaid | Not visualized as a graph |
| Parallelization | Via multiple outgoing edges | Via multiple task futures |
| Best for | Complex routing, explicit state management | Simple sequential / conditional workflows |

---

## Core Primitives

### `@entrypoint`

Marks the main entry function of a workflow. Handles:
- Persistence (checkpointing)
- Streaming
- Interrupt/resume

```python
from langgraph.func import entrypoint

@entrypoint()
def my_workflow(input_value: str):
    # workflow logic here
    return result
```

### `@task`

Wraps a function to make it:
- Durable (results are checkpointed)
- Async-safe (non-deterministic ops are isolated)
- Parallelizable (returns a future)

```python
from langgraph.func import task

@task
def fetch_data(url: str):
    return requests.get(url).text

@task
def process_data(data: str):
    return llm.invoke(data).content
```

---

## Sequential Workflow

```python
from langgraph.func import entrypoint, task

@task
def generate_joke(topic: str):
    return llm.invoke(f"Write a joke about {topic}").content

@task
def improve_joke(joke: str):
    return llm.invoke(f"Make this joke funnier: {joke}").content

@entrypoint()
def workflow(topic: str):
    joke = generate_joke(topic).result()
    improved = improve_joke(joke).result()
    return improved

# Execute
for step in workflow.stream("cats", stream_mode="updates"):
    print(step)
```

---

## Parallel Execution

Launch multiple tasks simultaneously, collect results:

```python
@task
def call_llm_1(topic: str):
    return llm.invoke(f"Write a joke about {topic}").content

@task
def call_llm_2(topic: str):
    return llm.invoke(f"Write a story about {topic}").content

@task
def call_llm_3(topic: str):
    return llm.invoke(f"Write a poem about {topic}").content

@entrypoint()
def parallel_workflow(topic: str):
    joke_fut = call_llm_1(topic)    # starts immediately
    story_fut = call_llm_2(topic)   # starts immediately
    poem_fut = call_llm_3(topic)    # starts immediately

    # collect results
    return {
        "joke": joke_fut.result(),
        "story": story_fut.result(),
        "poem": poem_fut.result(),
    }
```

---

## With Persistence (Checkpointing)

```python
from langgraph.checkpoint.memory import InMemorySaver
import uuid

checkpointer = InMemorySaver()

@entrypoint(checkpointer=checkpointer)
def my_workflow(input_value: str):
    result = fetch_data(input_value).result()
    return process_data(result).result()

# Run with thread_id
thread_id = str(uuid.uuid4())
config = {"configurable": {"thread_id": thread_id}}
my_workflow.invoke("some input", config)
```

---

## With Interrupts

```python
from langgraph.types import interrupt, Command

@entrypoint(checkpointer=InMemorySaver())
def approval_workflow(action: str):
    # Pause and wait for human approval
    approved = interrupt(f"Do you approve: {action}?")
    if approved:
        return execute_action(action).result()
    return "Action cancelled"

config = {"configurable": {"thread_id": "1"}}

# First call — pauses at interrupt
result = approval_workflow.invoke("send_email", config)

# Resume with approval
result = approval_workflow.invoke(Command(resume=True), config)
```

---

## Common Pitfalls

### DO NOT put side effects outside tasks

```python
# WRONG — side effect runs on every replay
@entrypoint()
def bad_workflow(url: str):
    result = requests.get(url).text  # not wrapped in @task!
    return result
```

```python
# CORRECT
@task
def fetch(url: str):
    return requests.get(url).text

@entrypoint()
def good_workflow(url: str):
    return fetch(url).result()
```

### DO NOT use mutable shared state

Each `@task` should be a pure function with no shared mutable state.

---

## Resuming After an Error

If a task fails, resume from the last checkpoint by passing `None`:

```python
# Resume after failure
my_workflow.invoke(None, config)
```

---

## Relevance to OneOps AI Service

- **Functional API** is well-suited for OneOps' **summarization pipeline** — a clear sequential flow: `get_ticket_details` → `get_timeline` → `get_links` → `summarize_entity` → `put_cached_summary`
- **`@task` wrapping** is exactly what each OneOps tool implementation should use — it ensures ITSM API calls are isolated for safe replay
- **Parallel tasks** enable concurrent fetching: `get_ticket_links` and `get_ticket_attachment_metadata` can fire in parallel before `summarize_entity`
- **`@entrypoint` with checkpointer** maps to OneOps' session-aware agent execution — each session has a `thread_id`
- The Functional API is **simpler to implement initially** for OneOps before migrating to full `StateGraph` with multi-agent subgraphs
