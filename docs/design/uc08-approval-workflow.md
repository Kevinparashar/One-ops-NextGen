# Design: Second-party (manager) approval for UC-8 Service Catalog

> Status: DRAFT — design only, not yet implemented.
> Context: today a UC-8 catalog request goes requester-confirm → create SR/RITM →
> NATS fulfilment immediately. There is no manager/second-party approval gate.
> The DB already has the substrate (`itsm.approval`, `request_item.approval_state`);
> this design wires the gate. Validated against ServiceNow, Jira SM, Freshservice,
> BMC Helix, Ivanti, ManageEngine — all converge on the same pattern.

## 1. Load-bearing decisions (the "why", up front)

1. **Approval is a durable state machine on the request — never an in-session
   interrupt.** The requester's turn ends at "submitted, pending approval." The
   approver decides *later, in their own session/channel*. This is how all 6
   vendors do it, and it's the one thing we must not get wrong.
2. **Approver is resolved from a central ATTRIBUTE-KEYED matrix, never named on
   the item (§2.1).** This is the universal pattern across ServiceNow (Decision
   Tables), SAP (BRF+), Oracle (AME), Coupa (approval chains), Workday (BP
   framework), and Camunda (DMN): catalog items carry only *attributes*
   (category, cost, department, `owner_group`, risk); a separate **approval
   matrix** maps `conditions → approver resolver`, evaluated top-down with a
   DEFAULT fallback row. Adding the Nth category = adding a row (or just tagging
   the item), not per-item config and not code.
3. **The gate sits between *create* and *NATS dispatch*.** RITM is persisted in
   `pending_approval`; fulfilment is **not** dispatched until the policy resolves
   to approved. Rejection means no tasks ever spawn.
4. **Reuse, don't rebuild.** The `itsm.approval` table, `request_item.approval_state`,
   the NATS fulfilment path, and the interrupt protocol all already exist. This
   design only adds the *policy eval + gate + approver resolution + approver surface*.
5. **Fail safe, never fail open (§2.7).** The matrix always ends in a guaranteed
   catch-all, and an unrecognised request routes to a human (owning team →
   service-desk queue), never silently auto-fulfils. Auto-fulfil happens ONLY via an
   explicit `required:false` rule. See §2.e.

## 2. Data model

### (a) Catalog item carries ATTRIBUTES only (no approver)

The item already has `category` and `owner_group`. We rely on those plus optional
attributes the request can supply (`estimated_cost`, `risk`, `data_classification`).
**No `approval_policy` lives on the item** — that was the per-item-wiring anti-pattern.

### (b) Central approval matrix — new `itsm.approval_policy` table (the decision table)

Rows of `conditions → approver resolver + stage`, evaluated **top-down,
most-specific first**, with a guaranteed DEFAULT fallback row. This is the
ServiceNow Decision-Table / SAP BRF+ / Coupa approval-chain / Camunda-DMN model.

```jsonc
// one row of itsm.approval_policy (seeded in data/itsm/approval_policy.json)
{
  "priority": 10,                                          // lower = evaluated first (most-specific wins)
  "match": {                                               // all conditions AND'd; omit a key = "any"
    "category": "Hardware",
    "estimated_cost": { "op": ">", "value": 1000 }
  },
  "stages": [
    {
      "name": "Owning-team approval",
      "approver": { "type": "owning_group" },              // resolves to the item's owner_group
      "rule": "any_one",                                   // any_one | everyone | n_of_m:2 | percentage:50
      "block_self_approval": true,
      "timeout_hours": 48,                                 // Phase 2
      "on_timeout": "escalate"                             // Phase 2
    }
    // additional stages run SEQUENTIALLY (stage N+1 opens only after N resolves)
  ]
}
```

Resolver `type` ∈ `owning_group` (the #1 case — the team that owns the category),
`manager_of_requester`, `group`, `user`, `cost_center_owner`/`dynamic` (Phase 2),
plus the **fail-safe** resolver `service_desk` (the catch-all queue).

A final row with empty `match` is the guaranteed DEFAULT so a request can **never**
dead-end. Its behaviour is **fail-safe, not fail-open** (see §2.e). Self-service
items that should auto-fulfil (password / MFA reset) get an **explicit** no-approval
row near the top — they are a deliberate decision, never the silent default. To make
a category require a specific approver, add/seed a row — no item edit, no code.

### (c) Request state machine — reuses existing columns

(`request_item.state`, `request_item.approval_state`)

```
                 approval not required
   requested ───────────────────────────────► in_progress ──► fulfilled
       │
       │ policy.required = true
       ▼
 pending_approval ──approve(final stage)──► approved ──► (NATS dispatch) ──► in_progress ──► fulfilled
       │
       └──────────────reject──────────────► rejected   (no fulfilment, requester notified)

 approval_state:  requested ─► approved | rejected   (existing CHECK constraint)
```

### (d) Approval records — `itsm.approval` already supports this fully

One row per approver per stage: `approval_type='manager'`,
`requested_from=<approver_id>`, `ritm_id`, `state='pending'`, plus
`decision`/`decided_by`/`decided_at` on resolution. Add a `stage_index` column
for multi-stage.

### (e) Fail-safe fallback — what happens when the matrix has no rule for a request

The matrix **can never "have nothing."** It is evaluated top-down and **always**
ends in a guaranteed catch-all row, so a request never dead-ends. The catch-all is
**fail-safe** (route to a human), **never fail-open** (silently auto-fulfil) — an
unrecognised request must not skip oversight.

**Resolution order for an unmatched request:**

```
1. a specific matrix row matches            → use it
2. no specific row, but the item has owner_group
                                            → the OWNING TEAM approves   (fail-safe, natural)
3. no owner_group at all (truly orphan)     → route to the SERVICE_DESK approval queue
                                              (resolver type `service_desk`)
```

This works because **every catalog item carries `owner_group`** — so even a brand-new,
never-seen item has an obvious backstop approver (the team that owns it). Only a
genuinely orphan item (no owner_group) falls through to the service-desk queue.

**Rules baked in:**
- **Never silently auto-approve an unknown.** The default routes to a human; it does
  NOT fulfil unattended. (Matches Coupa's "ultimate approver" and ServiceNow's
  "Default result" patterns.)
- **Self-service is explicit, not default.** Items that should auto-fulfil
  (password / MFA reset) carry an explicit `required:false` row near the top — a
  deliberate decision, never inferred from "no rule matched."
- **No silent gaps (§2.7).** When a request hits the generic catch-all (step 3), emit
  a log + an `ai.uc08.approval.fallback.total` metric tagged with the category, so an
  admin can add a precise rule later. The matrix grows by exception.
- **Catalog no-match ≠ approval fallback.** If there is no matching *catalog item* at
  all (not just no matrix rule), the request never reaches approval — it hits the
  catalog no-match path (offer to raise an incident / flag IT) per the runbook.
  Approval applies only to items that ARE in the catalog.

**Open knob:** the ultimate (orphan) fallback target — `service_desk` approval queue
(recommended, safe) vs `none`/auto-fulfil (frictionless, NOT recommended). Default =
`service_desk`.

## 3. End-to-end flow

```
REQUESTER SESSION                          │  APPROVER SESSION (later, different user)
───────────────────────────────────────── │ ──────────────────────────────────────────
1. conductor: search→pick→fill→CONFIRM     │
2. create_service_request:                 │
   • evaluate APPROVAL MATRIX vs the item's│
     attributes (category/cost/owner_group)│
     → first matching row's stages         │
   ┌─ no approval ─► dispatch NATS (today) │
   └─ approval ─►                          │
       • persist RITM state=pending_approval│
       • resolve stage-1 approver(s)        │
       • INSERT itsm.approval rows          │
       • NOTIFY approver(s)                 │
       • DO NOT dispatch NATS               │
3. reply: "Submitted — REQ123 is pending   │
   approval from <manager>."  TURN ENDS.    │
                                            │ 4. "show my pending approvals"
                                            │    → list_my_approvals (read)
                                            │ 5. picks one → approve / reject
                                            │    → decide_approval (action)
                                            │      • write decision to itsm.approval
                                            │      • evaluate stage rule
                                            │        ├ more stages → open next, notify
                                            │        ├ final approve → RITM approved
                                            │        │   → DISPATCH NATS (existing path)
                                            │        └ reject → RITM rejected, notify requester
```

The approver's approve/reject **is** an in-session interrupt
(`interrupt_for_selection` → `interrupt_for_confirmation`) — correct, because
*they* are the one acting now. We never hold the *requester's* session open.

## 4. Approver resolution (`approver.type` →)

The matrix row names a resolver *type*; the actual person/group is computed at
runtime. Approvers are resolved **indirectly** — never enumerated per item.

| type | resolves to | typical categories |
|---|---|---|
| `owning_group` | the item's `owner_group` (the team that owns the category) | **the default for most categories** — VPN→network, mailbox→messaging, access→security |
| `manager_of_requester` | `sys_user.manager` of the RITM's `requested_for` (the dot-walk every vendor uses) | hardware, generic spend, leave |
| `group` | members of a named role/group (RBAC registry / `sys_user_group`) | finance, security, CAB |
| `user` | a named `sys_user` id (catalog owner) | niche items with a single owner |
| `cost_center_owner` / `dynamic` | derived from request data (cost-center owner, asset owner) — Phase 2 | paid licenses, asset moves |
| `service_desk` | the catch-all approval queue (the fail-safe target, §2.e) | orphan items / unmatched requests |

Plus **amount-threshold escalation** (Phase 2): per-approver limits chain upward
(< $1k auto, manager, > $10k director+finance) — driven by limit data, not
hardcoded tiers (the Coupa/AME model).

**Self-approval guard:** if `block_self_approval` and the only resolved approver
== requester, escalate one level up or fail loudly (§2.7) — never silently
auto-approve.

### Separation of concerns (three independent role-sets — do not collapse)

| concern | question | where it lives |
|---|---|---|
| **Entitlement** | who can *request* this item? | existing RBAC / visibility (User-Criteria equivalent) |
| **Approval** | who must *approve* it? | the approval matrix (this design) |
| **Fulfilment** | who *does* the work? | the item's `tasks` / `owner_group` assignment (existing) |

These are deliberately distinct (per Microsoft Entra / ServiceNow). `owner_group`
may serve double duty as fulfiller AND `owning_group` approver, but they remain
conceptually separate knobs.

## 5. Approver UX surface (chat-native, MVP)

Two new registry tool records (card-driven routing, **no axis** —
`use_when`/`not_when` on the cards):

- **`list_my_approvals`** (read) — "what's waiting on me?" → returns pending
  `itsm.approval` where `requested_from = me`.
- **`decide_approval`** (action, `manages_own_approval:true`) — approve/reject a
  specific approval; the decide step is the gate.

Phase 2 adds out-of-band channels (email approve-by-reply, Slack/Teams buttons)
hitting `POST /api/uc08/approvals/{id}/decision` — same decision logic, different door.

## 6. Where each piece lives

| Piece | Location |
|---|---|
| `itsm.approval_policy` matrix table + seed | `database/catalog_fulfillment/` migration + `data/itsm/approval_policy.json` |
| `stage_index` column on `itsm.approval` | `database/catalog_fulfillment/` migration |
| Matrix eval, approver resolution, stage advance, decision apply | **new** `src/oneops/use_cases/uc08_fulfillment/approval.py` (pure, unit-tested) |
| Gate in create path; extract dispatch into a shared fn | `tools.py` / `core.py` (`create_service_request`, `fulfill_request`, `nats_dispatcher.dispatch_execute`) |
| `list_my_approvals` / `decide_approval` tools | `registries/v2/tools/uc08_fulfillment/` + handlers in `tools.py` |
| Routing | agent card `use_when`/`not_when` (no keyword catalog) |
| Notify | **new** `notify.py` — Phase 1: in-app record surfaceable in chat; Phase 2: email/Slack |
| Timeout/escalation scan | **new** periodic worker (like the embedding workers) — Phase 2 |

## 7. Phasing

**Phase 1 (MVP — the generic, matrix-driven gate):**
single-stage; the approver is resolved by the **approval matrix** from the item's
attributes using the cheap resolvers — `owning_group` (the default — the team that
owns the category), `manager_of_requester`, `group`, or `user`. Self-service items
(password / MFA reset) get an explicit `required:false` row → fulfil instantly like
today; everything unmatched **fails safe** to the owning team / service-desk queue
(§2.e), never auto-fulfils. · `any_one` voting · matrix evaluated top-down with a
guaranteed fail-safe fallback · gate before dispatch · in-chat
"My Approvals" + approve/reject · reject handling · self-approval guard ·
idempotent (no duplicate approvals on re-submit).

The point of Phase 1 is the **central attribute-keyed matrix** — adding a category
is a seed row, never per-item config. `cost_center_owner`/`dynamic` resolvers,
amount-threshold escalation, multi-stage chains (e.g. HR onboarding = HR group +
manager), and conditional voting are Phase 2.

**Phase 2:**
multi-stage sequential · conditional policy (cost/dept) · voting rules (`n_of_m`,
`percentage`, `everyone`) · timeout → auto-action + reminders · delegation ·
email/Slack approve · withdrawal/resubmit.

## 8. Rule alignment

§2.1 (policy is data) · §2.7 (resolution failures are loud, never silent
auto-approve) · §2.8 (state lives in DB + resumes via the existing
checkpointer/decision tool) · no-axis routing (cards, not keyword lists) ·
§2.6 (approval spans + an `ai.uc08.approval.*` metric).

## 9. Open decisions (before building)

1. **Approver channel for MVP** — in-chat "My Approvals" only, or also email
   approve-by-reply from day one? (changes `notify.py` scope)
2. **Matrix granularity for the demo** — key rows on `category` + `owner_group`
   only (simplest), or also `estimated_cost` thresholds from day one? (recommend
   category/owner_group for MVP; thresholds Phase 2.)
3. **Resolver data readiness** — is `owner_group` populated on our catalog items
   (yes — it exists) and are those groups resolvable to members? Is
   `sys_user.manager` populated for the `manager_of_requester` resolver? If not,
   seed group memberships + a reporting chain for the demo tenants.
4. **Self-approval** — block by default and escalate one level up? (recommended)

## 10. Competitor validation (why this shape)

All six leading platforms converge on: a **policy-driven, dynamically-resolved
approval gate that precedes fulfilment and short-circuits on rejection**,
decoupled from but bound to the catalog item, run as a **durable async cross-user
workflow**.

- ServiceNow: approval = step in bound Flow; `sysapproval_approver` records pause
  the process; `request.requested_for.manager` dot-walk; no SCTASK until RITM Approved.
- Jira SM: approval step on a workflow status; Approve/Decline transitions; Assets/
  automation-driven approvers; "Waiting for approval" blocks transition.
- Freshservice: Workflow Automator + Groups/Chains; native reporting manager;
  any/all/majority/first-responder voting; parallel chains.
- BMC Helix: SRD + Approval Mappings (Approval Server / `AP:Signature`);
  Management Chain (1–10 levels); "Waiting Approval" before PDT tasks.
- Ivanti: Get-Approval workflow block; dynamic (requester's manager); quorum/
  percent voting; 5 exit ports incl. Timed-out; "requester can't self-approve".
- ManageEngine: per-template workflow; up to 5 sequential stages; department head;
  technician assigned only after approval.

### Scaling to N categories — the matrix/decision-table pattern (the load-bearing finding)

A second research pass (ITSM + identity governance + procurement/ERP) showed every
system scales approver assignment the SAME way, which is why this design uses a
central matrix rather than per-item policy:

- **ServiceNow** — **Decision Tables** (`sys_decision`) map RITM attributes →
  approval group; one table reused across many flows; a **Default result** row
  catches new categories. Flow's "Ask for Approval" takes the group as a runtime
  data pill, so one reusable subflow serves hundreds of items.
- **Jira SM** — **Assets/CMDB Lookup-objects** + automation derive the Approvers
  field from object attributes; approver is computed, not hand-set.
- **Freshservice** — **Reader Node** reads a Custom Object by service-item
  attributes → returns the right group; Groups & Chains reused across items.
- **SAP** — **Flexible Workflow + BRF+** decision tables (one table decides the
  step, another decides WHO); **Ariba** approval rules keyed on
  commodity/cost/amount, evaluated top-down with a fallback.
- **Oracle** — **AME**: central rules repository (conditions → action-types that
  *generate* approvers via chain-of-authority); reusable approver groups with
  voting regimes.
- **Coupa** — **Approval Chains** (priority + conditions → approvers); management
  hierarchy at priority 50; per-approver amount limits auto-chain the next approver.
- **Workday** — **Business Process Framework**; routes to org **roles** (cost-center
  manager, supervisory-org manager), so personnel changes need no process edit.
- **Camunda/BPMN** — **DMN decision table** (Business Rule Task) outputs
  `assignee`/`candidateGroups`; **policy is separated from process** by design (DMN
  was created to pull this logic out of BPMN).

**The single universal pattern:** a declarative, attribute-keyed decision table
(the "approval matrix") maps request data → approver(s), resolved at runtime by
role/group/org-owner/hierarchy (+ amount thresholds), kept SEPARATE from the
workflow. Adding the Nth category = adding a ROW, not a branch. Plus: items hold
attributes only; entitlement/approval/fulfilment are three distinct role-sets.

Key sources: ServiceNow Decision Tables ([sn.works/CoE/FAQDecision](https://sn.works/CoE/FAQDecision)),
Oracle AME ([docs.oracle.com](https://docs.oracle.com/cd/E18727_01/doc.121/e13516/T405156T467237.htm)),
Coupa Approval Chains ([compass.coupa.com](https://compass.coupa.com/en-us/products/product-documentation/integration-technical-documentation/coupa-core-flat-files-(csv)/flat-file-(csv)-import/approval-chain-import)),
SAP BRF+ ([help.sap.com](https://help.sap.com/docs/SAP_S4HANA_ON-PREMISE/8308e6d301d54584a33cd04a9861bc52/92a02f8a18e9464da881bc6281c88327.html)),
Workday BP framework ([kognitivinc.com](https://kognitivinc.com/blog/workday-rule-based-business-process-configuration/)),
Camunda DMN approver assignment ([camunda.com](https://camunda.com/blog/2020/05/camunda-bpm-user-task-assignment-based-on-a-dmn-decision-table/)),
ABAC ([en.wikipedia.org/wiki/Attribute-based_access_control](https://en.wikipedia.org/wiki/Attribute-based_access_control)),
Microsoft Entra separation of duties ([learn.microsoft.com](https://learn.microsoft.com/en-us/entra/id-governance/entitlement-management-access-package-incompatible)).
