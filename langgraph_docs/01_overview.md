# LangGraph Overview

> Source: https://langchain-ai.github.io/langgraph/

LangGraph is a low-level orchestration framework and runtime for building, managing, and deploying long-running, stateful agents. It is very low-level and focused entirely on agent orchestration — it does not abstract prompts or architecture. Trusted by Klarna, Uber, J.P. Morgan, and more.

Before using LangGraph, familiarize yourself with [models](https://docs.langchain.com/oss/python/langchain/models) and [tools](https://docs.langchain.com/oss/python/langchain/tools).

## Install

```bash
pip install -U langgraph
```

## Hello World Example

```python
from langgraph.graph import StateGraph, MessagesState, START, END

def mock_llm(state: MessagesState):
    return {"messages": [{"role": "ai", "content": "hello world"}]}

graph = StateGraph(MessagesState)
graph.add_node(mock_llm)
graph.add_edge(START, "mock_llm")
graph.add_edge("mock_llm", END)
graph = graph.compile()

graph.invoke({"messages": [{"role": "user", "content": "hi!"}]})
```

## Core Benefits

| Benefit | Description |
|---|---|
| **Durable execution** | Persist through failures; resume from where it left off |
| **Human-in-the-loop** | Inspect and modify agent state at any point |
| **Comprehensive memory** | Short-term working memory + long-term memory across sessions |
| **Debugging with LangSmith** | Visualize traces, capture state transitions, runtime metrics |
| **Production-ready deployment** | Scalable infrastructure for stateful, long-running workflows |

## LangGraph Ecosystem

- **LangGraph** — core orchestration framework
- **LangSmith** — observability, tracing, evaluation, deployment
- **LangChain** — integrations and composable components for LLM applications

LangGraph is inspired by [Pregel](https://research.google/pubs/pub37252/) and [Apache Beam](https://beam.apache.org/), and draws interface inspiration from [NetworkX](https://networkx.org/).

## Workflows vs Agents

- **Workflows** have predetermined code paths and operate in a certain order
- **Agents** are dynamic and define their own processes and tool usage

---

## Relevance to OneOps AI Service

- LangGraph is an **alternative orchestration framework** to Agno — comparing them is valuable
- Its **Planner-Executor model** can be mapped to LangGraph's `StateGraph` with conditional routing
- LangGraph's `thread_id` maps directly to OneOps session management for multi-turn conversations
- The `interrupt()` primitive is the LangGraph equivalent for human-in-the-loop flows (e.g., ticket creation confirmation)
- LangGraph's `Store` (cross-thread memory) maps to OneOps' need for user preference and context persistence
