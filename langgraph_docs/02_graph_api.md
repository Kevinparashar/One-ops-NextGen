# LangGraph Graph API

> Source: https://docs.langchain.com/oss/python/langgraph/graph-api

The Graph API is the primary way to build agents in LangGraph. Agent workflows are modeled as graphs with three key components:

1. **State** — Shared data structure (snapshot of your application)
2. **Nodes** — Python functions encoding agent logic (receive state → return updated state)
3. **Edges** — Routing functions determining which node to execute next

> "In short: nodes do the work, edges tell what to do next."

LangGraph uses **message passing** (inspired by Google's Pregel). Nodes execute in discrete **super-steps**. Nodes with multiple outgoing edges trigger those destination nodes **in parallel**.

---

## State

The `State` is the input schema for all nodes and edges. Defined as a `TypedDict`, `dataclass`, or Pydantic `BaseModel`.

```python
from typing_extensions import TypedDict

class State(TypedDict):
    foo: int
    bar: list[str]
```

### Reducers

Each state key has a reducer function determining how updates are applied.

```python
from typing import Annotated
from operator import add

class State(TypedDict):
    foo: int
    bar: Annotated[list[str], add]  # accumulates instead of overwriting
```

### MessagesState (prebuilt)

A prebuilt state with a `messages` key using the `add_messages` reducer:

```python
from langgraph.graph import MessagesState

class State(MessagesState):
    documents: list[str]  # extend with extra fields
```

### Multiple Schemas (Input / Output / Private)

```python
class InputState(TypedDict):
    user_input: str

class OutputState(TypedDict):
    graph_output: str

class OverallState(TypedDict):
    foo: str
    user_input: str
    graph_output: str

builder = StateGraph(OverallState, input_schema=InputState, output_schema=OutputState)
```

---

## Nodes

Nodes are Python functions (sync or async). They accept:
1. `state` — the current graph state
2. `config` — `RunnableConfig` (thread_id, tags, etc.)
3. `runtime` — `Runtime` object (context, store, stream_writer)

```python
from langgraph.runtime import Runtime

def node_with_runtime(state: State, runtime: Runtime[Context]):
    print(runtime.context.user_id)
    return {"results": f"Hello, {state['input']}!"}
```

### Node Caching

```python
from langgraph.types import CachePolicy
from langgraph.cache.memory import InMemoryCache

builder.add_node("expensive_node", expensive_node, cache_policy=CachePolicy(ttl=3))
graph = builder.compile(cache=InMemoryCache())
```

---

## Edges

### Normal Edges

```python
graph.add_edge("node_a", "node_b")
```

### Conditional Edges

```python
graph.add_conditional_edges("node_a", routing_function)
# With mapping:
graph.add_conditional_edges("node_a", routing_function, {True: "node_b", False: "node_c"})
```

### Send (Map-Reduce pattern)

```python
from langgraph.types import Send

def continue_to_jokes(state: OverallState):
    return [Send("generate_joke", {"subject": s}) for s in state['subjects']]

graph.add_conditional_edges("node_a", continue_to_jokes)
```

---

## Command (State Update + Routing in one step)

```python
from langgraph.types import Command
from typing import Literal

def my_node(state: State) -> Command[Literal["my_other_node"]]:
    return Command(
        update={"foo": "bar"},  # state update
        goto="my_other_node"    # routing
    )
```

### Resume after interrupt

```python
# Resume a paused graph
result = graph.invoke(Command(resume="yes"), config)
```

> Do NOT use `Command(update=...)` as input to continue multi-turn conversations — pass a plain dict instead.

---

## Runtime Context

Pass runtime context (not part of state) to nodes:

```python
from dataclasses import dataclass

@dataclass
class ContextSchema:
    llm_provider: str = "openai"

graph = StateGraph(State, context_schema=ContextSchema)
graph.invoke(inputs, context={"llm_provider": "anthropic"})
```

## Recursion Limit

```python
graph.invoke(inputs, config={"recursion_limit": 5})
```

Use `RemainingSteps` for proactive handling before hitting the limit.

---

## Relevance to OneOps AI Service

- **State** maps to OneOps' `PlanningRequest` / `ExecutionPlan` / `ExecutionResult` Pydantic schemas
- **Nodes** map to OneOps' specialist agents (summarization, triage, kb, etc.)
- **Conditional edges** map to OneOps' `router-alias-registry.json` routing logic
- **Send (map-reduce)** is ideal for OneOps' fanout patterns — e.g., summarizing multiple tickets in parallel
- **Command** is the LangGraph mechanism for combining state mutation + routing, similar to OneOps Planner-Executor handoff
- **Runtime Context** maps to OneOps' `RunContext` (user_id, tenant_id, role) injection pattern
