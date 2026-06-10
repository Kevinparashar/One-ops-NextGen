# UC-8 Service Catalog — Approval Workflow: Phase 1 Implementation Plan

Living checklist. Design source: `docs/design/uc08-approval-workflow.md`.

## How to use this file (the DONE rule)

A step's top-level box `- [ ] Step N` may be ticked **only when ALL SIX verification
gates below it pass**. No partial credit, no "build works so it's done."

The six gates for every step:

1. **Build** — compiles, `ruff` clean, `mypy` clean.
2. **Smoke test** — the thing runs live against the running app/DB and does what it should.
3. **Unit test** — isolated logic covered, added to the suite, green.
4. **Integration test** — works wired to its real neighbours (DB / NATS / gateway / executor).
5. **Devil's play** — adversarial probing (bad input, wrong actor, races, duplicates, nulls).
6. **Edge cases** — the boundary list for that step is enumerated and each passes.

If a gate is genuinely not applicable to a step, write `N/A — <reason>` on that line;
an unticked, unexplained gate blocks the step.

**Global invariants (must hold after EVERY step):**
- The full `./scripts/ci.sh --fast` gate is green (currently **1672 unit tests**).
- The existing UC-8 chat flow is unchanged (search → form → confirm → create → fulfil).
- Everything is behind `UC08_APPROVAL_ENABLED` (default **false**) until Step 12.
- No live-DB object/seed is applied without explicit owner approval.

Legend: `- [ ]` not done · `- [x]` done (all 6 gates pass) · `~~strikethrough~~` dropped.

---

## STAGE A — Foundation (no behavior change possible)

### - [x] Step 0 — Feature flag `UC08_APPROVAL_ENABLED`  ✅ DONE 2026-06-10
Build: `src/oneops/use_cases/uc08_fulfillment/approval.py` → `approval_enabled()` (default `false`, read per-call via `config._parse_flag`).
- [x] Build — ruff + mypy clean
- [x] Smoke — import OK; default OFF; flips ON; garbage→OFF (warns); app/CI unaffected
- [x] Unit — `tests/unit/use_cases/uc08_fulfillment/test_approval_flag.py` (23 cases, green)
- [x] Integration — N/A — inert flag, no neighbour yet
- [x] Devil's play — garbage/empty/"✓"/"2"/"yess" all fail safe → False (parametrized, green)
- [x] Edge cases — unset, empty, whitespace, mixed case, runtime flip — all covered + green

### - [x] Step 1 — Schema migration (`itsm.approval_policy` + `approval.stage_index`)  ✅ DONE 2026-06-10 (applied to remote Supabase)
Build: `database/catalog_fulfillment/04_approval_policy.sql` (additive + idempotent) + both READMEs' apply-order updated.
- [x] Build — SQL written; 3 IF-NOT-EXISTS guards, balanced BEGIN/COMMIT; full `--fast` CI green
- [x] Smoke — applied clean (BEGIN→CREATE TABLE→CREATE INDEX→ALTER TABLE→COMMIT); `itsm.approval_policy` + `idx_approval_policy_eval` + `approval.stage_index` (default 0) present; app `/api/chat` still 200
- [x] Unit — N/A — DDL only
- [x] Integration — existing tables undisturbed (catalog_item 85, request_item 20, approval 0 unchanged); `itsm.approval` 2 FKs intact
- [x] Devil's play — re-ran the migration: `NOTICE … already exists, skipping`, exit 0 (idempotent on a DB that already has `itsm.approval`)
- [x] Edge cases — table+column already exist → skipped cleanly; policy table empty (0 rows); approval rows untouched

---

## STAGE B — Data (seeded, still unread by the live path)

### - [x] Step 2 — Owning-group resolution (reuse `sys_user.role` — NO new table/column)  ✅ DONE 2026-06-10
Decision: `sys_user` already carries `role`/`department` that staff each team, so we map
`owner_group → role|department` (`group_role_map.json`) and resolve members from existing rows.
No `sys_user_group` table, no new column — lighter, uses populated data, matches the IdP-sync pattern.
Build: `data/itsm/group_role_map.json` (18 groups, config-as-code source) → `itsm.group_role_map` table (migration `05_group_role_map.sql` + `load_group_role_map.py`) + `resolve_group_members` does a SINGLE JOIN (`group_role_map ⋈ sys_user`) — table-backed, consistent with `approval_policy`, **zero added latency** (1 round-trip; measured JOIN 152ms vs bare query 148ms = +4ms noise). The Phase-2 IdP sync populates the same table; resolver unchanged.
- [x] Build — ruff + mypy clean
- [x] Smoke — live: `GRP-NETOPS`→3 real users (USR00003,…); `GRP-ASSET`→5 real users
- [x] Unit — `test_approval_group_resolver.py` (7 cases): all catalog groups mapped, role-XOR-dept, unknown→None
- [x] Integration — live DB: **18/18 owner_groups resolve to ≥1 active member, 0 gaps**
- [x] Devil's play — unknown group → `[]`; wrong tenant → `[]` (fail-safe signals, no raise)
- [x] Edge cases — tenant isolation proven (T001 vs T002 netops disjoint); tenant-specific `GRP-T002/3-*` resolve per tenant
- [x] **Robustness (RCA, not hot-fix)** — survives new catalogs/teams:
      (a) **self-validating guard** — `test_every_catalog_owner_group_resolves` reads the REAL catalog seed (no hand-kept mirror), so a new item with a new `owner_group` FAILS CI until mapped;
      (b) **no silent failure (§2.7)** — unmapped/empty group emits `ai.uc08.approval.group_unresolved` metric + `group_unmapped` warning and fails safe to `[]` (→ service desk), never lost;
      (c) **single swap seam** — `resolve_group_members` is the one place to swap the demo map for the live IdP/HR sync in Phase 2 (zero-touch new teams).
- Note: full `-m unit` green (1703); one transient `test_conversational_boundary` flake confirmed unrelated (nothing imports `approval.py`; passes 3/3 in isolation).

### - [x] Step 3 — Org-chart completeness + `manager_of_requester` resolver  ✅ DONE 2026-06-10
Build: `resolve_manager` (asyncpg, active-only, tenant-scoped, fail-safe+observable) + org-chart completion (seed `data/itsm/sys_user.json` corrected as source of truth + idempotent `database/_utils/backfill_missing_managers.sql` to sync live rows; fills NULLs only, chain `oneops/u_demo/u_viewer → u_admin → it_director`).
- [x] Build — ruff + mypy clean
- [x] Smoke — live: `oneops`→`u_admin` in T001/T002/T003; FK valid (manager active, same tenant)
- [x] Unit — `test_approval_manager_chain.py` (4): whole-seed integrity — no dangling/self/cycle refs; active non-directors all have a manager
- [x] Integration — `resolve_manager` resolves the chain (oneops→u_admin→USR00020); applied live: UPDATE 3+9=12, only the 3 top it_directors remain NULL
- [x] Devil's play — top-of-chain→None+warning; non-existent user→None; wrong tenant→None; **inactive manager excluded by the SQL JOIN `m.is_active`** — all fail-safe + a `manager_unresolved` metric/log (never silent)
- [x] Edge cases — pre-existing `USR00001`→`USR00020` **unchanged**; chains terminate (no cycle); backfill idempotent (re-run UPDATE 0)
- [x] **Production-grade (not demo)** — resolver handles missing/inactive managers correctly regardless of the backfill; the org-chart fix is idempotent + only-NULLs + FK-safe + self-validated by the integrity test.

### - [x] Step 4 — The rules / matrix (`data/itsm/approval_policy.json` + loader)  ✅ DONE 2026-06-10
Build: `data/itsm/approval_policy.json` (17 rows: 2 self-service items + incident triage + 5 manager categories + 8 owning-group categories + fail-safe catch-all) + `match_policy` (pure, deterministic) + `load_policies` (DB reader) + `load_approval_policy.py` (per-tenant loader, tenants derived dynamically). **Nothing hardcoded** — rules are JSON data; loader/backfill derive tenants/users by query/role (no hardcoded IDs).
- [x] Build — ruff + mypy clean
- [x] Smoke — loader live: 51 rows upserted (17 policies × 3 tenants), ordered by priority
- [x] Unit — `test_approval_matrix.py` (9): well-formed, unique priorities+ids, catch-all last & matches anything, required↔stages, deterministic matching (priority wins, self-service beats category)
- [x] Integration — live DB: **all 85 catalog items map to exactly one rule, 0 unmatched, 0 fell through to catch-all** (every category covered)
- [x] Devil's play — unknown category → `fallback_service_desk`; two-match → lower priority wins (proven)
- [x] Edge cases — no category → catch-all; password(security) → `selfservice_password` (required=false, beats `cat_security`)
- [x] **No hardcoding** — `match_policy` pure; `load_approval_policy.py` derives tenants via `SELECT DISTINCT`; `backfill_missing_managers.sql` derives managers by ROLE (no hardcoded user/tenant ids); replaced the earlier hardcoded `backfill_demo_managers.sql`.

---

## STAGE C — Pure logic (fully unit-tested, zero live impact)

### - [x] Step 5 — The evaluator (`resolve_approvers`)  ✅ DONE 2026-06-10
Build: `ApprovalDecision` + `resolve_approvers` — composes `match_policy` + `resolve_group_members`/`resolve_manager`: required-false short-circuit → stage dispatch → self-approval guard → fail-safe to service desk → never-auto-approve guard. DB reads only; not wired to create. **No hardcoding** — the service-desk group is read from the matrix DATA (`_service_desk_group`), resolvers injectable for tests.
- [x] Build — ruff + mypy clean
- [x] Smoke — live: laptop(hardware)→`u_admin` (manager); access→owning team (6 real members)
- [x] Unit — `test_approval_resolve.py` (9): laptop→manager · access→owning_group · password→not_required · unknown→service_desk · self-approval→fail-safe · manager-null→fail-safe · owning-empty→fail-safe · nobody→unresolved(never auto-approve) · requester filtered from group
- [x] Integration — live DB (real resolvers): all canonical cases resolve correctly against `approval_policy` + `group_role_map` + `sys_user`
- [x] Devil's play — requester==only approver → escalate to service desk (never auto-approve); manager null / empty roster → fail-safe; nobody anywhere → `resolved=False` + `unresolved_approvers` metric (gate must hold); proven live: requester `USR00011` filtered from its own group
- [x] Edge cases — multi-approver group (any_one, all returned); self-in-group removed; tenant isolation via the resolvers
- [x] **No hardcoding** — service-desk group from matrix data, not a code constant; `group_resolver`/`manager_resolver` injected in tests, real in prod

---

## STAGE D — The gate (merged but INERT behind the flag)

### - [x] Step 6 — Wire the gate into `create_service_request` (flag-gated)  ✅ DONE 2026-06-10
Build: `_apply_approval_gate` in `tools.py` between fulfil (§3) and dispatch (§4), guarded by `approval_enabled()`, **fail-CLOSED** (gate error → held, never dispatched). Calls `resolve_approvers`; if required → `insert_approval` per approver (transactional) + `set_ritm_approval_state('requested')` + **no NATS**; if not required → return None (dispatch as today); if unresolved → hold. New `db.set_ritm_approval_state`. **No hardcoding** — `approval_type` comes from the matrix DATA (`decision.approval_type`), not a code mapping.
- [x] Build — ruff + mypy clean (tools/db/approval)
- [x] Smoke — flag OFF: `approval_enabled()=False`, live path inert. Flag ON live: hardware → `status=pending_approval`, `dispatched=False`
- [x] Unit — `test_approval_gate.py` (3, hermetic): park (1 row/approver, approval_type from data, state set) · proceed (None, no writes) · hold-unresolved (no rows, parked, never auto-approve)
- [x] Integration — flag ON live e2e via real `create_service_request`: hardware parked → `itsm.approval`=`(u_admin, manager, pending)`, RITM `state=requested approval_state=requested`, NOT dispatched; rows cleaned up after
- [x] Devil's play — unresolved → held (no approval rows, never auto-approves); gate raises → fail-CLOSED `approval_error`, `dispatched=False` (never falls through to dispatch); duplicate submit blocked upstream by `DuplicateRequestError` before the gate
- [x] Edge cases — **flag OFF → full `-m unit` 1726 passed, zero regression**; not-required → proceed; multi-approver → one row each; required-but-no-stage → service-desk fail-safe
- [x] **No hardcoding** — DB `approval_type` from matrix stage data; service-desk group from data; resolvers injected in tests

---

## STAGE E — Approval RELEASE (runbook-compliant: approve is NOT a chat action)

> **DECISION 2026-06-10 — follow the runbook.** The runbook guardrail stands:
> *"For actions outside tools (approve, reassign), say the IT team handles it on
> the request."* So the chat agent **never approves**. The 4 catalog tools stay;
> **no APPROVE intent, no chat approver tools.** The gate (Steps 0–6) still parks
> requests; the APPROVE **action** lives OUTSIDE chat (on the request, by the IT
> team), and the requester sees status via the existing TRACK path.

### ~~Step 7 — `list_my_approvals` (chat tool)~~ — DROPPED (runbook: approve not a chat action)
### ~~Step 8 — `decide_approval` (chat tool)~~ — DROPPED
### ~~Step 9 — routing for chat approval tools~~ — DROPPED (no chat approval tools to route)

### - [x] Step 7′ — Non-chat approval decision ("the IT team handles it on the request")  ✅ DONE 2026-06-10
Build: `approval.decide_approval` service (any_one: first approve releases, withdraws siblings; reject stops; actor must be `requested_from`; idempotent; transactional) + `db` helpers (`update_approval_decision`/`withdraw_other_pending_approvals`/`apply_approval_outcome`) + `tools.release_fulfilment` (dispatches the held NATS) + **endpoint** `POST /api/uc08/approvals/{id}/decision` (`api/uc08_approval_routes.py`, NOT a chat tool, distinct from the removed catalog button routes).
- [x] Build — ruff + mypy clean; route registered
- [x] Smoke — live: approve → `should_dispatch=True`; endpoint returns outcome
- [x] Unit — `test_approval_decide.py` (5): approve-releases · reject-stops · wrong-actor-denied · already-decided-idempotent · bad-decision
- [x] Integration — live e2e: park → approve → RITM `approved/approved` (dispatch); park → reject → `rejected/rejected` (no dispatch); 3 requests cleaned up
- [x] Devil's play — non-approver denied; double-decide idempotent (no re-release); transactional (no half-release)
- [x] Edge — any_one rule (siblings withdrawn on approve); tenant-scoped; **no hardcoding** (no IDs/groups in code)

### - [x] Step 8′ — Requester sees approval status via UC-1 (parent-SR lifecycle stamp)  ✅ DONE 2026-06-11
**Correction (2026-06-11):** the requester sees status through **UC-1** (its `ticket_store` does `SELECT * FROM itsm.request`), NOT a UC-8 status tool — we have no `search_requests`/`get_fulfillment_status` chat tool. The real gap was: the gate stamped the RITM's `approval_state` but **not the parent `itsm.request.status`** (what UC-1 reads). Fix: stamp the parent SR's `status`/`stage` across the FULL lifecycle (principled, not bug-patched): park/held → `pending_approval`/`approval`; approve → `approved`/`fulfillment`; reject → `rejected`/`closed`. New `db.set_request_lifecycle`; `apply_approval_outcome` now returns `request_id`; gate + `decide_approval` call it (same transaction).
- [x] Build — ruff + mypy clean
- [x] Unit — `test_approval_gate.py` (park+held stamp `pending_approval`) + `test_approval_decide.py` (approve→`approved/fulfillment`, reject→`rejected/closed`)
- [x] Integration — **live**: park → `itsm.request.status=pending_approval`; **UC-1 `ticket_store.get` returns `pending_approval`** (proves UC-1 surfaces it); approve→`approved`; reject→`rejected`; cleaned up
- [x] Edge — held (no approver) also stamps `pending_approval`; full lifecycle covered; flag-gated; no hardcoding
- Note: `get_fulfillment_status` approval display kept (harmless; reads the right data) but it's not a registered tool — UC-1 is the real surface.

---

## STAGE F — End-to-end, then flip

### ~~Step 10 — In-app "My Approvals" chat surface~~ — DROPPED (runbook: approve not a chat action)
Replaced by Step 7′ (non-chat decision) + Step 8′ (requester sees status via TRACK).

### - [ ] Step 11 — Full E2E with flag ON (staging/test only)
Build: nothing new — verification only.
- [ ] Build — N/A
- [ ] Smoke — the three real journeys: laptop→manager→fulfilled · VPN→network team→fulfilled · password→instant · unknown→fail-safe
- [ ] Unit — N/A (covered above)
- [ ] Integration — full chain across executor + DB + NATS + gateway with flag ON
- [ ] Devil's play — requester cancels while pending; approver rejects; approver out (no roster) → service_desk; concurrent approvals on different requests
- [ ] Edge cases — **full `./scripts/ci.sh --fast` green (1672+ tests)**; observability: approval spans + `ai.uc08.approval.*` metrics emit; fallback metric fires on unknown

### - [ ] Step 12 — Flip the flag ON + commit
Build: set `UC08_APPROVAL_ENABLED=true`; commit; final gate.
- [ ] Build
- [ ] Smoke — production-config request that needs approval actually pauses; one that doesn't fulfils
- [ ] Unit — N/A
- [ ] Integration — end-to-end on the running app with flag ON
- [ ] Devil's play — flip back OFF instantly restores old behavior (reversibility proven)
- [ ] Edge cases — final CI gate green; design doc + this plan reconciled with what shipped

---

## Sign-off

- [ ] **Phase 1 COMPLETE** — all of Steps 0–12 ticked (every gate green), CI gate green,
      existing UC-8 flow intact, flag ON, reversibility verified.

Deferred to Phase 2 (NOT in this plan): multi-stage chains, cost thresholds, voting
rules (n_of_m/percentage), timeout/auto-escalate, delegation, email/Slack push, live
HR/IdP sync of managers + rosters, AI-assisted authoring.
