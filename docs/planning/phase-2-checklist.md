---
title: Phase 2 — UC-5 Triage handler — Production-Grade Checklist
date: 2026-05-29
status: Active — drive top-to-bottom; tick only when ALL gate conditions pass
sister_doc: docs/planning/day1-execution-plan.md (Phase 2 section)
---

# Phase 2 — UC-5 Triage handler — Production-Grade Checklist

## Tick criteria (NON-NEGOTIABLE)

A checkbox is ticked **only when ALL of the following hold:**

1. **Implemented** — code exists, logic correct, no `TODO`/`FIXME` markers
2. **Integrated** — imports resolve, wiring confirmed via runtime check, not just static analysis
3. **Unit-tested** — pytest green with assertions on real behaviour (not `assert True` stubs)
4. **End-to-end tested** — runs through the full pipeline (LLM gateway, policy composer, span emission, DB read/write) against real seed data
5. **No regression** — UC-1 + UC-3 unit tests still green; smoke 81/84 still green; devil's-play 11/11 still green
6. **Evidence captured** — log file or trace ID artefact under `ops/pmg-evidence/` proving the working state

If any gate fails, the item is NOT ticked. Fix the underlying step before claiming closure (rule §2.7 no silent failures).

---

## Section 0 — Pre-flight data prep (~75 min)

These run BEFORE any UC-5 code is written. UC-5 cannot be built without them.

### B1 — Motadata priority matrix added to incident service-schema

- [ ] `registries/service-schema.json` has a `priority_matrix` block under `incident` with the exact Motadata axes (Impact: Low / On Users / On Department / On Business; Urgency: Low / Medium / High / Urgent) and the 16-cell mapping from the screenshot
- [ ] JSON parses cleanly: `python3 -c "import json; json.load(open('registries/service-schema.json'))"` exits 0
- [ ] Matrix cells match the screenshot verbatim (no derived guesses)
- [ ] Rule cited in the `_doc` field: §C10 deterministic + §2.13 agents-as-data

### B2 — Request priority pattern (a) added to request service-schema

- [ ] `registries/service-schema.json` has `priority_matrix_ref: "incident.priority_matrix"` under `request`
- [ ] `derive_impact_for_request` block maps catalog categories → impact (onboarding → On Department, access/hardware/software → On Users, knowledge → Low)
- [ ] `derive_urgency_for_request` block maps SLA-state → urgency (healthy / approaching_50pct / approaching_25pct / breached)
- [ ] Rule cited: pattern (a) confirmed with operator on 2026-05-29

### B3 — Embedding migration file written (NOT yet executed)

- [ ] `migrations/0003_incident_request_embedding.sql` exists
- [ ] Adds `search_tsv tsvector GENERATED ALWAYS AS (...) STORED` to `itsm.incident` and `itsm.request`
- [ ] Adds `embedding vector(1536)`, `embedding_model text`, `embedding_version text`, `embedded_at timestamptz` to both
- [ ] Creates `gin` index on `search_tsv`
- [ ] Creates `hnsw vector_cosine_ops` index on `embedding`
- [ ] Idempotent — `IF NOT EXISTS` on every ALTER and INDEX

### B3-RUN — Operator executes the migration **[OPERATOR ACTION]**

- [ ] You run: `psql "$DATABASE_URL" -f migrations/0003_incident_request_embedding.sql`
- [ ] Verification command exits clean: `psql -c "SELECT column_name FROM information_schema.columns WHERE table_schema='itsm' AND table_name='incident' AND column_name='embedding'"`
- [ ] HNSW index exists: `psql -c "SELECT indexname FROM pg_indexes WHERE tablename='incident' AND indexname LIKE '%emb%'"`
- [ ] Same checks pass for `itsm.request`
- [ ] Evidence: `ops/pmg-evidence/phase-2-migration-applied.log`

### B4 — Embedding backfill script written (NOT yet executed)

- [ ] `tools/seed_incident_embeddings.py` exists
- [ ] Mirrors the existing `tools/seed_kb_embeddings.py` pattern (single egress through `gateway.embed`, per-tenant cost recorded, idempotent re-runs skip already-embedded rows)
- [ ] Embeds `title + " " + description` for each `incident` and `request`
- [ ] Captures count of rows embedded + cost summary

### B4-RUN — Operator backfills embeddings **[OPERATOR ACTION]**

- [ ] You run: `.venv/bin/python tools/seed_incident_embeddings.py`
- [ ] All 160 incidents + 130 requests embedded (290 total)
- [ ] OpenAI cost ~$0.01 confirmed via the script's summary
- [ ] Verification: `psql -c "SELECT COUNT(*) FROM itsm.incident WHERE embedding IS NOT NULL"` returns 160; same for request returns 130
- [ ] Evidence: `ops/pmg-evidence/phase-2-embedding-backfill.log`

### B5 — In-memory test fixtures

- [ ] `tests/fixtures/sample_tickets.py` exports `make_inmemory_ticket_store()` returning a seeded `InMemoryTicketStore`
- [ ] Seeded with 6 incidents (covering 6 categories) + 4 requests (covering 4 catalog categories)
- [ ] Used by every UC-5 unit test

### B6 — Demo prep script

- [ ] `scripts/prepare_uc5_demo.py` exists
- [ ] Backs up the original values of (category, subcategory, impact, urgency, priority, assignment_group, status) for 5 hand-picked T001 incidents to `ops/pmg-evidence/demo-rollback.json`
- [ ] Wipes those 8 fields on the 5 incidents
- [ ] Has a `--rollback` flag that restores from the backup file
- [ ] Verification: dry-run output shows exactly which 5 incidents will be wiped

### B7 — TicketStore seed→Motadata translator **[+ added]**

- [ ] `src/oneops/use_cases/_shared/ticket_store.py` gains `_IMPACT_SEED_TO_MOTADATA`, `_URGENCY_SEED_TO_MOTADATA`, `_PRIORITY_SEED_TO_MOTADATA` lookup dicts
- [ ] `TicketStore.get()` and `.search()` methods translate ITIL values to Motadata values on read
- [ ] Reverse translation on write (when UC-5 writes Motadata values to the DB)
- [ ] Unit tests cover both directions, including unmapped values raising `TypedTranslationError`
- [ ] No silent fallback to `None` for unmapped values (rule §2.7)
- [ ] Evidence: `ops/pmg-evidence/phase-2-translator-tests.log`

### B8 — service-schema JSON validation **[+ added]**

- [ ] One-shot validator script `scripts/validate_service_schema.py` checks:
  - matrix axes match the enum lists declared in `_axes`
  - every cell value is in `_priority_values`
  - request `priority_matrix_ref` resolves to an existing matrix
- [ ] Run on the modified file; passes
- [ ] Evidence: validator output captured

---

## Section 1 — UC-5 module skeleton + contracts (~35 min)

### C1 — Folder skeleton

- [ ] `src/oneops/use_cases/uc05_triage/` exists with `__init__.py`, `contracts.py`, `handlers.py`, `triage_tools.py`
- [ ] `__init__.py` re-exports the public surface (`handler`, `TriageDecision`, `TriageRefusal`)
- [ ] Module imports cleanly: `python3 -c "from oneops.use_cases.uc05_triage import handler, TriageDecision"` exits 0

### C2 — Pydantic contracts

- [ ] `contracts.py` defines: `TriageRequest`, `ClassifyResult`, `PriorityResult`, `DupCheckResult`, `AssignmentRecommendation`, `TriageDecision`, `TriageRefusal`, `ApprovalRequest`, `StructuredAction` (9 models total)
- [ ] All use Motadata vocabulary (`Literal["Low", "On Users", "On Department", "On Business"]`, etc.)
- [ ] Frozen by default (`model_config = ConfigDict(frozen=True)` per COMPONENT_SPEC C7)
- [ ] `test_contracts.py`: 9 round-trip tests green
- [ ] Rule §C7 cited in module docstring

---

## Section 2 — The 4 tools (~85 min)

### C3a — `classify_entity` (LLM via gateway + policy composer)

- [ ] Tool composes prompt via `policy.composer.compose(Profile.PLATFORM_SYSTEM_POLICY, ...)` — NEVER a raw f-string system prompt
- [ ] Calls `llm.gateway.LlmGateway.call(response_model=ClassifyResult)` — single egress
- [ ] Emits `uc05.tool.classify_entity` span with `tenant_id`, `request_id`, `agent_id`, `agent_version`, `confidence_score`, `llm.cost_usd`, `llm.tokens.total`
- [ ] Returns `ClassifyResult` or raises typed `ClassificationError` — never bare `Exception`
- [ ] `test_classify_entity.py`: happy path + LLM-5xx error + invalid-response-shape error = 3 tests green
- [ ] Mocked LLM gateway used in unit tests; integration test exercises the real gateway

### C3b — `prioritize_entity` (deterministic matrix lookup)

- [ ] Reads `priority_matrix` from `registries/service-schema.json` (loaded once at boot, not per-call)
- [ ] No LLM call inside this tool (rule §C10 deterministic by default)
- [ ] For incidents: direct `matrix[impact][urgency]` lookup
- [ ] For requests: derives impact from catalog, urgency from SLA, then matrix lookup
- [ ] Emits `uc05.tool.prioritize_entity` span with derivation inputs as attributes
- [ ] Raises typed `PriorityDerivationError` for unmapped values — no silent default
- [ ] `test_prioritize_entity.py`: all 16 incident matrix cells + 6 catalog × 4 SLA-state request cells = 40 tests green
- [ ] Evidence: span audit shows matrix cells are logged

### C3c — `check_duplicate_candidates` (hybrid retrieval, Tier 3)

- [ ] Embeds the query via `gateway.embed` — reuses the Dragonfly cache (per `(tenant, model, normalised_query)`)
- [ ] Runs **two parallel queries** via `asyncio.gather`:
  - vector cosine search on `itsm.<service>.embedding` filtered by tenant + status
  - FTS via `ts_rank_cd` on `search_tsv` filtered by tenant + status
- [ ] **Fuses via RRF** with k=60 (same algorithm UC-3 uses) — reuse UC-3's `rrf_fuse` helper
- [ ] Applies relevance gate at cosine ≥ 0.50 (matches UC-3 calibration)
- [ ] Returns top 5 candidates as `DupCheckResult` with each candidate's similarity
- [ ] Emits `uc05.tool.check_duplicate_candidates` span with `candidates_count`, `vector_hits`, `fts_hits`, `fused_count`, `gated_count`
- [ ] `test_check_duplicate_candidates.py`: vector-only / FTS-only / hybrid / zero-results / sub-threshold = 5 tests green
- [ ] Verified shares UC-3 helpers: grep proves no duplicate RRF or relevance-gate logic
- [ ] Rule §2.5 cited (single egress) + reuses UC-3's tested infrastructure

### C3d — `assign_entity` (mutation with approval gate)

- [ ] Advisory mode: returns `AssignmentRecommendation`, no DB write
- [ ] Apply mode: emits `interrupt({"reason": "high_risk_apply_assignment", "summary": ...})`, awaits `Command(resume={"approved": true|false})`
- [ ] On approve: writes only the 8 allowed columns to `itsm.incident` or `itsm.request` (column whitelist enforced in SQL); appends a structured `work_note` with agent_id + agent_version + confidence + timestamp
- [ ] On reject: NO DB write; appends a `work_note` recording the rejection
- [ ] On modify: writes the user's modified value, NOT the AI's recommendation; appends both values to the work_note
- [ ] Emits `uc05.tool.assign_entity` span with `mode`, `approval_outcome`, `mutation_applied`
- [ ] `test_assign_entity.py`: advisory + apply-approved + apply-rejected + apply-modified + tenant-mismatch = 5 tests green
- [ ] Rule §2.7 + §C18 cited

---

## Section 3 — Handler orchestration (~50 min)

### C4 — `handlers.py` main orchestrator

- [ ] Validates `TriageRequest` (tenant_id required, service_id ∈ {incident, request})
- [ ] Branches by service_id; calls 4 tools in order (classify → prioritize → check_dup → assign)
- [ ] Each tool call wrapped in its own span; handler emits parent span `uc05.triage.handle`
- [ ] Returns `TriageDecision` (success) or `TriageRefusal` (any failure) — never bare `None`
- [ ] `test_handler.py`: happy (advisory) / happy (apply, approved) / happy (apply, rejected) / tenant-missing / incident-missing / classify-failure / dup-check-failure = 7 tests green
- [ ] Rules §2.3 + §2.4 + §2.6 + §2.7 + §C17 cited

---

## Section 4 — Integration wiring (~20 min)

### C5 — HandlerResolver finds UC-5

- [ ] `python3 -c "from oneops.executor.handlers import HandlerResolver; assert HandlerResolver().resolve('triage_agent') is not None"` exits 0
- [ ] Resolver returns the UC-5 handler, not a stub
- [ ] Routing-from-message integration test passes: a `TurnRequest` with message "triage INC0001001" reaches the UC-5 handler

### C5-ToolReg — Tools registered via `@register_tool`

- [ ] All 4 tools registered in `ToolRegistry`
- [ ] `(triage_agent, incident)` and `(triage_agent, request)` tool allowlists in `agent-tool-mapping.json` are honoured (executor refuses non-allowlisted tool calls)
- [ ] Verification: `python3 -c "from oneops.registry import ToolRegistry; tr=ToolRegistry(); assert all(tr.get(t) for t in ['classify_entity','prioritize_entity','check_duplicate_candidates','assign_entity'])"` exits 0

---

## Section 5 — Frontend additions (~105 min)

### C6a — Quick Action panel HTML

- [ ] `src/oneops/api/static/index.html` adds a right-side panel with a UC-5 form (ticket_id input, service dropdown, mode radio buttons, Run button) and a UC-8 form
- [ ] Existing chat layout preserved; new layout is responsive (no overflow at < 1200px)

### C6b — Approval bubble buttons (JS)

- [ ] `app.js` renders 3 buttons (Approve / Reject / Modify) inside an AI bubble when the response carries an `approval_request` field
- [ ] Modify opens an inline editor for the recommended `assignment_group`
- [ ] Button click sends `{approval_response: {request_id, action, override_value?}}` over the WebSocket

### C6c — Structured Action panel submit (JS)

- [ ] Form submit on the Quick Action panel sends `{structured_action: {uc_id: "uc05_triage", params: {...}}}` over the WebSocket
- [ ] Validates required fields before send; shows inline error on missing field

### C6d — WebSocket frame dispatch (Python)

- [ ] `src/oneops/api/app.py` `/ws/chat` handler dispatches 2 new frame types:
  - `approval_response` → resumes the paused executor with `Command(resume={...})` on the same `thread_id`
  - `structured_action` → invokes UC-5 (or UC-8) handler directly, returns the response on the same socket
- [ ] Existing `message`-type frame dispatch unchanged
- [ ] Rule §2.7: invalid frame shape returns typed error frame, never silent

### C6-E2E — Browser-to-DB round-trip

- [ ] Open `http://localhost:8000` in a browser (or use a headless WebSocket client)
- [ ] Identity = T001 / USR00003 / service_desk_agent
- [ ] Trigger triage via either chat OR quick-action; receive triage decision
- [ ] Click Apply (or send approval frame); UC-5 commits the mutation
- [ ] `psql -c "SELECT assignment_group, status, updated_at FROM itsm.incident WHERE incident_id='INC0001001'"` shows the new values
- [ ] Evidence: `ops/pmg-evidence/phase-2-frontend-e2e.log`

---

## Section 6 — Routing decision (no new code, just confirmation) (~10 min)

### C7-Confirm — Existing router recognizes UC-5

- [ ] `python3 scripts/probe_routing.py "triage INC0001001"` returns `agent_id=triage_agent, service_id=incident`
- [ ] `python3 scripts/probe_routing.py "triage REQ0007089"` returns `agent_id=triage_agent, service_id=request`
- [ ] `python3 scripts/probe_routing.py "triage"` (no ID) returns clarification request, not a guess
- [ ] No new code added — the existing 4-layer routing already handles all three cases
- [ ] Evidence: probe outputs captured

---

## Section 7 — Negative + security tests **[+ added]** (~30 min)

### D3 — Tenant isolation negative test

- [ ] T001 identity attempts to triage `INC0001234` where the ticket actually belongs to T002 → returns `TriageRefusal(reason=ticket_not_found_in_tenant)`
- [ ] No T002 data leaks into the response, span attributes, or logs
- [ ] Rule §2.4 cited

### D4 — RBAC negative test

- [ ] `end_user` role attempts to invoke `triage_agent` → router refuses BEFORE handler is called
- [ ] `viewer` role: same refusal
- [ ] `service_desk_agent` and above: succeeds
- [ ] Evidence: 3 trace IDs captured

### D5 — Idempotency test **[+ added]**

- [ ] Same triage called twice on the same ticket in advisory mode → identical `TriageDecision`, no duplicate work_note
- [ ] Same triage called twice in apply mode after the first apply committed → second call detects already-triaged and returns advisory-only

### D6 — LLM failure recovery **[+ added]**

- [ ] Force `gateway.call()` to raise `LlmGatewayError` (transient 5xx after retries) → handler returns `TriageRefusal(reason=llm_unavailable)`
- [ ] No bare exception escapes; no silent fallback to default values
- [ ] Evidence: trace shows typed refusal + ERROR span status

### D7 — DB failure recovery **[+ added]**

- [ ] Force `TicketStore.update()` to raise `PostgresUnavailable` → handler returns `TriageRefusal(reason=storage_unavailable, retryable=true)`
- [ ] Mutation does NOT partially commit (transaction enforced)

### D8 — Concurrency safety **[+ added]**

- [ ] Two simultaneous triage calls on different tickets in different sessions → no state bleed (each gets its own context, span tree, decision)
- [ ] Locust or asyncio.gather harness runs 10 concurrent triages → all 10 complete, no flakes

### D9 — Cost budget enforcement **[+ added]**

- [ ] If tenant cost would exceed the daily budget, handler refuses BEFORE the LLM call with `TriageRefusal(reason=quota_exceeded)`
- [ ] Per `docs/planning/production-maturity-plan.md §A.2` budget tiers — verifies the existing `QuotaGuard` is wired into UC-5
- [ ] Test: set `set_tenant_limit("T001", 0)`, attempt triage, assert refusal

### D10 — PII redaction before LLM **[+ added]**

- [ ] If a ticket title/description contains a literal email or phone number, the prompt sent to the LLM has `[EMAIL]` / `[PHONE]` placeholders (existing redaction pipeline applies)
- [ ] Test: seed an incident with `"Contact john.doe@example.com on 555-1234"`, run triage, assert the LLM prompt span hash does not contain the literal email/phone
- [ ] Rule §C15 cited

### D11 — WebSocket interrupt resilience **[+ added]**

- [ ] Browser disconnects during the `interrupt()` wait → checkpoint preserved
- [ ] Browser reconnects with same `session_id` → interrupt is resumable via `Command(resume={...})`
- [ ] Evidence: reconnect smoke test passes

---

## Section 8 — Live verification on real data (~30 min)

### F1 — 5 real T001 incidents triaged via chat UI

- [ ] Identity = T001 / USR00003 / service_desk_agent
- [ ] Triage INC0001001, INC0001004, INC0001006, INC0001007, INC0001008 (covering 5 different categories)
- [ ] Each returns a valid `TriageDecision` with confidence ≥ 0.50
- [ ] Each emits a complete Tempo trace
- [ ] Evidence: `ops/pmg-evidence/phase-2-uc05-routing.log` + 5 trace JSON dumps under `ops/pmg-evidence/traces/uc05-*.json`

### F2 — Span attribute audit on the 5 traces

- [ ] Every span has: `tenant_id`, `request_id`, `agent_id`, `agent_version`, `confidence_score`, `autonomy_level`
- [ ] No span has raw user text (rule §2.6 PII safety — only hashes per `safe_attrs.py`)
- [ ] Audit script: `python3 scripts/audit_uc05_spans.py ops/pmg-evidence/traces/uc05-*.json` exits 0

### F3 — Per-tenant cost recorded

- [ ] Grafana per-tenant cost dashboard shows non-zero LLM cost for T001 in the last 5 minutes after the 5 triage runs
- [ ] Cost attributable to UC-5: `ai.llm.cost_usd_micros{tenant_id="T001", agent_id="triage_agent"}` non-zero
- [ ] Evidence: Grafana screenshot to `ops/pmg-evidence/screenshots/per-tenant-cost-after-uc05.png`

### F4 — Apply path live

- [ ] Click Apply on the chat bubble for one of the 5 incidents
- [ ] Browser shows confirmation; `psql -c "SELECT ... FROM itsm.incident WHERE incident_id='INC...'"` shows the mutation
- [ ] Work note appended with agent_id, agent_version, confidence, timestamp
- [ ] Rollback: `python3 scripts/prepare_uc5_demo.py --rollback` restores the wiped fields
- [ ] Evidence: before/after `psql` outputs to `ops/pmg-evidence/phase-2-apply-roundtrip.log`

### F5 — Demo dry-run **[+ added]**

- [ ] Full PMG demo script (when the demo runbook ships in Phase 7) is executed once end-to-end before the meeting
- [ ] Every demo step works; no surprises
- [ ] Evidence: `ops/pmg-evidence/phase-2-demo-dryrun.log`

---

## Section 9 — Discipline rules (cross-cutting) (~10 min, run at end)

### G1 — Single LLM egress

- [ ] `grep -rE "openai\.|anthropic\.|litellm\." src/oneops/use_cases/uc05_triage/` returns ZERO hits
- [ ] All LLM/embedding calls go through `src/oneops/llm/gateway.py`

### G2 — Policy composer everywhere

- [ ] Every LLM call in UC-5 references `policy.composer.compose(Profile.X, ...)` — verified by grep
- [ ] Zero raw `f"You are..."` strings in UC-5 source

### G3 — Required span attributes everywhere

- [ ] Every span emitted by UC-5 carries `tenant_id` + `request_id` + `agent_id` + `agent_version` (the minimum 4)
- [ ] LLM-bearing spans additionally carry `confidence_score`, `autonomy_level`, `llm.cost_usd`
- [ ] Audit script confirms

### G4 — No bare except, no silent fallback

- [ ] `grep -E "except:\s*$|except Exception:.*pass" src/oneops/use_cases/uc05_triage/` returns ZERO hits
- [ ] Every failure path returns a typed result (Refusal, Error)

### G5 — Semantic principles only

- [ ] UC-5 registry descriptions are prose principles (intent: classify + route + recommend), never keyword lists
- [ ] No `_KEYWORDS = [...]` or similar pattern catalogs in UC-5 code
- [ ] Rule §2.1 cited

---

## Section 10 — No regression on existing functionality (~5 min)

### E1 — UC-1 summarization tests

- [ ] `pytest tests/unit/use_cases/uc01_summarization/ -v` → ALL green (same count as before Phase 2)

### E2 — UC-3 KB lookup tests

- [ ] `pytest tests/unit/use_cases/uc03_kb_lookup/ -v` → ALL green (same count as before Phase 2)

### E3 — Smoke routing 81/84

- [ ] `bash scripts/smoke_routing.py` (if present) → 81/84 passes as before
- [ ] If script is the Phase 6 fill-in, defer; mark with note

### E4 — Devil's-play 11/11

- [ ] `bash scripts/devils_play.py` (if present) → 11/11 passes as before
- [ ] If script is the Phase 6 fill-in, defer; mark with note

### E5 — Frontend doesn't break for UC-1/UC-3 users **[+ added]**

- [ ] Identity = T001 / USR00003 / service_desk_agent in chat
- [ ] Type "summarize INC0001001" → UC-1 still works as before, no Quick Action panel interference
- [ ] Type "how do I reset MFA" → UC-3 still works
- [ ] Evidence: two trace IDs captured

---

## Section 11 — CI gate + master verifier (~10 min)

### H1 — `make ci` green on full tree

- [ ] All 6 stages of `scripts/ci.sh` pass: ruff (after lint baseline ratchet per Task #18) + mypy + unit + integration + smoke + devil's-play
- [ ] Evidence: `ops/pmg-evidence/phase-2-ci-green.log`

### H2 — `make pmg-verify` Phase 2 row green

- [ ] `bash ops/pmg-evidence/verify-all.sh` shows Phase 2 row as ✅ green in REPORT.md
- [ ] `phase-2-uc05-routing.log` exists and is non-empty

---

## Section 12 — Documentation closure (~15 min)

### H3 — docs/runbooks/RUNBOOK.md updated

- [ ] `docs/runbooks/RUNBOOK.md` gains a section "Triage operations (UC-5)" with: how to invoke triage, how to roll back a misapplied triage, where the priority matrix lives, how to update it without redeploy

### H4 — Day-1 execution plan checkboxes synchronized

- [ ] `docs/planning/day1-execution-plan.md` Phase 2 sub-step checkboxes (steps 2.1–2.10) all ticked
- [ ] Cross-link from this file to the Day-1 plan and back

### H5 — Phase 2 evidence summary in production-maturity-plan §F-LOCKED

- [ ] `docs/planning/production-maturity-plan.md §F-LOCKED` Step 2 marked ✅ with a link to this checklist

---

## Master gate — Phase 2 closure

**Phase 2 is "done" only when every checkbox above is ticked AND the following 3 commands all pass cleanly:**

```bash
make ci                                    # all 6 stages green
make pmg-verify                            # Phase 2 row green in REPORT.md
pytest tests/ -v --tb=short                # the entire test suite green, no skips marked as "deferred"
```

If any of those three fails, Phase 2 is NOT done — regardless of how many checkboxes look ticked. Rule §2.7 applies to this checklist itself.

---

## What's NOT in Phase 2 (explicitly out of scope)

These belong to later phases or workstreams. Don't sneak them in:

- Misclassification Detector (Phase 5 + Workstream 3.1)
- Reversible PII token store (Workstream 3.2)
- Materialized RBAC matrix (P0-#3)
- AgentManifest export/import CLI (Phase 6 add-on or Day 2)
- UC-8 Fulfillment handler (Phase 4)
- Lifecycle state machine (Phase 3 — comes between UC-5 and UC-8)
- WebSocket Bridge Service (deferred per §G #2)
- ITOM agents UC-9..UC-14 (deferred per §G #4)
- Studio author plane (deferred per §G #3)

---

## Roll-up dashboard

| Section | Items | Ticked | Status |
|---|---|---|---|
| 0 Pre-flight data prep | 9 | 0 | not started |
| 1 Module skeleton + contracts | 2 | 0 | not started |
| 2 The 4 tools | 4 (+ ~14 sub-tests) | 0 | not started |
| 3 Handler orchestration | 1 (+ 7 sub-tests) | 0 | not started |
| 4 Integration wiring | 2 | 0 | not started |
| 5 Frontend additions | 5 | 0 | not started |
| 6 Routing decision | 1 | 0 | not started |
| 7 Negative + security tests | 9 | 0 | not started |
| 8 Live verification | 5 | 0 | not started |
| 9 Discipline rules | 5 | 0 | not started |
| 10 No-regression checks | 5 | 0 | not started |
| 11 CI + master verifier | 2 | 0 | not started |
| 12 Documentation closure | 3 | 0 | not started |

**Update this table as items tick. Phase 2 done = row totals match.**
