# LangGraph Observability with LangSmith

> Source: https://docs.langchain.com/oss/python/langgraph/observability

LangGraph integrates with **LangSmith** for tracing, debugging, and monitoring agent behavior.

---

## Enable Tracing

```bash
export LANGSMITH_TRACING=true
export LANGSMITH_API_KEY=<your-api-key>
export LANGSMITH_PROJECT=my-agent-project  # optional; defaults to "default"
```

All LangGraph executions are automatically traced once `LANGSMITH_TRACING=true` is set.

---

## Selective Tracing

Use `tracing_context` to trace only specific invocations:

```python
import langsmith as ls

with ls.tracing_context(enabled=True):
    agent.invoke({"messages": [{"role": "user", "content": "Send email to alice@example.com"}]})

# This call is NOT traced (unless LANGSMITH_TRACING is set globally)
agent.invoke({"messages": [{"role": "user", "content": "Send another email"}]})
```

---

## Log to a Specific Project

### Statically (environment variable)

```bash
export LANGSMITH_PROJECT=my-agent-project
```

### Dynamically (per-invocation)

```python
import langsmith as ls

with ls.tracing_context(project_name="email-agent-test", enabled=True):
    response = agent.invoke({"messages": [{"role": "user", "content": "Send welcome email"}]})
```

---

## Add Metadata and Tags to Traces

```python
response = agent.invoke(
    {"messages": [{"role": "user", "content": "Send a welcome email"}]},
    config={
        "tags": ["production", "email-assistant", "v1.0"],
        "metadata": {
            "user_id": "user_123",
            "session_id": "session_456",
            "environment": "production"
        }
    }
)
```

With `tracing_context`:

```python
with ls.tracing_context(
    project_name="email-agent-test",
    enabled=True,
    tags=["production"],
    metadata={"user_id": "user_123", "session_id": "session_456"}
):
    response = agent.invoke(...)
```

---

## PII / Sensitive Data Masking (Anonymizers)

Use anonymizers to prevent sensitive data from being logged to LangSmith:

```python
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client
from langsmith.anonymizer import create_anonymizer

anonymizer = create_anonymizer([
    # Mask Social Security Numbers
    {"pattern": r"\b\d{3}-?\d{2}-?\d{4}\b", "replace": "<ssn>"},
    # Add more patterns as needed
])

tracer_client = Client(anonymizer=anonymizer)
tracer = LangChainTracer(client=tracer_client)

graph = (
    StateGraph(MessagesState)
    ...
    .compile()
    .with_config({'callbacks': [tracer]})
)
```

---

## What LangSmith Provides

- **Trace visualization**: Every node execution, LLM call, tool invocation shown as a timeline
- **State transitions**: Inspect what state was before and after each node
- **Runtime metrics**: Latency, token counts, costs per step
- **Debug locally**: [Studio UI](https://docs.langchain.com/langsmith/studio) for local development
- **Evaluation**: Run evaluators on traces to score output quality
- **Monitoring dashboards**: Production dashboards for latency, error rates, usage

---

## Common Error Codes

| Error Code | Cause |
|---|---|
| `GRAPH_RECURSION_LIMIT` | Graph exceeded max steps (default 1000) |
| `INVALID_CHAT_HISTORY` | Malformed message history |
| `INVALID_CONCURRENT_GRAPH_UPDATE` | Concurrent state updates conflict |
| `INVALID_GRAPH_NODE_RETURN_VALUE` | Node returned unexpected type |
| `MISSING_CHECKPOINTER` | Interrupt used without checkpointer |
| `MULTIPLE_SUBGRAPHS` | Unsupported nested subgraph configuration |
| `MODEL_RATE_LIMIT` | LLM provider rate limit hit |
| `MODEL_NOT_FOUND` | Specified model doesn't exist |

---

## Relevance to OneOps AI Service

- **Per-request tracing**: Every OneOps AI request (`ticket_id`, `tenant_id`, `user_id`) should be passed as LangSmith `metadata` for searchable traces
- **PII masking**: Anonymize ticket descriptions, user names, and emails before sending traces to LangSmith — critical for ITSM compliance
- **Tags for filtering**: Tag traces by agent type (`triage`, `summarization`, `kb`) and environment (`prod`, `staging`) for organized debugging
- **GRAPH_RECURSION_LIMIT**: OneOps must set appropriate recursion limits for complex workflows and handle `GraphRecursionError` gracefully
- **Evaluation**: LangSmith evaluations can score OneOps summary quality, triage accuracy, and KB relevance against ground truth
- **LangSmith Studio**: Use for rapid iteration and debugging during development of new agents and tools
