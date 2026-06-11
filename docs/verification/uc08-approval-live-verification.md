# UC-8 Approval — live verification (real DB)

**Date:** 2026-06-11 · **Flag:** `UC08_APPROVAL_ENABLED=true` · **Tenant:** T001
**Reproduce:** `python dev/verify_uc08_approval.py` (writes + self-cleans demo rows on the configured `POSTGRES_URL`).

## Question
Yesterday's UC-8 work added the approval matrix + tables. Are they actually
**in use**, and does data flow through them correctly?

## Finding
The approval **infrastructure was fully in place but un-exercised** — `itsm.approval`
had **0 rows**. The matrix (`itsm.approval_policy`) and approver map
(`itsm.group_role_map`) were populated; the gate (`tools._apply_approval_gate`)
is wired into the create path (`tools.py` — after fulfil, before dispatch, when
`approval_enabled()`); the non-chat approve action (`approval.decide_approval`)
is implemented. This verification drives the **real** gate + decide functions
against the **real** DB and confirms the full data flow.

## Evidence — end-to-end, all three tables

Catalog item `CAT_HR_PORTAL_ACCESS` (category `access`), requester `USR00008`.

| Step | `itsm.request` | `itsm.request_item` | `itsm.approval` |
|---|---|---|---|
| **0 — created (pre-gate)** | status `open`, stage `—` | state `requested`, approval_state `not_required` | 0 rows |
| **1 — gate runs → PARK** | status **`pending_approval`**, stage **`approval`** | approval_state **`requested`** | **3 rows** (`state=pending`, `approval_type=catalog_owner`) |
| **2 — approve (USR00013)** | status **`approved`**, stage **`fulfillment`** | state **`approved`**, approval_state **`approved`** | approver row `approved/decided_by=USR00013`; **2 siblings `withdrawn`** |

## What this proves (data-driven, by design)
1. **Matrix resolution is data-driven** — `CAT_HR_PORTAL_ACCESS` → category `access`
   → policy `cat_access` → `approver_type=owning_group` (GRP-APPS). Log:
   `uc08.approval.parked policy_id=cat_access approver_type=owning_group approvers=3 fell_back=False`.
2. **Approver resolution via `group_role_map`** — GRP-APPS → role `application_support`
   → 3 real users (no hardcoded ids).
3. **Park is transactional** — one `itsm.approval` row per approver + `approval_state`
   + parent-SR `status/stage` stamped together.
4. **Requester visibility** — the parent `itsm.request.status=pending_approval` is
   what UC-1 / TRACK surface to the requester.
5. **`any_one` decide semantics** — the first approval releases the RITM and
   **withdraws the sibling pending rows**; `should_dispatch=True` releases fulfilment.
6. **Fail-safe** — required-but-unresolved would HOLD (set `approval_state` without
   writing approver rows), never auto-approve (§2.7).

## Status
UC-8 approval is **wired and functional** end-to-end. The mechanism produces and
updates data in `itsm.approval` / `itsm.request_item` / `itsm.request` exactly as
designed. Flag `UC08_APPROVAL_ENABLED` default state and rollout are unchanged by
this verification.
