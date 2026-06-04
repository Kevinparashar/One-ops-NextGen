"""UC-5 Triage — technician-facing AI triage proposal + approval.

End user files a raw ticket via the portal (Stage 1, not built by this UC).
Technician opens this UC on the existing ticket; AI proposes the 7 ITIL
fields (category, subcategory, service_name, impact, urgency, priority,
assignment_group) plus a possible-duplicate flag. Technician clicks
binary Yes/No. On Yes, apply.py UPDATEs the row in one transaction with
an audit log; on No, nothing changes.

Non-regression contract (locked 2026-05-29):
  * UC-5 is purely additive. It MUST NOT modify UC-1 (record lookup),
    UC-3 (KB lookup), the chat conversation pipeline, the session
    manager, or the router (Stage 0a..4 + embedding classifier).
  * UC-5 MUST NOT change behaviour for any existing chat-shaped query.
  * New API routes register additively (POST /api/uc05/propose,
    POST /api/uc05/decide); existing routes are not touched.
  * New frontend components live in a separate route; the chat
    component is not modified.
  * Section L of the build checklist re-runs UC-1 + UC-3 + chat smoke
    suites before any UC-5 section is marked done. Any regression =
    roll the offending change back and find another path.

Module isolation rule (locked 2026-05-29):
  * Every file under uc05_triage/ is owned by UC-5.
  * UC-5 code MUST NOT import from oneops.use_cases.* siblings
    (uc01_summarization, uc03_kb_lookup, future uc02_similar_tickets).
  * UC-5 code MAY import from the platform substrate:
    oneops.llm.gateway, oneops.observability, oneops.policy,
    oneops.tenancy, oneops.registry, oneops.embeddings.triage_input
    (the embedding-input contract — must not drift between seed-time
    and runtime).
  * Enforced by tests/unit/architecture/test_uc_isolation.py.

Structure:
  contracts.py             Pydantic models (input/output shapes)
  state.py                 LangGraph state
  graph.py                 LangGraph wiring (3 tools + interrupt + apply)
  apply.py                 DB UPDATE on Yes + audit row
  retrieval/
    schema_loader.py       Reads retrieval_schema block from service-schema.json
    similarity_search.py   FTS + cosine + RRF + rerank + threshold engine
  tools/
    check_duplicates.py    Tool 1 — duplicate verdict + neighbour aggregations
    recommend_assignment.py Tool 2 — majority assignment_group from neighbours
    prioritize.py          Tool 3 — impact + urgency + priority via Motadata matrix
"""
