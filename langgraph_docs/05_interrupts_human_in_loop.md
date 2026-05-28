# LangGraph Interrupts & Human-in-the-Loop

> Source: https://docs.langchain.com/oss/python/langgraph/interrupts

Interrupts allow you to **pause graph execution at specific points** and wait for external input before continuing. When triggered, LangGraph saves state using its persistence layer and waits indefinitely until resumed.

Unlike static breakpoints (before/after nodes), interrupts are **dynamic** — placed anywhere in code and conditional on application logic.

---

## How It Works

1. Call `interrupt(value)` inside any node — value is JSON-serializable
2. Graph pauses and saves state with the checkpointer
3. Interrupt payload surfaces to the caller (`result["__interrupt__"]`)
4. Resume by calling `graph.invoke(Command(resume=<value>), config)`
5. The resume value becomes the **return value of `interrupt()`** inside the node

```python
from langgraph.types import interrupt

def approval_node(state: State):
    approved = interrupt("Do you approve this action?")
    return {"approved": approved}
```

---

## Requirements

1. A checkpointer (use durable store in production)
2. A `thread_id` in config (your persistent cursor for that conversation)
3. Call `interrupt()` where you want to pause

---

## Pause and Resume

### v2 Format (LangGraph >= 1.1)

```python
from langgraph.types import Command

config = {"configurable": {"thread_id": "thread-1"}}

# First run — hits interrupt and pauses
result = graph.invoke({"input": "data"}, config=config, version="v2")
print(result.interrupts)
# > (Interrupt(value='Do you approve this action?'),)

# Resume with human response
graph.invoke(Command(resume=True), config=config, version="v2")
```

### v1 Format (default)

```python
result = graph.invoke({"input": "data"}, config=config)
print(result["__interrupt__"])  # [Interrupt(value='Do you approve this action?')]

graph.invoke(Command(resume=True), config=config)
```

---

## Key Rules

- Use the **same `thread_id`** when resuming
- The resume value becomes the **return value of `interrupt()`**
- The node **restarts from the beginning** when resumed — code before `interrupt()` runs again
- `Command(resume=...)` is the **only** `Command` pattern as input to `invoke()`/`stream()`
- Do NOT use `Command(update=...)` as input for multi-turn conversations — use a plain dict

---

## Common Patterns

### Approve or Reject

```python
def approval_node(state: State) -> Command[Literal["proceed", "cancel"]]:
    is_approved = interrupt({
        "question": "Do you want to proceed?",
        "details": state["action_details"]
    })
    return Command(goto="proceed" if is_approved else "cancel")
```

### Review and Edit State

```python
def review_node(state: State):
    edited_content = interrupt({
        "instruction": "Review and edit this content",
        "content": state["generated_text"]
    })
    return {"generated_text": edited_content}

# Resume with the edited version
graph.invoke(Command(resume="The edited and improved text"), config=config)
```

### Interrupt Inside Tools

```python
from langchain.tools import tool

@tool
def send_email(to: str, subject: str, body: str):
    """Send an email to a recipient."""
    response = interrupt({
        "action": "send_email",
        "to": to,
        "subject": subject,
        "body": body,
        "message": "Approve sending this email?"
    })
    if response.get("action") == "approve":
        return _actually_send_email(to, subject, body)
    return "Email cancelled."
```

### Handling Multiple Interrupts (Parallel branches)

```python
# Step 1: Both parallel nodes hit interrupt() simultaneously
interrupted_result = graph.invoke({"vals": []}, config)
# __interrupt__ has two entries with IDs

# Step 2: Resume all at once by mapping ID → answer
resume_map = {
    i.id: f"answer for {i.value}"
    for i in interrupted_result["__interrupt__"]
}
result = graph.invoke(Command(resume=resume_map), config)
```

### Validating Human Input (loop until valid)

```python
def get_validated_input(state: State):
    while True:
        user_input = interrupt({"prompt": "Enter a number between 1 and 10:"})
        try:
            value = int(user_input)
            if 1 <= value <= 10:
                return {"validated_value": value}
            else:
                # Loop back and ask again
                continue
        except ValueError:
            continue
```

---

## Stream with Human-in-the-Loop

```python
async for chunk in graph.astream(
    initial_input,
    stream_mode=["messages", "updates"],
    subgraphs=True,
    config=config,
    version="v2",
):
    if chunk["type"] == "messages":
        msg, _ = chunk["data"]
        if msg.content:
            display_streaming_content(msg.content)
    elif chunk["type"] == "updates":
        if "__interrupt__" in chunk["data"]:
            interrupt_info = chunk["data"]["__interrupt__"][0].value
            user_response = get_user_input(interrupt_info)
            initial_input = Command(resume=user_response)
            break
```

---

## Relevance to OneOps AI Service

- **Ticket creation confirmation (UC-6)**: Use `interrupt()` to pause after slot-filling and ask user to confirm before creating a ticket — the most natural HITL pattern
- **Resolution suggestion approval**: Pause before closing a ticket, ask user to confirm the suggested resolution
- **Assignment approval**: Pause before auto-assigning a ticket if confidence is below threshold, ask L1 agent to confirm
- **`thread_id` = session_id**: OneOps must maintain a consistent thread_id per user conversation to enable interrupt/resume
- **Interrupt in tools**: OneOps tools (e.g., `resolve_entity`, `assign_entity`) can embed `interrupt()` for approval gates before writing to ITSM
