# LangGraph Subgraphs

> Source: https://docs.langchain.com/oss/python/langgraph/use-subgraphs

Subgraphs allow you to compose complex graphs from smaller, reusable graphs. A subgraph is added to a parent graph as a node — it can have its own state schema, checkpointing, and internal logic.

---

## Why Use Subgraphs?

- **Modularity**: Encapsulate complex logic into reusable components
- **Namespace isolation**: Each subgraph has its own checkpoint namespace
- **Independent state**: Subgraphs can have private state channels invisible to the parent
- **Multi-agent teams**: Each specialist agent can be a subgraph

---

## Basic Usage

```python
from langgraph.graph import StateGraph, START
from langgraph.checkpoint.memory import InMemorySaver
from typing import TypedDict

class State(TypedDict):
    foo: str

# Subgraph
def subgraph_node_1(state: State):
    return {"foo": state["foo"] + "bar"}

subgraph_builder = StateGraph(State)
subgraph_builder.add_node(subgraph_node_1)
subgraph_builder.add_edge(START, "subgraph_node_1")
subgraph = subgraph_builder.compile()

# Parent graph — add subgraph as a node
builder = StateGraph(State)
builder.add_node("subgraph_node", subgraph)
builder.add_edge(START, "subgraph_node")

checkpointer = InMemorySaver()
graph = builder.compile(checkpointer=checkpointer)
```

---

## Checkpoint Namespaces

Each subgraph checkpoint has a `checkpoint_ns` field:
- `""` — parent (root) graph
- `"node_name:uuid"` — subgraph invoked as that node
- `"outer_node:uuid|inner_node:uuid"` — nested subgraph

Access from within a node:

```python
from langchain_core.runnables import RunnableConfig

def my_node(state: State, config: RunnableConfig):
    checkpoint_ns = config["configurable"]["checkpoint_ns"]
```

---

## Navigate from Subgraph to Parent

Use `Command(graph=Command.PARENT)` to route from a subgraph node to a parent graph node:

```python
def my_node(state: State) -> Command[Literal["other_subgraph"]]:
    return Command(
        update={"foo": "bar"},
        goto="other_subgraph",
        graph=Command.PARENT  # navigate to parent graph
    )
```

> When sharing keys between parent and subgraph, the **parent must have a reducer** for that key.

---

## Subgraph Persistence

Checkpointer is automatically propagated from parent to child subgraphs — only configure it at the parent level:

```python
graph = parent_builder.compile(checkpointer=checkpointer)
```

For subgraph-specific interrupt support:

```python
subgraph = subgraph_builder.compile(checkpointer=True)
```

---

## Multi-Agent with Subgraphs

Each specialist agent can be implemented as a compiled subgraph, added to an orchestrator parent graph as a node:

```python
# Specialist agent as a subgraph
summarization_agent = build_summarization_subgraph()
triage_agent = build_triage_subgraph()

# Orchestrator
orchestrator = StateGraph(OverallState)
orchestrator.add_node("summarization", summarization_agent)
orchestrator.add_node("triage", triage_agent)
orchestrator.add_conditional_edges(START, route_to_agent, {
    "summarization": "summarization",
    "triage": "triage",
})
```

---

## Relevance to OneOps AI Service

- **Each specialist agent as a subgraph**: `summarization_agent`, `triage_agent`, `kb_agent`, `sentiment_agent` can each be compiled subgraphs with their own tool sets and state
- **Orchestrator = Planner + Router**: The parent graph implements routing logic (from `router-alias-registry.json`) while each subgraph handles its capability domain
- **Namespace isolation**: Different agents processing the same ticket won't interfere with each other's checkpoint state
- **`Command.PARENT`**: Allows specialist agents to escalate back to the orchestrator (e.g., triage agent discovers a duplicate, escalates to parent to reroute)
- **Subgraph persistence**: Subgraph checkpointers enable independent interrupt/resume within each agent's processing loop
