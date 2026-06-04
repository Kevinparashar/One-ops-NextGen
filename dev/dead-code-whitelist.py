"""Vulture dead-code whitelist for the OneOps codebase.

PURPOSE
-------
Vulture is a static analyzer; it cannot see across runtime indirection.
This file declares the symbols vulture would otherwise flag as unused but
are in fact reached through one of the codebase's runtime-indirection
patterns. Pass this file to vulture as a positional argument:

    vulture src/oneops/ dev/dead-code-whitelist.py --min-confidence 80

USAGE NOTES
-----------
- Audit-only artefact. NOT imported by any runtime code. NOT loaded by
  pytest. Safe to delete without affecting the service.
- One module-level reference per symbol is enough — vulture treats every
  name appearing here as "used".
- DO NOT add inline `# noqa` or vulture pragmas in source. The whitelist
  is the single auditable place.
- When a runtime-indirection pattern is added (new registry-dispatched
  module, new LangGraph node, new FastAPI decorator), add the symbols
  here, not in source.

CATEGORIES (alphabetical)
-------------------------
1. FastAPI route handlers + Depends callables — discovered by FastAPI at
   decorator-evaluation time; vulture sees them as unused functions.
2. LangGraph node functions referenced by string in executor/graph.py.
3. LlmGateway / policy-composer profile members — referenced via enum
   value lookup.
4. Pydantic BaseModel fields — referenced through Pydantic's MRO; vulture
   marks each Field assignment as an unused attribute.
5. Registry-dispatched tool handlers — referenced via `module_path` +
   `function_name` strings in tool-registry.json.
6. Span helpers — referenced by string names in tracer.start_as_current_
   span(...) calls.
7. Dataclass / TypedDict frozen fields used only via attribute access on
   instances built by the registry.

Anything else added to this file must include the reason in a one-line
comment.
"""
from __future__ import annotations

# ── 1. FastAPI route handlers + Depends callables ──────────────────────
# These names are reached only when FastAPI builds its app and matches
# decorators. Vulture flags each handler as an unused function.
_ = """
_index            # GET /
_health           # GET /api/health
_config_chip      # GET /api/config
_identity_options # GET /api/identity-options
_session_history  # GET /api/session/{session_id}/history
_fast_path_spec   # GET /api/fast/{uc_id}/spec
_create_session   # POST /api/sessions
_list_sessions    # GET /api/sessions
_get_session      # GET /api/sessions/{session_id}
_delete_session   # DELETE /api/sessions/{session_id}
chat              # POST /api/chat
_fast_path        # POST /api/fast/{uc_id}
similar_tickets   # POST /api/uc02/similar-tickets
queue_summary     # GET /api/uc05/queue-summary
queue             # GET /api/uc05/queue
propose           # POST /api/uc05/propose
decide            # POST /api/uc05/decide
get_ticket_store  # Depends() callable
"""

# ── 2. LangGraph executor node methods ────────────────────────────────
# Registered by string name in executor/graph.py via add_node("name",
# nodes.method); vulture sees the method as unused.
_ = """
load_session
update_focus
control_gate
route
wave
run_step
aggregate
boundary
persist
"""

# ── 3. Policy profiles + composer Profile enum members ────────────────
# Referenced via Profile.<NAME>.value lookup, not direct attribute use.
_ = """
INTERNAL_AGENT
PLATFORM_SYSTEM
PLANNER
FEATURE_AGENT
FEATURE_AGENT_WITH_TOOLS
FEATURE_AGENT_JSON
TEAM_COORDINATOR
SUB_AGENT_MINIMAL
"""

# ── 4. Pydantic BaseModel fields ──────────────────────────────────────
# Each Field(...) assignment looks like an unused attribute to vulture
# but defines the API/contract surface. We whitelist by attribute name —
# vulture treats one mention here as "this attribute is used".
_ = """
tenant_id
user_id
role
session_id
request_id
ticket_id
service_id
agent_id
tool_id
entity_id
focus_entity_id
focus_service_id
relative_days
start_date
end_date
boundary
label
time_filter
time_window_hours
same_category_only
same_service_only
prefer_status
min_similarity_score
diagnosis_confirm
max_results
duplicate_threshold
max_candidates
match_pct
similarity_score
confidence
why_similar
flag
source_ticket
source_ticket_id
results
total_candidates_considered
message
warning
cached
opened_at
resolved_at
fulfilled_at
created_at
updated_at
priority
status
category
subcategory
service_name
ci_id
assigned_to
assignment_group
proposal_id
choice
actor_user_id
duplicate_verdict
top_match
basis
basis_ids
rationale
key_details
summary
classification
candidates_considered
vec_score
fts_score
fused_score
fields
acceptance
suggested
matched_prefix
candidates
mark_action
record_action
hint
limit
turn
content
"""

# ── 5. Registry-dispatched tool handler entry points ──────────────────
# These are imported by string at tool-runner time, not at module import.
# `module_path:function_name` resolution in HandlerResolver bypasses vulture.
_ = """
get_ticket_details
get_ticket_timeline
get_cached_summary
put_cached_summary
get_ticket_links
get_ticket_attachment_metadata
summarize_entity
find_similar_entities
check_duplicate_candidates
recommend_assignment
prioritize_entity
"""

# ── 6. Span-helper names referenced by string in tracer calls ─────────
# Used only inside start_as_current_span("name", ...) literals.
_ = """
ai_request
graph_planner
state_load
state_update
uc02_core_find_similar
uc05_runner_invoke
uc05_dispatch_propose
uc05_dispatch_decide
uc05_agent_on_propose
uc05_agent_on_decide
uc05_graph_check_duplicates
uc05_graph_recommend_assignment
uc05_graph_prioritize
uc05_graph_assemble
uc05_tool_check_duplicates
uc05_tool_recommend_assignment
uc05_assembly
uc05_store_get_ticket
uc05_store_apply
"""

# ── 7. Dataclass + TypedDict fields used via attribute access only ────
# State channels in LangGraph ExecutorState are read by name only — vulture
# can't see TypedDict access patterns.
_ = """
entity_clarification
control_gate_outcome
route_outcome
boundary_reason
plan
unrouted
route_diagnostics
step_results
final_status
final_response
entry_mode
"""

# ── 8. Settings / env-driven config attributes ────────────────────────
# Read via attribute access on a Settings instance; vulture sees the
# class definition but not the getattr-style usage in app.py / handlers.
_ = """
dragonfly_url
nats_url
postgres_url
langgraph_postgres_url
langgraph_postgres_pool_min
langgraph_postgres_pool_max
langgraph_checkpointer
langgraph_aes_key
postgres_pool_min
postgres_pool_max
otel_exporter_otlp_endpoint
llm_gateway_url
llm_default_model
"""

# ── 9. Protobuf-generated symbols ─────────────────────────────────────
# Auto-generated; vulture flags many fields. Generated paths under
# src/oneops/codec/generated/ are out of scope for human review.
_ = """
DESCRIPTOR
serialized_options
syntax
serialized_pb
"""
