---
title: Day-1 execution plan — Option B (UC-5 + UC-8 + lifecycle + SLO + CI + evidence + decision package)
date: 2026-05-29
status: Active — drive top-to-bottom; tick each box as the step closes
locked_in: `docs/planning/production-maturity-plan.md §F-LOCKED`
---

# Day-1 execution plan

End-to-end ordered sequence. **Drive top-to-bottom.** Each step has: action, verification, evidence path, and the non-negotiable rule it must honour. Tick the checkbox when the step is verified working.

> **How to use:** start at Phase 1. Do not skip steps. Each step's verification is the gate to the next step. If a verification fails, fix the step before continuing — do not paper over it (rule §2.7 no silent failures). Evidence files land in `ops/pmg-evidence/` per the locked path.

---

## Phase 1 — Scaffolding (≈30 min)

The evidence directory + skeleton scripts must exist before any deliverable starts. Every later phase writes proof into this fixed home.

- [ ] **1.1** Create `ops/pmg-evidence/` directory with subfolders `traces/`, `dashboards/`, `screenshots/`, `manifests/`.
  - **Verify:** `ls ops/pmg-evidence/` returns 4 subfolders.
  - **Evidence:** N/A (scaffolding step).
  - **Rule:** §2.10 no file bloat — additive only.

- [ ] **1.2** Write `ops/pmg-evidence/README.md` explaining the convention: each phase writes `phase-N-name.log` + optional artefact under the matching subfolder.
  - **Verify:** README renders cleanly; cross-references the verify-all script.
  - **Evidence:** the README itself.
  - **Rule:** §2.10 docs explain WHY, not WHAT.

- [ ] **1.3** Write `ops/pmg-evidence/verify-all.sh` skeleton — a bash script that prints a section header per phase, will be filled in as phases close. Final output: `ops/pmg-evidence/REPORT.md` linking every claim to its log.
  - **Verify:** `bash ops/pmg-evidence/verify-all.sh` runs without errors (prints headers only at this stage).
  - **Evidence:** stdout captured to `ops/pmg-evidence/phase-1-scaffolding.log`.
  - **Rule:** §2.7 no silent failures — script `set -euo pipefail`.

- [ ] **1.4** Write `scripts/ci.sh` skeleton with the 6 stages stubbed (`ruff`, `mypy`, `pytest -m unit`, `pytest -m integration`, `python scripts/smoke_routing.py`, `python scripts/devils_play.py`). Each stage prints `--- STAGE: <name> ---` then exits 0 for now.
  - **Verify:** `bash scripts/ci.sh` runs through all 6 sections without errors.
  - **Evidence:** captured in 1.3's log.
  - **Rule:** §2.7 fail loud — `set -euo pipefail`.

- [ ] **1.5** Add `Makefile` target `ci: ; bash scripts/ci.sh`. Add `pmg-verify: ; bash ops/pmg-evidence/verify-all.sh`.
  - **Verify:** `make ci` runs the script; `make pmg-verify` runs the verifier.
  - **Evidence:** terminal output appended to 1.3's log.

**Phase 1 gate:** `make ci` and `make pmg-verify` both run cleanly. The evidence folder is the canonical home for every later claim. **Proceed to Phase 2.**

---

## Phase 2 — UC-5 Triage handler (≈2.5–3 h)

`triage_agent` registry entry, tool mappings, capability descriptor, and role-permission rows already exist. This phase adds **only the handler module**.

- [ ] **2.1** Verify the existing registry surface for UC-5 is intact.
  - **Command:** `python3 -c "import json; d=json.load(open('registries/agent-catalog-registry.json')); print([a for a in d['agent_catalog'] if a['agent_id']=='triage_agent'])"`
  - **Verify:** output shows the `triage_agent` entry with `capability_id=triage` and `supported_services=['incident','request']`.
  - **Rule:** §2 "agents are data" — the data already exists; we don't duplicate it in code.

- [ ] **2.2** Read the canonical handler pattern from `src/oneops/use_cases/uc01_summarization/handlers.py` to mirror its shape (handler signature, span coverage, error handling, policy composition, gateway call).
  - **Verify:** explicit list captured of the 5 patterns to mirror: (a) `(args, ctx) → result` signature, (b) span via `observability.span(...)`, (c) tenant scoping via `ctx.tenant`, (d) policy composition via `policy.composer.compose(Profile.X, ...)`, (e) LLM call via `llm.gateway.LlmGateway.call(...)`.
  - **Rule:** C23 placement — UC code lives in its folder; cross-UC patterns are reused, not re-invented.

- [ ] **2.3** Create folder `src/oneops/use_cases/uc05_triage/` with `__init__.py`, `contracts.py`, `handlers.py`, `tools.py`.
  - **Verify:** folder exists; `__init__.py` re-exports the public surface.
  - **Rule:** §2.10 + C23 — minimum file set, structured by responsibility.

- [ ] **2.4** Build `contracts.py` — Pydantic models: `TriageRequest`, `TriageDecision` (fields: `category`, `subcategory`, `priority`, `recommended_assignment_group`, `confidence_score`, `risk_class`, `rationale`, `mutation_intent` = "recommend_only"), `TriageRefusal` (typed error result).
  - **Verify:** `pytest tests/unit/use_cases/uc05_triage/test_contracts.py` passes (model validation, round-trip).
  - **Rule:** C7 Pydantic at boundaries; C8 structured output not prose.

- [ ] **2.5** Build `handlers.py` — main handler that:
  1. Validates `TriageRequest` (`tenant_id` mandatory).
  2. Loads the source incident via `TicketStore` scoped to `tenant_id`.
  3. Composes the prompt via `policy.composer.compose(Profile.PLATFORM_SYSTEM_POLICY, ...)` with the incident as context.
  4. Calls `llm.gateway.LlmGateway.call(...)` requesting structured `TriageDecision` output.
  5. Emits OTel span `uc05.triage.classify` with `tenant_id`, `request_id`, `agent_id=triage_agent`, `agent_version`, `confidence_score`, `autonomy_level=suggest_only`.
  6. Returns `TriageDecision` (success) or `TriageRefusal` (typed error) — never bare `None`.
  - **Verify:** unit tests cover happy path + 3 error paths (missing tenant, missing incident, LLM failure).
  - **Rules:** §2.3 policy layer mandatory; §2.4 tenant isolation; §2.5 single LLM egress; §2.6 observability mandatory; §2.7 no silent failures; C17 explicit result, never `None`.

- [ ] **2.6** Build `tools.py` — register one tool `triage_classify` via the `@register_tool` decorator with input schema `TriageRequest`, output schema `TriageDecision`, side_effect_class=`read`, risk_tier=`low`.
  - **Verify:** `python3 -c "from oneops.registry import ToolRegistry; print(ToolRegistry.get('triage_classify'))"` returns the entry.
  - **Rule:** §2 "agents are data" — tool registers via decorator at import; allowlist comes from `agent-tool-mapping.json`.

- [ ] **2.7** Wire `HandlerResolver` to find the UC-5 handler (entry-point convention — confirm by reading `uc01_summarization/__init__.py` and matching).
  - **Verify:** `python3 -c "from oneops.executor.handlers import HandlerResolver; print(HandlerResolver().resolve('triage_agent'))"` returns the UC-5 handler.

- [ ] **2.8** Write tests under `tests/unit/use_cases/uc05_triage/` — `test_contracts.py`, `test_handler_happy_path.py`, `test_handler_tenant_missing.py`, `test_handler_incident_missing.py`, `test_handler_llm_failure.py`.
  - **Verify:** `pytest tests/unit/use_cases/uc05_triage/ -v` reports 5 passing.
  - **Rule:** §2.9 production-grade testing; C22 adversarial + edge cases.

- [ ] **2.9** Write integration test `tests/integration/use_cases/test_uc05_e2e.py` that calls the handler against an in-memory `TicketStore` seeded with one T001 incident.
  - **Verify:** `pytest tests/integration/use_cases/test_uc05_e2e.py -v` reports 1 passing.

- [ ] **2.10** Run UC-5 against 5 real T001 incidents from `data/itsm/incident.json`. Capture stdout + Tempo trace IDs.
  - **Verify:** 5/5 incidents produce a valid `TriageDecision`; each trace ID is resolvable in Tempo at `:3201`.
  - **Evidence:** `ops/pmg-evidence/phase-2-uc05-routing.log` with one trace ID per incident, the `TriageDecision` payload, and the spans-emitted count.

**Phase 2 gate:** Unit tests + integration test + 5 real-incident run all green; evidence log shows valid trace IDs and structured decisions. **Proceed to Phase 3.**

---

## Phase 3 — Lifecycle state machine + boot enforcement (≈1 h)

Adds `version`/`status`/`lifecycle_stage`/`owner` to every catalog entry; boot validates; router refuses non-`active` agents.

- [ ] **3.1** Update `registries/agent-catalog-registry.json` — add `version: "1.0.0"`, `status: "active"`, `lifecycle_stage: "active"`, `owner: "ai-team"` to all 9 agents.
  - **Verify:** `python3 -c "import json; d=json.load(open('registries/agent-catalog-registry.json')); assert all(a.get('status')=='active' and a.get('version') for a in d['agent_catalog']); print('OK')"`.
  - **Rule:** §2 agents are data; C2 hot-reloadable & versioned.

- [ ] **3.2** Add boot-time validation in `src/oneops/registry/loader.py` — refuse to load if any agent is missing required lifecycle fields. Raise `ConfigError`, not `Exception`.
  - **Verify:** unit test in `tests/unit/registry/test_loader_lifecycle.py` — pass for valid catalog, fail with `ConfigError` for catalog missing `status` field.
  - **Rules:** §2.7 fail loud; C18 typed errors.

- [ ] **3.3** Add router gate: in `src/oneops/router/router.py` (or equivalent dispatch site), before invoking a handler, check `agent.status == "active"`. If not active, emit `lifecycle.refused` OTel span event with `agent_id`, `agent_version`, `lifecycle_stage`, return a typed `LifecycleRefusalError`.
  - **Verify:** unit test for both paths (active = pass-through; deprecated = refusal with span event).
  - **Rules:** §2.6 observability — span event with required attrs; §2.7 typed failure.

- [ ] **3.4** Add one synthetic deprecated agent `legacy_summary_agent` to the catalog with `status: "deprecated"`. Used only for the demo to prove the refusal path.
  - **Verify:** synthetic agent loads at boot (passes 3.2) but its routing path emits `lifecycle.refused`.

- [ ] **3.5** Run UC-1 (active) — succeeds normally. Attempt route to `legacy_summary_agent` (deprecated) — refused with span.
  - **Evidence:** `ops/pmg-evidence/phase-3-lifecycle.log` — boot validation output + 1 active-route trace ID + 1 refused-route trace ID.

**Phase 3 gate:** Boot validation catches malformed catalog. Active agents route. Deprecated agents are refused with span event. **Proceed to Phase 4.**

---

## Phase 4 — UC-8 Fulfillment handler (≈4–5 h)

`fulfillment_agent` registry entry already exists. Orchestration substrate (`Send` + `wave⇄run_step` + `interrupt()`) already exists in `src/oneops/executor/graph.py`. This phase adds **only the handler module** that plugs in.

- [ ] **4.1** Verify the existing registry surface for UC-8 is intact (mirror of 2.1 but for `fulfillment_agent`).
  - **Verify:** entry exists with `capability_id=fulfillment` and `supported_services=['request','catalog','onboarding']`.

- [ ] **4.2** Read `src/oneops/executor/graph.py` lines 1–60 + `src/oneops/executor/nodes.py` `dispatch_wave` to confirm the integration points: how a plan step list is consumed, how `Send` is emitted per parallel step, how `interrupt()` is triggered, how `Command(resume={...})` resumes.
  - **Verify:** explicit list of 4 integration points documented in the handler module's docstring.
  - **Rule:** §2.8 LangGraph-first — use existing primitives, don't re-implement.

- [ ] **4.3** Create folder `src/oneops/use_cases/uc08_fulfillment/` with `__init__.py`, `contracts.py`, `handlers.py`, `tools.py`, `decomposer.py`.
  - **Rule:** C23 placement.

- [ ] **4.4** Build `contracts.py` — Pydantic models: `FulfillmentRequest` (`catalog_item_id`, `requested_for`, `tenant_id`), `FulfillmentTask` (mirror of `catalog_item.tasks[]`: `task_id`, `name`, `type`, `owner_group`, `depends_on`, `risk_tier`, `automation_endpoint`), `WavePlan` (`waves: list[list[FulfillmentTask]]`), `FulfillmentResult` (`status`, `completed_tasks`, `pending_approval`, `failed_tasks`).
  - **Verify:** `pytest tests/unit/use_cases/uc08_fulfillment/test_contracts.py` passes.
  - **Rules:** C7 Pydantic at boundaries; C8 structured output.

- [ ] **4.5** Build `decomposer.py` — pure function `decompose(catalog_item) -> WavePlan`. Reads `tasks[]` and `depends_on[]`, produces a list of waves where wave[0] = tasks with no deps, wave[k] = tasks whose deps are all in waves[0..k-1]. Deterministic, no LLM.
  - **Verify:** unit test against the existing onboarding template — produces correct wave structure with parallel + sequential tasks.
  - **Rules:** C10 deterministic by default; §2 logic out of the LLM.

- [ ] **4.6** Build `handlers.py` — orchestrator handler that:
  1. Validates `FulfillmentRequest`.
  2. Loads catalog item from `CatalogStore` scoped to `tenant_id`.
  3. Calls `decomposer.decompose(...)` to produce `WavePlan`.
  4. For each wave: emit `Send` per parallel task via the existing `dispatch_wave` integration point.
  5. Between waves, check if any task has `risk_tier == "high"` — if yes, call `interrupt({"reason": "high_risk_approval", "task": ...})`.
  6. On resume (via `Command(resume={"approved": true})`), continue with next wave.
  7. Emit OTel span `uc08.fulfillment.execute` with `tenant_id`, `request_id`, `agent_id`, `agent_version`, `wave_count`, `approval_gate_count`.
  8. Return `FulfillmentResult`.
  - **Verify:** unit + integration tests.
  - **Rules:** §2.3 policy compose; §2.4 tenant scoping; §2.6 observability; §2.7 no silent failures; §2.8 LangGraph-first; C16 lifecycle hooks; C19 idempotent.

- [ ] **4.7** Build `tools.py` — register `fulfill_catalog_item` tool with `side_effect_class=mutation`, `risk_tier=high`.
  - **Rule:** §2 tools registered via decorator.

- [ ] **4.8** Wire `HandlerResolver` (mirror of 2.7).

- [ ] **4.9** Write tests: `test_contracts.py`, `test_decomposer.py`, `test_handler_parallel_wave.py`, `test_handler_interrupt_resume.py`, `test_handler_tenant_missing.py`.
  - **Verify:** `pytest tests/unit/use_cases/uc08_fulfillment/ -v` reports 5 passing.

- [ ] **4.10** Integration test `tests/integration/use_cases/test_uc08_e2e.py` — runs the full graph against one onboarding template, with the interrupt gate auto-resumed via fixture.
  - **Verify:** 1 passing; produces a full Tempo trace tree (wave-1 fan-out → interrupt → resume → wave-2 → aggregate).

- [ ] **4.11** End-to-end run against one onboarding template from `data/itsm/onboarding_template.json`. Capture full Tempo trace tree.
  - **Evidence:** `ops/pmg-evidence/phase-4-uc08-fulfillment.log` with trace ID + per-wave span counts + interrupt-resume timestamps.

**Phase 4 gate:** All UC-8 tests green; end-to-end run produces a full Tempo trace tree showing parallel wave + interrupt + resume + sequential wave. **Proceed to Phase 5.**

---

## Phase 5 — SLO alerts + per-tenant cost dashboard + synthetic probes (≈1 h)

Converts the existing OTel + LiteLLM cost tracking into operator-visible production-grade tracking.

- [ ] **5.1** Write `ops/prometheus/alerts.yml` with 4 SLO rules — `oneops_fast_path_p95_above_2s`, `oneops_standard_p95_above_6s`, `oneops_complex_p95_above_12s`, `oneops_multi_turn_p95_above_3s`. Each with `severity: warning` and a `runbook_url` pointing to `docs/runbooks/RUNBOOK.md#2`.
  - **Verify:** `promtool check rules ops/prometheus/alerts.yml` reports OK.
  - **Rule:** §2.7 alerts route to typed action, not silent log.

- [ ] **5.2** Write `ops/grafana/dashboards/per-tenant-cost.json` — Grafana JSON exporting one row of panels: per-tenant LLM cost (stacked), per-tenant request rate, per-tenant token spend, per-tenant SLO compliance. Labels: `tenant_id`, `model`, `agent_id`.
  - **Verify:** import into Grafana; visualises real data from the running stack.
  - **Evidence:** screenshot to `ops/pmg-evidence/screenshots/per-tenant-cost.png`.

- [ ] **5.3** Write `ops/probes/uc_synthetic.py` — script with one entry-point per UC (UC-1, UC-3, UC-5, UC-8). Each probe sends a canonical request, asserts a valid result, records latency + status to `ai.synthetic_probe.<uc_id>` metric.
  - **Verify:** `python3 ops/probes/uc_synthetic.py --once` returns green for all 4 UCs.
  - **Rule:** §2.6 observability — probes are first-class.

- [ ] **5.4** Add a cron entry / systemd timer (documented in RUNBOOK) running the probes every minute.
  - **Verify:** probes run every minute; 5 minutes of green captured.

- [ ] **5.5** Force an SLO breach — set UC-5 to sleep 5 s before responding. Run the probe. Confirm alert fires.
  - **Evidence:** `ops/pmg-evidence/phase-5-slo-alert.log` — probe before/after, alert payload from Prometheus.

**Phase 5 gate:** Dashboard shows real data per tenant. Probes green. Forced breach triggers alert. **Proceed to Phase 6.**

---

## Phase 6 — Local CI gate (≈1 h)

`scripts/ci.sh` skeleton already exists from Phase 1. This phase fills it in with the real test commands and wires the pre-commit hook.

- [ ] **6.1** Fill `scripts/ci.sh` with real stages: `ruff check src/ tests/`, `mypy src/`, `pytest -m unit -q`, `pytest -m integration -q`, `python scripts/smoke_routing.py`, `python scripts/devils_play.py`. Use `set -euo pipefail`; each stage fail-fast.
  - **Verify:** `bash scripts/ci.sh` runs all 6 stages on a clean tree; exit 0.

- [ ] **6.2** Add `Makefile` target `ci-fast: ; bash scripts/ci.sh --fast` (omits integration suite, used for pre-commit).

- [ ] **6.3** Write `.git/hooks/pre-commit` calling `scripts/ci.sh --fast`. Make executable.
  - **Verify:** `git commit` triggers the hook; clean tree → commit allowed; broken probe → commit blocked.
  - **Rule:** §2.9 production-grade testing.

- [ ] **6.4** Update `docs/runbooks/RUNBOOK.md` with one section — "Pre-merge gate: `make ci`. Pre-commit hook runs `make ci-fast` automatically."
  - **Rule:** §2.10 every deliverable lands with a RUNBOOK entry.

- [ ] **6.5** Run `make ci` end-to-end on the current tree — capture green run.
  - **Evidence:** `ops/pmg-evidence/phase-6-ci-gate-green.log`.

- [ ] **6.6** Deliberately break a probe (introduce one assertion that always fails). Run `make ci` — capture failure.
  - **Evidence:** `ops/pmg-evidence/phase-6-ci-gate-blocks.log`.
  - **Revert** the deliberate break.

**Phase 6 gate:** `make ci` green on clean tree; blocked on broken tree; pre-commit fires on `git commit`. **Proceed to Phase 7.**

---

## Phase 7 — Demo runbook + manager decision package + final report (≈30 min)

Ties everything into a PMG-deliverable bundle.

- [ ] **7.1** Write `docs/runbooks/pmg-demo-runbook.md`. Sections:
  1. Demo agenda (10–15 min live + 15–20 min documentation).
  2. Step-by-step script of the 8 live demos (UC-1, UC-3, UC-5, UC-8, forced SLO alert, cost dashboard, `make ci`, `verify-all.sh`). What to say at each.
  3. Evidence-log link per claim.
  4. Expected manager / PMG questions + crisp answers.
  - **Rule:** §2.10 docs explain WHY.

- [ ] **7.2** Write `docs/briefings/manager-decision-package.md`. The 10 §G open questions, each as: question / recommended answer / rationale / cost of alternative / downstream gates. Mark the 5 scoping-critical ones (EKS critical-path, Bridge Service in/out, intent ontology shape, PII reversibility, dual-cluster NATS).
  - **Rule:** §2.12 don't ask, drive — package frames decisions, not requests.

- [ ] **7.3** Fill in `ops/pmg-evidence/verify-all.sh` with one section per phase, each pointing to its `phase-N-*.log` artefact. Output: `ops/pmg-evidence/REPORT.md` with a row per phase, status, link.
  - **Verify:** `make pmg-verify` produces REPORT.md with all phases green.

- [ ] **7.4** Update `docs/runbooks/RUNBOOK.md` index — add pointers to the new docs (`docs/planning/day1-execution-plan.md`, `docs/runbooks/pmg-demo-runbook.md`, `docs/briefings/manager-decision-package.md`).

- [ ] **7.5** Identify and remove dead docs (`docs/history/internal-cleanup-notes.md`, `docs/history/CLEANUP.md` if superseded, `docs/planning/phase-status.md` if obsolete). Conservative — only delete clearly-dead files.

**Phase 7 gate:** PMG demo runbook + manager decision package + final evidence report all complete. Dead docs removed. **Day-1 deliverable bundle is PMG-ready.**

---

## Master verification — end of day

Run `make pmg-verify`. The script produces `ops/pmg-evidence/REPORT.md`. The report must show:

| Phase | Deliverable | Evidence | Status |
|---|---|---|---|
| 1 | Scaffolding | `phase-1-scaffolding.log` | green |
| 2 | UC-5 Triage | `phase-2-uc05-routing.log` + 5 trace IDs | green |
| 3 | Lifecycle state machine | `phase-3-lifecycle.log` + 2 trace IDs | green |
| 4 | UC-8 Fulfillment | `phase-4-uc08-fulfillment.log` + 1 trace tree | green |
| 5 | SLO + cost + probes | `phase-5-slo-alert.log` + dashboard screenshot | green |
| 6 | Local CI gate | `phase-6-ci-gate-green.log` + `phase-6-ci-gate-blocks.log` | green |
| 7 | Runbook + decision package + REPORT.md | docs + REPORT | green |

Anything not green → fix it before claiming Day 1 done. **No green tick on a verification means no green tick in REPORT.md.** Rule §2.7 no silent failures applies to the meta-report too.

---

## Non-negotiable thumb rules — cross-reference

Every step above carries at least one explicit rule citation. Master list:

| Rule | Source | Where it applies |
|---|---|---|
| Agents are data | `PROJECT-BRIEFING §2.13` | Phases 2.1, 4.1, 3.1, all registry edits |
| No keyword phrasebooks | `PROJECT-BRIEFING §2.1` | UC-5 and UC-8 use semantic registry descriptions, never user-text substring checks |
| Policy layer mandatory | `PROJECT-BRIEFING §2.3` | All LLM calls in UC-5 (2.5) and UC-8 (4.6) compose through `policy.composer` |
| Tenant isolation structural | `PROJECT-BRIEFING §2.4` | `tenant_id` required parameter on every UC-5 / UC-8 path; data-layer scoped reads only |
| Single LLM egress | `PROJECT-BRIEFING §2.5` | UC-5 / UC-8 call `llm.gateway.LlmGateway.call()` only; CI gate `test_no_direct_provider` enforces |
| Observability mandatory | `PROJECT-BRIEFING §2.6` | Every span carries `tenant_id` + `request_id` + `agent_id` + `agent_version`; refusal path emits a span event |
| No silent failures | `PROJECT-BRIEFING §2.7` | UC-5 returns `TriageDecision` or `TriageRefusal`, never `None`; UC-8 returns `FulfillmentResult` with explicit status; CI fails loud; alert fires loud |
| LangGraph-first | `PROJECT-BRIEFING §2.8` | UC-8 uses existing `Send` + `wave⇄run_step` + `interrupt()` substrate, does not re-implement orchestration |
| Production-grade testing | `PROJECT-BRIEFING §2.9` | Every step has unit + integration tests; smoke + devil's-play stay green |
| No file bloat | `PROJECT-BRIEFING §2.10` | Edit existing files where possible; new files justified by responsibility (UC folder, evidence folder); no docstrings explaining what code does |
| Don't ask, drive | `PROJECT-BRIEFING §2.12` | Plan is the contract; deviations surface only on hard blockers |
| Pydantic at boundaries | `COMPONENT_SPEC C7` | All UC-5 / UC-8 inputs and outputs are typed models |
| Structured output not prose | `COMPONENT_SPEC C8` | `TriageDecision` and `FulfillmentResult` are structured; never free-form |
| Failures typed and contained | `COMPONENT_SPEC C18` | `TriageRefusal`, `LifecycleRefusalError`, `FulfillmentResult.failed_tasks` are typed |
| Observable | `COMPONENT_SPEC C20` | Every step emits spans + metrics |

---

## What this plan does NOT do

Reminder of the explicit scope. These items remain in `docs/planning/production-maturity-plan.md §A.7 Scoping commitments` and are not built in Day 1:

- DOC-04 reversible PII token store
- DOC-04 hash-chained audit
- DOC-04 materialized `(role × tool)` RBAC matrix
- DOC-04 front-door user JWT verification
- DOC-08 WebSocket / Bridge Service / webhooks / ChatOps / SDKs
- DOC-09 ITOM (UC-9..UC-14)
- DOC-10 formal intent ontology
- DOC-11 EKS / Istio / Lambda / IaC / canary / DR / chaos
- DOC-13A platform UCs (UC-15..UC-29) + Studio
- UC-2, UC-4, UC-6, UC-7 — registered in catalog but not built; handlers deferred

Each one has a roadmap week in `docs/planning/production-maturity-plan.md §E`. PMG sees them deferred-with-rationale, not silently missed.
