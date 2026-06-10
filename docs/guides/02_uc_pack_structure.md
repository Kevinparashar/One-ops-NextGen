# UC Folder Structure — Pack Discipline Without Common File Bloat

## The rule (carried forward from CLAUDE.md, sharpened)

**UC-specific code never goes in common files.** A new UC = files in its folder. Zero edits to graph builder, state schema, gateway, adapters, or any other cross-UC infrastructure.

## What changed vs POC3's 6-pack model

POC3 had: `intent_pack.py`, `planner_pack.py`, `bridge_pack.py`, `preprocessor_pack.py`, `capability_pack.py`, `tools.py`. Six files per UC.

Most of those existed to compensate for missing LangGraph + decomposer:
- `preprocessor_pack` — UC-specific preprocessing hooks → **absorbed by decomposer** (LLM reads operation cards)
- `intent_pack` (classifier prompt content) → **absorbed by decomposer** (cards include semantic descriptions)
- `planner_pack` (planner LLM rules) → **absorbed by decomposer + tool manifests** (decomposer produces the DAG; node decides tool sequence)
- `bridge_pack` (answer_directly / synthesize_plan hooks) → **absorbed into the UC node** (one function, clear flow)
- `capability_pack` (Agent instruction block) → **kept but simplified** (one prompt file per intent)
- `tools.py` → **kept** (same shape)

Net: 6 files → 4-5 files. And the files that remain are smaller because they don't have to fight the orchestration.

## New layout

```
src/oneops/use_cases/uc01_summarization/
├── __init__.py                       # registers node + tools + cards at import time
├── node.py                           # async def uc1_node(state) — the LangGraph node body
├── tools.py                          # @tool-decorated tool functions
├── catalog.yaml                      # field catalog (schema-described, NO aliases)
├── prompts/
│   ├── full_summary.md               # agent instruction for full-summary intent
│   └── field_read.md                 # agent instruction for field-read intent
├── operations/
│   ├── entity_summary.yaml           # operation card for the decomposer
│   ├── field_read.yaml
│   └── linked_entity.yaml
├── repository.py                     # UC-specific DB queries (extends BaseRepository)
└── tests/
    ├── test_summary.py
    ├── test_field_read.py
    └── golden/                       # golden response files for replay testing
```

## File-by-file

### `node.py` — the LangGraph node

One async function. Receives `OneOpsState`. Returns a state update (a dict of new `node_results`).

```python
# src/oneops/use_cases/uc01_summarization/node.py
from oneops.state.schema import OneOpsState, NodeResult
from oneops.observability.tracing import tracer
from oneops.use_cases.uc01_summarization.tools import (
    summarize_entity, get_ticket_details, get_ticket_links, get_field_value,
)

async def uc1_node(state: OneOpsState) -> dict:
    """Execute every DAG node assigned to uc01_summarization.
    
    Handles intents: summary, field_read, linked_entity_summary.
    """
    my_nodes = [n for n in state["dag"] if n["uc_id"] == "uc01_summarization"]
    if not my_nodes:
        return {}
    
    results: list[NodeResult] = []
    for dag_node in my_nodes:
        # Resolve dependencies (LangGraph schedules nodes in dep order; this is
        # just for params that reference prior node outputs)
        params = resolve_node_params(dag_node, state["node_results"])
        
        with tracer.start_as_current_span(
            f"uc01.{dag_node['intent']}",
            attributes={
                "node_id": dag_node["node_id"],
                "entity_id": params.get("entity_id", ""),
            },
        ):
            try:
                handler = INTENT_HANDLERS[dag_node["intent"]]
                result = await handler(params=params, state=state)
                results.append(result)
            except Exception as e:
                results.append(make_failed_result(dag_node, e))
    
    return {"node_results": results}


INTENT_HANDLERS = {
    "summary": _handle_summary,
    "field_read": _handle_field_read,
    "linked_entity_summary": _handle_linked_summary,
}
```

The node:
- Is **registered** in `__init__.py` via a manifest (see below)
- Does **not** call into the common graph builder
- Does **not** know about NATS, OTEL config, sessions — those are platform concerns it consumes via injection

### `tools.py` — tool functions

```python
# src/oneops/use_cases/uc01_summarization/tools.py
from oneops.tools.registry import tool
from oneops.use_cases.uc01_summarization.repository import ITSMRepo

@tool(
    uc_id="uc01_summarization",
    audience={"service_desk_agent", "manager", "employee"},
    side_effects="read",
)
async def get_ticket_details(ticket_id: str, service_id: str, tenant_id: str) -> dict:
    """Fetch full ticket record for the given ID and service."""
    return await ITSMRepo.get_details(ticket_id, service_id, tenant_id)

@tool(uc_id="uc01_summarization", audience={"service_desk_agent", "manager"}, side_effects="read")
async def summarize_entity(entity_id: str, service_id: str, tenant_id: str, role: str) -> str:
    """Generate a structured summary via LLM. Returns formatted markdown."""
    # Implementation calls LLM Gateway with the prompt
    ...
```

The `@tool` decorator:
- Registers the tool in a process-wide registry
- Records audience + side_effects metadata (consumed by RBAC adapter)
- Wraps with OTEL span automatically
- Exposes a LangChain-compatible `Tool` object for `create_react_agent`

### `catalog.yaml` — field catalog (schema-described, NO aliases)

```yaml
# src/oneops/use_cases/uc01_summarization/catalog.yaml
fields:
  priority:
    description: |
      How critical the ticket is. ITIL P1-P4 scale where P1 is highest.
      Users commonly call this priority, urgency, criticality, importance, P-level.
      Distinct from severity (technical magnitude) and impact (business scope) —
      those are separate fields with their own semantics.
    db_column: priority
    source_tool: get_ticket_details
    type: scalar
  
  severity:
    description: |
      Technical magnitude of the issue. P1-P4 scale similar to priority but
      distinct: severity is "how bad is it technically?" while priority is
      "how soon must we fix it?". For most tickets they're correlated but
      can diverge (e.g. low-severity bug in a P1 customer's environment).
    db_column: severity
    source_tool: get_ticket_details
    type: scalar
  
  related_incidents:
    description: |
      Incident IDs linked to this problem or change record. Returns a list
      of canonical INC IDs. Users ask for this as "related incidents",
      "linked incidents", "incidents caused by this problem", or in plural/singular.
    db_column: related_incidents
    source_tool: get_ticket_links
    type: list
  
  workaround:
    description: |
      Temporary fix or mitigation steps. Free-text. Preserve verbatim — do
      NOT paraphrase technical terms (database names, file paths, error codes,
      product names). Users ask for this as "workaround", "temporary fix",
      "how do we work around this".
    db_column: workaround
    source_tool: get_ticket_details
    type: text
```

**No `aliases` lists.** The `description` is the contract. When a user asks for "urgency" or "criticality", a semantic field resolver (LLM call against the catalog) maps it to `priority`. No alias drift, no plural bugs, no synonym gaps.

### `prompts/` — agent instructions

One markdown file per intent. Loaded at startup. Versioned via filename + git history.

```markdown
<!-- src/oneops/use_cases/uc01_summarization/prompts/full_summary.md -->
You are the summarization agent for an ITSM service. The user has asked for
a full summary of an entity (ticket / asset / CMDB CI).

Your job:
1. Call get_ticket_details, get_ticket_timeline, get_ticket_links,
   get_ticket_attachment_metadata to gather data.
2. Call summarize_entity(entity_id) — its output is the FINAL response.
3. Return summarize_entity's output VERBATIM. Do not add preamble, do not
   add closing language, do not reformat.

[... rest of the instructions ...]
```

POC3's `_FULL_SUMMARY_INSTRUCTIONS` and `_FIELD_READ_INSTRUCTIONS` blocks port directly into these markdown files. No code wrapping.

### `operations/*.yaml` — operation cards (the decomposer's source of truth)

One file per operation. The decomposer reads these to know what each UC can do.

```yaml
# src/oneops/use_cases/uc01_summarization/operations/entity_summary.yaml
operation_id: uc01_summarization.entity_summary
uc_id: uc01_summarization
intent: summary
execution_type: read
semantic_description: |
  Summarize an ITSM entity — full structured report covering status, what
  happened, key updates, pending actions, linked records. Requires a
  grounded entity reference (incident, request, problem, change, asset, CI).
  
  When to use: user asks for a summary, overview, details, or explanation
  of a specific named entity.
  When NOT to use: user asks how to fix a symptom (use uc03_kb_lookup),
  asks for similar tickets (use uc02_similar_tickets), asks to create a
  new ticket (use uc06_conversational_create).

handles_examples:
  - "summarize INC0001001"
  - "give me a summary of the VPN incident"
  - "tell me about CHG0004007"
  - "what happened with this incident?"  # after focus is set

does_not_handle_examples:
  - "find KB articles for VPN issues"
  - "what tickets are similar to this?"

required_params:
  - name: entity_id
    type: string
    description: Canonical entity ID (INC0001001, PBM0003003, CHG0004007, AST0001001, CI0000001)
  - name: service_id
    type: string
    description: Service domain (incident, problem, change, request, asset, cmdb_ci)

optional_params:
  - name: summary_length
    type: enum
    values: [short, medium, long]
    default: medium

audience:
  - service_desk_agent
  - manager
  - employee  # employees see field-filtered summaries

dependencies: []   # this operation has no prerequisites

excluded_intents: []   # nothing excluded from this operation
```

These cards are **data, not code**. Decomposer loads them, formats into its prompt, makes routing decisions semantically.

### `__init__.py` — the manifest

```python
# src/oneops/use_cases/uc01_summarization/__init__.py
from oneops.use_cases.registry import register_uc
from oneops.use_cases.uc01_summarization.node import uc1_node
from oneops.use_cases.uc01_summarization import tools  # triggers @tool registration
from pathlib import Path

_HERE = Path(__file__).parent

register_uc(
    uc_id="uc01_summarization",
    node=uc1_node,
    operations_dir=_HERE / "operations",
    catalog_path=_HERE / "catalog.yaml",
    prompts_dir=_HERE / "prompts",
)
```

This is what makes the UC self-registering. Auto-discovered at startup via `importlib.import_module("oneops.use_cases." + folder)` for every folder under `use_cases/`.

### `repository.py` — UC-specific DB

```python
# src/oneops/use_cases/uc01_summarization/repository.py
from oneops.adapters.postgres import BaseRepository

class ITSMRepo(BaseRepository):
    @classmethod
    async def get_details(cls, ticket_id: str, service_id: str, tenant_id: str) -> dict:
        return await cls._fetchone(
            f"SELECT * FROM {service_id}s WHERE id = $1 AND tenant_id = $2",
            ticket_id, tenant_id,
        )
```

Each UC owns its repository. UC-1 manages ticket queries. UC-3 manages KB queries. No cross-UC repository imports.

## What's NOT in a UC folder

- ❌ Any graph-building code (lives in `src/oneops/graph/`)
- ❌ State schema (lives in `src/oneops/state/`)
- ❌ Adapters (LLM gateway, NATS, Dragonfly, Postgres pool — all in `src/oneops/adapters/` or `src/oneops/gateway/`)
- ❌ Decomposer (one decomposer for all UCs, in `src/oneops/nodes/decomposer.py`)
- ❌ Aggregator (one aggregator, in `src/oneops/nodes/aggregator.py`)
- ❌ Phrase-list regexes (don't write them at all)
- ❌ Alias maps (don't write them at all)
- ❌ "If service_id == 'incident'" branching (it's a UC, of course it handles incidents)

## How the graph picks up a UC

At process startup:

```python
# src/oneops/graph/builder.py
from oneops.use_cases.registry import all_ucs

def build_graph():
    g = StateGraph(OneOpsState)
    g.add_node("load_session", load_session_node)
    g.add_node("decomposer", decomposer_node)
    
    # Register each UC's node generically
    for uc_id, uc_manifest in all_ucs().items():
        g.add_node(uc_id, uc_manifest.node)
    
    g.add_node("aggregator", aggregator_node)
    
    g.set_entry_point("load_session")
    g.add_edge("load_session", "decomposer")
    
    # Conditional edges read state.dag and route to the UCs needed
    g.add_conditional_edges(
        "decomposer",
        lambda s: list({n["uc_id"] for n in s["dag"]}) or ["aggregator"],
        {uc_id: uc_id for uc_id in all_ucs()} | {"aggregator": "aggregator"},
    )
    
    # All UCs converge to aggregator
    for uc_id in all_ucs():
        g.add_edge(uc_id, "aggregator")
    
    g.add_edge("aggregator", END)
    return g.compile()
```

**Graph builder NEVER references a specific UC name.** Adding UC-2 is literally: drop the folder, restart. The builder picks it up.

## Bloat prevention rules

Red flags that mean you're about to overload a common file:

```python
# BAD — UC name in graph builder
if uc_id == "uc01_summarization":
    g.add_edge("uc01_summarization", "special_intermediate")
# RIGHT — manifest declares the edge if special
# uc_manifest.extra_edges = [("special_intermediate", "aggregator")]

# BAD — UC-specific routing in decomposer
prompt = "If user asks about KB, route to UC-3..."
# RIGHT — operation cards say what each UC handles; decomposer reads cards

# BAD — UC-specific tool in shared adapter
class LLMGateway:
    def summarize(self, ...): ...   # UC-1-specific!
# RIGHT — LLMGateway.call() is generic; UC-1 node calls it with UC-1 prompts

# BAD — hardcoded field in adapter
class SessionStore:
    def set_priority(self, ...): ...  # field name!
# RIGHT — SessionStore.update_canonical_state(updates: dict)

# BAD — alias list in catalog
priority:
  aliases: ["urgency", "criticality", "P-level"]
# RIGHT
priority:
  description: "How critical... users also say urgency, criticality, P-level..."
```

## Adding a new UC — the checklist

1. Create folder: `src/oneops/use_cases/ucXX_name/`
2. Write `node.py` with the node function
3. Write `tools.py` with @tool-decorated functions
4. Write `catalog.yaml` with field descriptions (no aliases)
5. Write `prompts/*.md` for each intent
6. Write `operations/*.yaml` cards for each operation
7. Write `repository.py` if the UC has DB queries
8. Write `__init__.py` to register the UC
9. Run integration tests
10. **Touch ZERO files outside this folder.** If you need to, your design is wrong.

If step 10 is impossible, raise it as an architectural issue — don't reach for the common file.

## Scaling to 100 UCs

The pattern scales linearly:
- Decomposer prompt scales with operation cards (data, not code) — but use card-pagination + role-filtering so only ~10-15 relevant cards reach any single prompt
- Graph builder loop iterates UCs — O(N) at startup, free at request time
- LangGraph routing edges scale O(N) at startup, O(1) at request time
- No code change in common files regardless of N

This is exactly the discipline CLAUDE.md enforces. The architecture honors it.
