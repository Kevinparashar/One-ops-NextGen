# LangGraph Workflows & Agents

> Source: https://docs.langchain.com/oss/python/langgraph/workflows-agents

LangGraph supports building both **workflows** (predetermined paths) and **agents** (dynamic, self-directing). Both use the same `StateGraph` API.

---

## Common Patterns

### 1. Prompt Chaining

Each LLM call processes the output of the previous. Used for well-defined tasks broken into verifiable steps.

```python
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END

class State(TypedDict):
    topic: str
    joke: str
    improved_joke: str
    final_joke: str

def generate_joke(state: State):
    msg = llm.invoke(f"Write a short joke about {state['topic']}")
    return {"joke": msg.content}

def check_punchline(state: State):
    if "?" in state["joke"] or "!" in state["joke"]:
        return "Pass"
    return "Fail"

def improve_joke(state: State):
    msg = llm.invoke(f"Make this joke funnier: {state['joke']}")
    return {"improved_joke": msg.content}

workflow = StateGraph(State)
workflow.add_node("generate_joke", generate_joke)
workflow.add_node("improve_joke", improve_joke)
workflow.add_edge(START, "generate_joke")
workflow.add_conditional_edges(
    "generate_joke", check_punchline, {"Fail": "improve_joke", "Pass": END}
)
workflow.add_edge("improve_joke", END)
chain = workflow.compile()
```

---

### 2. Parallelization

Run multiple LLM calls simultaneously (fan-out), then aggregate (fan-in).

```python
class State(TypedDict):
    topic: str
    joke: str
    story: str
    poem: str
    combined_output: str

parallel_builder = StateGraph(State)
parallel_builder.add_node("call_llm_1", call_llm_1)
parallel_builder.add_node("call_llm_2", call_llm_2)
parallel_builder.add_node("call_llm_3", call_llm_3)
parallel_builder.add_node("aggregator", aggregator)

# All three fire from START in parallel
parallel_builder.add_edge(START, "call_llm_1")
parallel_builder.add_edge(START, "call_llm_2")
parallel_builder.add_edge(START, "call_llm_3")
# All feed into aggregator
parallel_builder.add_edge("call_llm_1", "aggregator")
parallel_builder.add_edge("call_llm_2", "aggregator")
parallel_builder.add_edge("call_llm_3", "aggregator")
parallel_builder.add_edge("aggregator", END)
```

---

### 3. Routing

Process input, then direct to a specialized subflow based on content type.

```python
from pydantic import BaseModel, Field
from typing_extensions import Literal

class Route(BaseModel):
    step: Literal["poem", "story", "joke"] = Field(description="The next step")

router = llm.with_structured_output(Route)

def llm_call_router(state: State):
    decision = router.invoke([
        SystemMessage(content="Route the input to story, joke, or poem."),
        HumanMessage(content=state["input"]),
    ])
    return {"decision": decision.step}

def route_decision(state: State):
    if state["decision"] == "story": return "llm_call_1"
    elif state["decision"] == "joke": return "llm_call_2"
    elif state["decision"] == "poem": return "llm_call_3"

router_builder.add_edge(START, "llm_call_router")
router_builder.add_conditional_edges("llm_call_router", route_decision, {
    "llm_call_1": "llm_call_1",
    "llm_call_2": "llm_call_2",
    "llm_call_3": "llm_call_3",
})
```

---

### 4. Orchestrator-Worker (Map-Reduce)

An orchestrator LLM breaks work into subtasks and assigns them to worker nodes.

```python
class State(TypedDict):
    topic: str
    sections: list[str]
    completed_sections: Annotated[list, operator.add]
    final_report: str

def orchestrator(state: State):
    plan = planner.invoke([
        SystemMessage(content="Generate a plan of sections for the report."),
        HumanMessage(content=state["topic"])
    ])
    return {"sections": plan.sections}

def assign_workers(state: State):
    return [Send("llm_call", {"section": s, "topic": state["topic"]})
            for s in state["sections"]]

def llm_call(state: WorkerState):
    result = llm.invoke(f"Write a section: {state['section']}")
    return {"completed_sections": [result.content]}

def synthesizer(state: State):
    return {"final_report": "\n\n---\n\n".join(state["completed_sections"])}

builder.add_node("orchestrator", orchestrator)
builder.add_node("llm_call", llm_call)
builder.add_node("synthesizer", synthesizer)
builder.add_edge(START, "orchestrator")
builder.add_conditional_edges("orchestrator", assign_workers, ["llm_call"])
builder.add_edge("llm_call", "synthesizer")
builder.add_edge("synthesizer", END)
```

---

### 5. Evaluator-Optimizer (Reflexion Loop)

Generate → evaluate → retry until quality threshold is met.

```python
def llm_call_generator(state: State):
    result = llm.invoke(f"Write a joke about {state['topic']}")
    return {"joke": result.content, "feedback": None}

def llm_call_evaluator(state: State):
    feedback = evaluator.invoke([
        SystemMessage(content="Evaluate this joke. Return 'Funny' or 'Not funny'."),
        HumanMessage(content=state["joke"])
    ])
    return {"feedback": feedback.grade}

def route_joke(state: State):
    if state["feedback"] == "Funny":
        return "Accepted"
    return "Rejected + Regenerate"

builder.add_conditional_edges("llm_call_evaluator", route_joke, {
    "Accepted": END,
    "Rejected + Regenerate": "llm_call_generator"
})
```

---

### 6. Agent (ReAct Loop)

LLM decides which tool to call, runs it, observes output, and continues.

```python
from langchain.chat_models import init_chat_model
from langgraph.graph import StateGraph, MessagesState, START
from langgraph.prebuilt import ToolNode

def multiply(a: int, b: int) -> int:
    """Multiply a and b."""
    return a * b

tools = [multiply]
llm = init_chat_model("anthropic:claude-3-5-haiku")
llm_with_tools = llm.bind_tools(tools)

def call_llm(state: MessagesState):
    return {"messages": [llm_with_tools.invoke(state["messages"])]}

def route_after_llm(state: MessagesState) -> Literal["environment", END]:
    if state["messages"][-1].tool_calls:
        return "environment"
    return END

builder = StateGraph(MessagesState)
builder.add_node("llm", call_llm)
builder.add_node("environment", ToolNode(tools))
builder.add_edge(START, "llm")
builder.add_conditional_edges("llm", route_after_llm)
builder.add_edge("environment", "llm")
agent = builder.compile()
```

---

## Functional API Alternative

For simpler, function-based workflows without explicit graph construction:

```python
from langgraph.func import entrypoint, task

@task
def generate_joke(topic: str):
    return llm.invoke(f"Write a joke about {topic}").content

@entrypoint()
def prompt_chaining_workflow(topic: str):
    joke = generate_joke(topic).result()
    return joke
```

---

## Relevance to OneOps AI Service

| LangGraph Pattern | OneOps Use Case |
|---|---|
| **Prompt Chaining** | Multi-step summarization pipeline: `get_ticket_details` → `get_ticket_timeline` → `summarize_entity` → `put_cached_summary` |
| **Parallelization** | Run `summarize_asset` + `summarize_cmdb_ci` in parallel for a single ticket |
| **Routing** | Route user intent to the correct specialist agent (summarization / triage / KB) |
| **Orchestrator-Worker** | Planner generates `ExecutionPlan` steps, Executor fans them out to specialist agents |
| **Evaluator-Optimizer** | Retry triage assignment if routing confidence < 0.7 |
| **Agent (ReAct)** | KB agent decides which tool to call next based on search results |
