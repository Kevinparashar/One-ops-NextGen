# Phase 5 — Send fan-out per sub-query (design, not code)

**Status:** Design under review. No code lands until this document is signed off.
**Author:** OneOps eng + AI partner, 2026-05-17
**Replaces:** task #23 (D1) — Phase 5 Send fan-out

---

## What Phase 5 is, and what it isn't

**Is:** Make every sub-query produced by the decomposer flow through its own independent rewriter → shortlister → reranker → planner-step pipeline in parallel via LangGraph `Send`. The planner emits one (or more) `PlanStep` per sub-query with explicit `depends_on` edges between sub-queries. The executor honors the DAG via wave-based execution.

**Isn't:**
- Implementing UC-2 action handlers (that's D8 — its own batch with `interrupt()` approval flow, idempotency keys, rollback semantics)
- Replacing the existing planner multi-Send (Example 0c) — that path keeps working for legacy/three_stage parity
- Eval-set expansion (D3) — separate workstream

**Scope discipline:** if any of those creep in during implementation, push back. Phase 5 is a routing-layer change.

---

## Architectural decisions (load-bearing — flag in review if any of these is wrong)

LBD-1 through LBD-8 — eight load-bearing decisions. LBDs 7 and 8 were added after the Finding 3 + Finding 5 review-cycle revision (2026-05-17).

### LBD-1 — Decomposer remains a SINGLE node; fan-out happens AFTER it

The decomposer produces `state["subqueries"]: list[dict]`. The graph then transitions into a fan-out edge that emits one `Send` per sub-query targeting a new node `sub_pipeline`. This keeps the decomposer's deterministic-trigger semantics intact and concentrates concurrency to a single edge.

### LBD-2 — Each Send carries a `SubPipelineInput` with sub-query-scoped focus

Each `Send` payload is NOT the full `OneOpsState` — it's a small `SubPipelineInput` dataclass containing:
- `sub_query_id: str` (e.g. `"s2"`)
- `sub_query_text: str`
- `depends_on: list[str]` (from decomposer)
- `inherited_focus: dict | None` (focus snapshot AT FAN-OUT TIME, not live focus)
- `request_envelope: dict` (request_id, tenant_id, user_id, role, session_id, locale)

This solves the per-sub-query focus scoping problem (Surface 1 below): focus is **snapshotted at fan-out time**, so sub-query 2 sees focus as it existed before any of the sibling sub-queries ran. No leakage between siblings.

### LBD-3 — Per-sub-query state lives in NEW Annotated channels, not in legacy fields

To preserve legacy mode's byte-identical behavior AND to allow concurrent multi-Send writes, Phase 5 adds these new channels (all `Annotated[dict[str, T], merge_by_subquery_id]`):

- `subquery_rewritten: Annotated[dict[str, str], merge_dict]` — `{sub_query_id: rewritten_text}`
- `subquery_shortlist: Annotated[dict[str, list], merge_dict]` — `{sub_query_id: shortlist_candidates}`
- `subquery_reranked:  Annotated[dict[str, list], merge_dict]` — `{sub_query_id: reranked_candidates}`
- `subquery_plan_steps: Annotated[dict[str, list], merge_dict]` — `{sub_query_id: list[PlanStep]}`

The existing `step_results` channel (already `Annotated[list[NodeOutcome], merge_step_results]`) is reused for execution outputs, with each `NodeOutcome.step_id` carrying its `sub_query_id` prefix so the aggregator can correlate.

### LBD-4 — Dependency edges are emitted by the planner, executed via wave-based Send

The decomposer emits `depends_on` HINTS. The planner verifies / refines and emits the final DAG. The executor groups `PlanStep`s into waves (steps with no unmet dependencies fire in parallel; subsequent waves fire when their dependencies complete). This is already the C1 wave-based-Send pattern; we extend it to also gate on **sub-query-level** dependencies.

### LBD-5 — Action sub-queries route through `interrupt()` before execution

Any `PlanStep` with `execution_type="action"` triggers a LangGraph `interrupt()` showing the user what would happen and waiting for approval. **Decomposer continues to be agnostic to read/action** — that classification happens at planner time, based on the chosen capability's `execution_type` field in `uc_capabilities`.

**Verified by probe (2026-05-17):** LangGraph allows per-branch `interrupt()` inside a Send fan-out. Parallel branches complete normally; only the interrupting branch blocks. Action sub-queries CAN be in the same wave as parallel reads — wave structure design stands as written. The `__interrupt__` field on the returned state is a list (supports multiple parallel interrupts in one turn). `Command(resume=...)` is the correct resume mechanism. See `/tmp/lg_interrupt_probe.py` and Phase 5 design log.

### LBD-6 — Cross-step state injection on dependent sub-queries

When a `PlanStep` for sub-query 2 has `depends_on=["s1"]`, the executor injects sub-query 1's `NodeOutcome.result["entity_id"]` (or similar canonical reference) into sub-query 2's parameters BEFORE execution. The rewriter doesn't try to do this at decomposition time; it's a runtime resolution.

### LBD-7 — Cross-step conditional schema (`PlanStep.condition`)

When sub-query 2's text expresses a conditional ("close it **if resolved**", "create a problem **if no fix exists**"), the **planner** parses the conditional during plan generation (an LLM task) and emits an explicit machine-readable structure on the PlanStep:

```python
class PlanStep(BaseModel):
    ...
    condition: dict | None = None
    # Shape: {"sub_id": "s1", "field": "status", "op": "==", "value": "resolved"}
    # Or compound (v1 keeps simple): None means unconditional
```

The **executor** evaluates the condition against the depended-on sub-query's `NodeOutcome.result` BEFORE deciding whether to fire `Send` / `interrupt()`. Three outcomes:
- Condition True → step fires (with cross-step state injection per LBD-6)
- Condition False → step marked `status="skipped"`, reason recorded
- Condition references a missing field on the upstream result → step marked `status="blocked"`, reason `"depended on missing field <X>"`

**Why this is a load-bearing decision:** "evaluate the conditional" was hand-wavy in the prior draft. Without an explicit schema, the executor would invent its own conditional format silently. With this LBD, the planner produces the structure and the executor reads it deterministically. No LLM call at evaluation time.

**v1 scope of `condition`** (per Q3 recommendation — accepted):
- Operators: `==`, `!=`, `in`, `not_in`, `is_null`, `is_not_null`, `contains`
- One field per condition (no AND/OR compounds yet)
- `contains` covers substring matching for string fields (e.g. *"if `assignment_group` contains 'NETOPS'"*) — a single elif branch in the executor's dispatcher, common ITSM pattern; cheap to include in v1
- Deferred to v2: numeric `<`/`>` (rare in ITSM where most numeric fields are priorities P1-P5 handled via `in`), regex matching, compound AND/OR
- Compound conditions defer to v2; the simple shape is forward-compatible

### LBD-8 — Legacy preservation: `ROUTING_MODE=legacy` does NOT add Phase 5 nodes

Under `ROUTING_MODE=legacy`:
- The graph DOES NOT wire `sub_pipeline`, the fan-out edge, or any Phase 5 node
- The new state channels (`subquery_*`) exist in the `OneOpsState` TypedDict but stay empty
- UC-1's existing path (`load_session → content_safety → planner → uc_executor → aggregator`) is **byte-identical** to today

This matches Phase 4 batch 1's wiring-exclusion pattern (see `graph/builder.py` where `routing_mode == "three_stage"` gates the addition of decomposer/rewriter/shortlist/rerank nodes). Phase 5's nodes are gated identically. Adding the nodes to legacy mode would be a regression.

**Test invariant (step 5.4's regression gate):** UC-1 sanity 3-probe under `ROUTING_MODE=legacy` produces byte-identical `user_response` to the Phase 4 batch 1 baseline.

---

## Surface 1 — Per-sub-query focus scoping

**Problem:** *"summarize INC0001001 and check CHG0004007's status"* → after decomposition, sub-query 1 has focus = "INC0001001 just mentioned" while sub-query 2 might erroneously inherit that as it's rewritten. The rewriter uses `focus.active_subject` — if it sees INC0001001's focus while rewriting *"check CHG0004007's status"*, it could mis-resolve a future pronoun.

**Design:**

1. **Decomposer reads CURRENT focus only** (option C — same input contract as today; no change)
2. **Fan-out edge SNAPSHOTS focus once**, passing the snapshot to each `Send` payload
3. **Sub-pipeline rewriter operates on the snapshotted focus**, NOT the live focus channel
4. **If a sub-query's text already names an entity** (prefix_router match), the rewriter ignores focus entirely — that entity wins
5. **Cross-sub-query reference resolution is DEFERRED to v2** — if sub-query 2 needs the result of sub-query 1, it's a `depends_on` edge, not a focus inheritance

The rewriter is updated to accept a `focus_override: dict | None` parameter (load-bearing — easy to test in isolation).

**Failure mode caught:** if sub-query 2 says *"close it"* with no entity and the snapshot focus is INC0001001 from BEFORE the user's message, the rewriter resolves "it" → INC0001001. That's almost always wrong (user probably meant "close it" referring to a result from sub-query 1). **Mitigation:** when a sub-query has a non-empty `depends_on`, the rewriter does NOT apply Branch 1 pronoun substitution — it leaves the pronoun for runtime cross-step state injection (LBD-6 below).

### LBD-6 — Cross-step state injection on dependent sub-queries

When a `PlanStep` for sub-query 2 has `depends_on=["s1"]`, the executor injects sub-query 1's `NodeOutcome.result["entity_id"]` (or similar canonical reference) into sub-query 2's parameters BEFORE execution. The rewriter doesn't try to do this at decomposition time; it's a runtime resolution.

---

## Surface 2 — Dependency edges between sub-queries

### Read → read (parallel by default)

*"summarize INC0001001 and CHG0004007"* — two reads, no dependency.
- Decomposer: `s1, s2` both `depends_on=[]`
- Planner: emits 2 PlanSteps, also `depends_on=[]`
- Executor: single wave, both fire via `Send`

### Read → action (sequential, with cross-step injection)

*"summarize INC0001001 and close it if resolved"* — read first, action conditional on result.
- Decomposer: `s1="summarize INC0001001"` (no deps), `s2="close it if resolved"` (`depends_on=["s1"]`)
- Planner: emits s1's PlanStep with `execution_type="read"`, s2's with `execution_type="action"` and `requires_approval=true`
- Executor:
  - Wave 1: fire s1 via `Send`
  - Wait for s1 completion
  - **Cross-step injection:** read s1's `NodeOutcome.result["status"]`, evaluate the conditional ("if resolved")
  - If condition is True → emit s2's `interrupt()` requesting approval → on approve, fire s2
  - If condition is False → mark s2 as `status="skipped"` with reason

### Action → read (rare, but possible)

*"close INC0001001 and then summarize it"* — close first, then summary that should see the post-close state.
- Decomposer: s1 action (`depends_on=[]`, `requires_approval=true`), s2 read (`depends_on=["s1"]`)
- Executor honors the dependency naturally

### Worked example — `"summarize INC0001001 and find related KB"`

```
Decomposer output:
  s1 = "summarize INC0001001"          depends_on = []
  s2 = "find related KB for it"        depends_on = []   ← no real data dep

Wave 1 (parallel via Send):
  s1 → planner emits summarization_agent / summary
  s2 → rewriter resolves "it" → "find related KB for INC0001001"
       (focus snapshot from fan-out OR prefix_router matches INC in s2's text)
       → planner emits kb_lookup_agent / find_related_kb_for_incident

Both execute in parallel. Aggregator stitches.
```

### Conditional-execution failure modes (named explicitly)

1. **s1 succeeds but the conditional in s2 evaluates to False** → s2 marked `status="skipped"`, reason surfaced in user response
2. **s1 fails (LLM error / authz / not_found)** → s2 marked `status="blocked"`, NOT executed, surfaced to user
3. **s1 succeeds but its result doesn't contain the field s2's conditional references** → s2 marked `status="blocked"` with reason `"depended on missing field <X>"`, surfaced to user

---

## Surface 3 — Confirmation gates and mixed read+action waves

### `interrupt()` is per-action-step, not per-wave

A wave can contain multiple PlanSteps. If wave N has 2 reads + 1 action, the reads fire immediately via `Send`; the action emits `interrupt()` and the wave blocks ONLY on the action approval.

When the user approves (or denies) via the resume mechanism, the action either executes (approved) or is marked `status="denied"` (denied) — the wave then completes.

### Approval payload (user sees)

```
Pending action: close INC0001001 (because s1 confirmed status = resolved)
  - Tool: ticket_action_agent / close
  - Target: INC0001001
  - Side effects: status → closed, close_code → "user_confirmed_resolved"
  - This action is irreversible without manual reopen.
Approve? [yes / no]
```

This payload is **structured, not free-form** — the user sees exactly which tool, which target, which side effects. UC-2 (D8) owns the payload-shaping logic; Phase 5 owns the gating mechanism.

### Idempotency note (deferred to D8 with explicit prereq)

Idempotency keys, rollback semantics, retry policies for actions are D8 scope. Phase 5 emits the `interrupt()` but treats the action handler as a black box that either succeeds or fails. **D8 must be built before three_stage is flipped on with action UCs registered.**

---

## Surface 4 — Aggregator stitching pattern

### Pattern: section-header per successful sub-query, footer for partial-failure

Each section's header derives from the sub-query text (first 60 chars of `sub_query_text`, single-line, first letter capitalized). Handler raw output goes between headers verbatim — no LLM re-stitching at the aggregator level (that's overhead and a hallucination surface).

### Worked output 1 — 3-sub-query all-success

**User asks:** `"summarize INC0001001, find related KB, and check CHG0004007 status"`

**Decomposer emits** (using indented quote to avoid markdown nesting issues — the rule applies to the *quoting in this document*, not the production output):

> s1 = "summarize INC0001001"          depends_on=[]
> s2 = "find related KB"               depends_on=[]
> s3 = "check CHG0004007 status"       depends_on=[]

**All three execute successfully.** `succeeded = 3 == subqueries = 3` → NO disclaimer fires.

**Verbatim user response (each line is what the user literally sees):**

> **Part 1 — Summarize INC0001001**
>
> \`\`\`
> **INC0001001 — VPN disconnects on Wi-Fi handoff**
>
> **Status**: open | **Priority**: P2 | **Assigned**: USR00003 / GRP-NETOPS
>
> **What happened**: VPN session drops every time the user moves between SSIDs at HQ...
> \`\`\`
>
> ---
>
> **Part 2 — Find related KB**
>
> **Fix VPN disconnects on Wi-Fi handoff** (KB0005001)
>
> To resolve VPN disconnects, apply VPN client profile v2.3 to fix tunnel drops when roaming between access points...
>
> 📎 Sources: KB0005001
>
> ---
>
> **Part 3 — Check CHG0004007 status**
>
> **Status**: closed
> _(for CHG0004007)_

**Rendering verification (Finding 1, 2026-05-17 probe):** the produced response contains exactly 2 triple-backtick markers (a paired open/close around UC-1's summary block). All other markdown structure parses cleanly — bold for headers, `---` for separators, italics for UC-1's parenthetical, emoji for UC-3's source attribution. **Format A (`**Part N — text**`) is the verified format.** Q1 recommendation stands.

### Worked output 2 — Mixed-status (2 succeed, 1 fails with transient error)

**User asks:** same as above. **s1, s2 succeed. s3 fails after retries with `error_class="transient"`.**

**Verbatim user response:**

> **Part 1 — Summarize INC0001001**
>
> \`\`\`
> **INC0001001 — VPN disconnects on Wi-Fi handoff**
> **Status**: open | **Priority**: P2 | **Assigned**: USR00003 / GRP-NETOPS
> ...
> \`\`\`
>
> ---
>
> **Part 2 — Find related KB**
>
> **Fix VPN disconnects on Wi-Fi handoff** (KB0005001)
> ...
> 📎 Sources: KB0005001
>
> ---
>
> I addressed: 'summarize INC0001001'; 'find related KB'. I did not act on: 'check CHG0004007 status' — a temporary system issue prevented that request from completing (already retried 3 times). Please try sending that part again in 60–90 seconds. If the same part keeps failing, contact OneOps support.

**Disclaimer-vs-section-header interaction:**
- Sections 1 and 2 render normally with their headers
- Section 3 does NOT get a `**Part 3 —**` header (because it didn't succeed)
- The disclaimer at the bottom NAMES the handled parts AND the failed one AND includes the V-3-specific actionable hint from Surface 5 (verbatim — 60–90 second retry window + OneOps-support escalation)

### Worked output 3 — Single-sub-query turn (NO header — legacy behavior preserved)

**User asks:** `"summarize INC0001001"`

**Decomposer:** single-intent passthrough, `subqueries = [{"id":"s1","text":"summarize INC0001001"}]`.

**Verbatim user response (byte-identical to today's UC-1 output):**

> \`\`\`
> **INC0001001 — VPN disconnects on Wi-Fi handoff**
>
> **Status**: open | **Priority**: P2 | **Assigned**: USR00003 / GRP-NETOPS
> ...
> \`\`\`

No `**Part 1 —**` header. UC-1's existing format is preserved for single-intent queries — critical for backward compatibility. Implementation rule: header only when `len(subqueries) >= 2`.

### Header generation rule (deterministic, no LLM)

- Take the first 60 chars of `sub_query_text`, stripped to a single line
- Capitalize first letter
- **Strip a single trailing `?` if present** — question-shaped sub-queries like *"What is INC0001001's priority?"* render as headers cleanly without the trailing punctuation. Other trailing punctuation (`.`, `!`, `;`) is also stripped. Internal punctuation is preserved.
- Append nothing — the rule produces stable, predictable headers
- Examples:
  - *"summarize INC0001001"* → `**Part 1 — Summarize INC0001001**`
  - *"what is the priority?"* → `**Part 1 — What is the priority**`
  - *"close it if status is resolved."* → `**Part 1 — Close it if status is resolved**`

### Single-sub-query response (legacy behavior preserved)

If `len(subqueries) == 1`, NO header is added. The response is byte-identical to today's single-handler output. **This preserves UC-1's existing format for all single-intent queries.**

### Mixed-status stitching

If sub-queries 1, 2 succeed and 3 fails:
```
**Part 1 — INC0001001 summary**
<s1 content>

---

**Part 2 — Related KB articles**
<s2 content>

---

I addressed: 'summarize INC0001001'; 'find related KB'.
I did not act on: 'check CHG0004007 status' (failed: <reason>).
Please send the remaining part as a follow-up.
```

The G-MultiIntent disclaimer template is reused but extended to name the failure reason. The disclaimer fires per the existing invariant: `len(succeeded) < len(subqueries)`.

---

## Surface 5 — Partial-failure semantics (three failure-type variants)

Each user-visible failure text must pass this test: **a user reading it must know the next concrete action they can take.** Vague phrasings ("contact your administrator", "this isn't available") fail the test. Each text below names a specific actor, a specific channel, OR a specific time window.

### V-1: Authorization failure

User's role lacks scope for the chosen UC + capability. Example: `end_user` role asking for an action-tier capability.

- `step_results[i].status = "failed"`, `error_class = "authz"`, `error_message = "authz: role 'end_user' lacks scope 'write:incident'"`
- **User-visible text** (actionable — names the channel):
  > *"I did not act on '<sub-query text>' — your role (`<role>`) doesn't have permission for that. To request access, contact your tenant's IT administrator or open an access-request ticket via your service portal. The required scope is `<scope>`."*
- Logged with `event=authz_denial`, `role`, `capability`, `scope`, `tenant_id`
- Note: the user-visible text NAMES the role they have AND the scope they're missing — so the IT admin they contact has the information needed to grant access without back-and-forth.

### V-2: UC-missing / capability-not-built

Planner picked a capability whose handler isn't registered (e.g. shortlister surfaced an `inactive_in_registry` agent). Should be caught by integrity check, but if it slips:

- `step_results[i].status = "failed"`, `error_class = "uc_missing"`, `error_message = "uc_not_built: <agent_id>/<capability_id>"`
- **User-visible text** (actionable — names whether to retry or never):
  > *"I did not act on '<sub-query text>' — I can't complete this kind of request in the current configuration. Re-sending will not help. If this capability is important to your team, please raise it with your OneOps administrator so it can be prioritized for a future release."*
- Logged with `event=uc_missing`, severity=`alert` (this should be impossible — surfaces as a registry-integrity bug)
- Note: the user-visible text explicitly says **re-sending will not help** so the user doesn't waste time retrying. Names the escalation path (OneOps admin). Wording avoids the self-contradiction of "isn't supported in the system yet" (the system *parsed* the request — the limit is configuration, not understanding).

### V-3: LLM error / transient infrastructure

Reranker times out, gateway returns 5xx, etc.

- RetryPolicy on the node applies first (3 attempts with exponential backoff per existing `transient_retry` policy in graph builder)
- After retries exhausted: `step_results[i].status = "failed"`, `error_class = "transient"`, `error_message = "transient: <reason>"`
- **User-visible text** (actionable — names a concrete time window):
  > *"I did not act on '<sub-query text>' — a temporary system issue prevented that request from completing (already retried 3 times). Please try sending that part again in 60–90 seconds. If the same part keeps failing, contact OneOps support."*
- Logged with `event=transient_failure`, latency, retry_count
- Note: the user-visible text names a **specific time window** (60–90 seconds) so the user knows when to retry, and provides an escalation path if the same part keeps failing across retries (so they're not stuck in a perpetual retry loop).

### Actionable-text invariant (assertion for step 5.9 smoke gate)

Every user-visible text in this section MUST contain at least one of these
phrase shapes (validated by regex in the smoke):
- An actor name (`administrator`, `OneOps admin`, `support`, `IT administrator`)
- A specific time window (`60–90 seconds`, `1 minute`, `tomorrow morning`)
- An explicit "do not" instruction (`re-sending will not help`, `this is irreversible`)
- A specific scope / capability name when the user can request it

This is the assertion the V-1/V-2/V-3 unit tests check, not just "the text exists."

### Consistency: each variant gets a structured `error_class` field

```python
class NodeOutcome(TypedDict, total=False):
    ...
    error_class: Literal["authz", "uc_missing", "transient", "unknown"] | None
```

The aggregator's user-visible language is selected from a small dispatch table keyed on `error_class`. No LLM call for error message phrasing — too risky.

---

## Surface 6 — New state channels, classified against runbook R1–R6

**⚠ ALL FOUR new channels use `Annotated[dict[str, T], merge_dict]`.** The runbook
(see `docs/runbooks/state-channel-additions.md` Caveat A) explicitly lists
dict-merge as "likely safe but UNVERIFIED — re-test." This pattern is NOT
covered by the Path-B-shape verification we did in Phase 4 batch 1 (which
verified only `Annotated[list[dict], list-concat-reducer]`).

Step 5.1 is the verification gate: dict-merge Path-B-shape smoke MUST run
and pass before any of these channels go live. Until 5.1 passes, R2 status
for these channels is ⚠ pending, not ✓.

### Channel: `subquery_rewritten`

```python
subquery_rewritten: Annotated[dict[str, str], merge_dict]
```
- **R1** ✓ Annotated with reducer
- **R2** ⚠ Identity is `{}` — **pending step 5.1 dict-merge Path-B smoke** (Caveat A)
- **R3** Defensive: nodes use `state.get("subquery_rewritten") or {}`
- **R4** ⚠ pending step 5.1 — must verify `merge_dict(identity, {"s1":"x"})` returns `{"s1":"x"}` AND `merge_dict({"s1":"x"}, {"s2":"y"})` returns `{"s1":"x","s2":"y"}` AND no None-input crash
- **R5** N/A (additive)
- **R6** ⚠ pending step 5.1 — runbook step explicitly required for this Caveat A pattern

### Channel: `subquery_shortlist`

```python
subquery_shortlist: Annotated[dict[str, list[dict]], merge_dict]
```
- R1–R6: ⚠ same Caveat A status as `subquery_rewritten` — verification gated to step 5.1

### Channel: `subquery_reranked`

```python
subquery_reranked: Annotated[dict[str, list[dict]], merge_dict]
```
- R1–R6: ⚠ same Caveat A status as `subquery_rewritten` — verification gated to step 5.1

### Channel: `subquery_plan_steps`

```python
subquery_plan_steps: Annotated[dict[str, list[dict]], merge_dict]
```
- R1–R6: ⚠ same Caveat A status as `subquery_rewritten` — verification gated to step 5.1

**If step 5.1 fails:** the design changes. Three options if dict-merge is
unsafe on restore: (a) use four separate `Annotated[list[dict], list-concat]`
channels with sub_query_id as a record field instead of dict key, (b) implement
a custom reducer with explicit identity construction, (c) accept a manual
migration handshake. Re-design lands BEFORE 5.2 starts.

### Channel: `step_results` (EXISTING, reused — no new channel)

Already `Annotated[list[NodeOutcome], merge_step_results]`. Phase 5 uses it as-is; each `NodeOutcome.step_id` is prefixed with the originating `sub_query_id` (e.g. `"s1.step_1"`) so the aggregator can group by sub-query.

**Prefix convention — where the wrap happens (cross-cutting concern):** the **executor** wraps NodeOutcomes from handlers before merging them into `step_results`. UC handlers (UC-1, UC-3, UC-99, future UCs) continue to emit step_ids without the prefix (`"step_1"`); the executor catches this output and rewrites it as `"<sub_query_id>.<original_step_id>"` before the reducer sees it. Handlers stay unaware of the per-sub-query context — they're called with one PlanStep at a time and produce one NodeOutcome. This preserves handler isolation. Aggregator splits on `.` to recover sub_query_id.

### Channel: `final_status` modification — needs verification

Current `final_status: Literal["executed", "clarification_required", "no_match", "failed"]`. Phase 5 introduces a new possible state: **"partial"** — some sub-queries succeeded, some didn't.

This is a **plain field, NOT Annotated** — runbook **Caveat B applies.** Action required: **before adding the new literal, run a Path-B-shape smoke on the plain-field-no-reducer pattern**. Currently UNVERIFIED whether changing the Literal's value set affects restored checkpoints.

**Decision:** Phase 5 first builds the new Annotated channels (R1-compliant) and tests them. Adding "partial" to `final_status` is gated behind the plain-field smoke (Caveat B test) — that's a sub-task within Phase 5, not a Phase 5 prerequisite.

---

## Implementation order (no code yet — for review)

All step acceptance gates route through `print_verdict()`. No code at step N+1 until step N's gate clears.

| # | What | Acceptance gate (specific, falsifiable, verdict-guarded) |
|---|---|---|
| **5.1** | Add new Annotated channels to `OneOpsState`; Path-B-shape smoke per channel (Caveat A) | **Probes**: 4 channels × 4 phases each (v2 persist / restore into v2.1-with-new-channel / read+write through new node / **concurrent-write via 2 parallel Sends writing different keys to the same channel**) = 16 sub-checks. **Invariants**: (a) `state.get("ch")` returns `{}` on restored checkpoint, (b) `state["ch"]` returns `{}` (no KeyError), (c) reducer never called with `left=None`, (d) sequential write through reducer persists, (e) **concurrent writes from parallel Sends merge correctly without race or lost-update** — the central production pattern. **Regression gate**: if any of 4 channels fails any invariant, step 5.1 FAILS and design re-opens (per "if step 5.1 fails" section above). |
| **5.2** | Refactor `rewriter_node` to accept `focus_override` parameter AND skip Branch 1 when called with non-empty `depends_on` | **Probes**: (a) live-graph single-sub-query turn with no `focus_override` set → behavior identical to today (byte-compare `user_response`); (b) unit test calling `rewrite_query(focus_override={"entity_id":"INC0001001","service_id":"incident"})` with active `state.focus` pointing to CHG0004007 → rewriter uses INC override, not CHG live focus; (c) **empty `depends_on` + pronoun** ("close it" with focus INC) → Branch 1 fires, "it" resolves to INC; (d) **non-empty `depends_on` + pronoun** ("find the duplicate and close it" — s2's "it" with `depends_on=["s1"]`) → Branch 1 does NOT fire, "it" stays in the rewritten text for runtime cross-step injection per LBD-6. **Regression gate**: UC-1 sanity 3-probe (summarize INC / field-read on focus / summarize CHG) all `executed` under three_stage with no behavior change vs Phase 4 batch 1 baseline. |
| **5.3** | Build `sub_pipeline_node` that runs rewriter+shortlist+rerank for ONE sub-query | **Probes**: 5 isolated-call unit tests — one each per Phase 4 PROBE class (KB-semantic, summary-direct, field-read-on-focus, conversational-greet, off-domain-Gate-A-fires). **Invariants**: (a) each writes to its per-sub-query channels keyed by `sub_query_id`, (b) `routing_gate_verdict` propagates correctly, (c) Gate A short-circuit still emits `Command(goto="aggregator")` with correct payload. **Regression gate**: 5/5 unit probes pass; channel writes scoped to correct sub_query_id. |
| **5.4** | Replace single-edge `decomposer→rewriter` with fan-out edge `decomposer→[Send(sub_pipeline, …) per sub-query]` | **Probes**: 8 multi-intent live-graph turns — 2-entity, 3-entity, 5-entity, mixed read+action, with focus, without focus, ambiguous (margin gate fires per sub-query), off-domain (Gate A fires per sub-query). **Invariants**: (a) `state.subquery_rewritten` has N entries matching `len(state.subqueries)`, (b) per-sub-query OTEL spans emit `routing.shortlist` AND `routing.rerank` for each sub_query_id, (c) parallel execution observable in span timestamps (overlapping intervals). **Regression gate**: 8/8 probes; UC-1 sanity 3-probe still green. |
| **5.5** | Refactor planner to read `subquery_reranked[sub_id]` and emit per-sub-query PlanSteps with `depends_on` | **Probes**: 3 worked examples — (a) read+read (`summarize INC and CHG`) → 2 parallel PlanSteps, no deps, (b) read+action (`summarize INC and close it if resolved`) → 2 PlanSteps, s2 depends on s1, s2 has `requires_approval=true`, (c) mixed 3-way (`summarize INC, find related KB, check sla breach`) → 3 PlanSteps with correct dep graph. **Invariants**: planner_no_match=false; every PlanStep references an `agent_id` in `_handlers`; `depends_on` IDs are valid sub_query_ids. **Regression gate**: 3/3 worked examples; legacy mode planner output byte-identical to Phase 4. |
| **5.6** | Extend executor wave-based-Send to honor sub-query-level dependencies + cross-step state injection | **Probes**: 4 cases — (a) read+action with condition True (action fires), (b) read+action with condition False (action skipped, reason recorded), (c) read fails (action blocked, reason recorded), (d) read succeeds but missing-field reference (action blocked, reason="depended on missing field"). **Invariants**: (a) waves execute in dependency order (assert via span start-time ordering), (b) cross-step injection writes `{sub_id, field}` resolved value into target PlanStep parameters before execution, (c) skipped/blocked steps emit `NodeOutcome` with explicit status and reason. **Regression gate**: 4/4 probes; no execution out-of-order. |
| **5.7** | `interrupt()` integration for action steps | **Probes**: 3 cases — (a) action approved → executes with correct params, (b) action denied → `step_results[i].status="denied"`, user response acknowledges, (c) interrupted mid-wave → other parallel reads complete; action waits for resume. **Invariants**: (a) `interrupt()` payload contains tool name, target entity, side-effect description, irreversibility note (structured, not free-form), (b) resume via `Command(resume=...)` correctly continues the wave, (c) denied action does NOT execute any side effect. **Regression gate**: 3/3 probes; resume token round-trips cleanly through LangGraph-native format. |
| **5.8** | Aggregator stitching — section-headers + mixed-status disclaimer | **Probes**: 4 cases — (a) single-sub-query turn (NO headers, output byte-identical to today), (b) 3-sub-query all-success (3 headers, no disclaimer), (c) 3-sub-query mixed-success (2 headers, disclaimer naming the failed one), (d) 3-sub-query all-failed (no headers, disclaimer for each). **Invariants**: (a) header rule fires iff `len(subqueries) >= 2`, (b) section text matches `**Part N — <first 60 chars of sub_query_text>**` exactly, (c) disclaimer template matches Phase 4 G-MultiIntent verbatim text including pronoun preservation. **Regression gate**: 4/4 probes; UC-1 single-intent output byte-identical to Phase 4 baseline (no leading header). |
| **5.9** | Partial-failure `error_class` dispatch table | **Probes**: 3 cases — one per V-1 / V-2 / V-3 variant. **Invariants**: (a) `NodeOutcome.error_class` correctly populated, (b) user-visible text matches Surface 5 wording per variant, (c) each user-visible text passes the "user knows next concrete action" test (codified as a separate assertion checking for actionable verbs: "contact", "try again in <N>", "rephrase", "escalate to <named role>"). **Regression gate**: 3/3 probes; each text contains an actionable next step. |
| **5.10** | Plain-field smoke for `final_status` extension (Caveat B); add "partial" literal if smoke passes | **Probes**: 1 plain-field-pattern Path-B-shape smoke. **Invariants**: (a) v2 checkpoint with `final_status="executed"` restored into v2.1 graph that adds `"partial"` to the Literal — does `state.get("final_status")` return `"executed"` or KeyError? (b) does the graph compile with the extended Literal? **Outcome branches**: smoke passes → add "partial" literal + dispatch entry. Smoke fails → `final_status` stays as-is, partial-success keeps using `"executed"` + disclaimer (per Q2 recommendation already). **Regression gate**: smoke result is binary outcome; either branch is acceptable, no third path. |
| **5.11** | G-MultiIntent live-graph re-smoke (closes D7) replaces synthetic-state P5/P6 with real fan-out | **Probes**: Phase 4 batch 1 G-MultiIntent v2 (6 probes) re-run with real fan-out enabled. **Invariants**: G-MultiIntent invariant `disclaimer_present == (len(subqueries) >= 2 AND len(succeeded) < len(subqueries))` holds for ALL 6 probes — both the live-graph and previously-synthetic cases now go through `g.ainvoke()`. **Regression gate**: 6/6 verdict-guard PASS; closes task #29 (D7). |
| **5.12** | UC-1 v12 stress + 30-min soak under three_stage with fan-out enabled (closes P-5) | **Probes**: 232-probe UC-1 v12 stress + 30-min soak at 8s interval (215 turns). **Invariants**: (a) UC-1 stress real-pass = 100% (matches Phase 4 baseline of 224/232 = 100% real-pass after GATE accounting), (b) soak success rate = 100%, (c) p95 latency under three_stage within +20% of legacy mode baseline (legacy p95 = 10.23s; three_stage p95 ≤ 12.28s), (d) memory growth < 50MB over soak (matches Phase 4 soak baseline). **Regression gate**: all 4 invariants must hold; closes P-FLIP prereq #5. |

---

## Known risks (named, not buried)

| Risk | Why | Mitigation |
|---|---|---|
| Concurrent dict-merge race | LangGraph reducer concurrency on `Annotated[dict, merge_dict]` — multiple `Send` payloads writing to the same key | Each `Send` uses its OWN `sub_query_id` as the key; collisions impossible by construction |
| Fan-out × LLM rate limits | N sub-queries → N rerank LLM calls in parallel → may hit OpenAI tier-1 RPM ceiling | Gateway already has `Semaphore(max_concurrent_calls=8)`; verify under load |
| `interrupt()` UX for multi-action turns | Multiple action sub-queries → multiple sequential approvals → user fatigue | Phase 5 builds the mechanism; UX consolidation (batch-approval) is D8 follow-up |
| Aggregator section-header bloat | Single-sub-query turn must NOT get a header | Implementation rule: header only when `len(subqueries) >= 2` |
| Plain-field `final_status` extension | Caveat B — not yet verified | Gated as step 5.10 with its own Path-B smoke |
| Cross-step state injection format | What does "the result of s1" look like to s2? | Step 5.6 defines an explicit `result_ref: {"sub_id": "s1", "field": "status"}` shape; documented in Surface 2 |

---

## Open questions — recommendations with trade-offs and reversibility

Each question carries a **recommended default** + the trade-off if wrong + the
reversibility cost. Accept the recommendation, push back if it's wrong, don't
relitigate if it isn't.

### Q1 — Section-header format

**Recommendation: `**Part N — <text>**`** (bold with em-dash separator).

- **Trade-off**: UC-1's `summarization_agent` currently emits code-fenced
  blocks. Bold-with-dash headers won't break that rendering. Markdown `##`
  headers could cause double-rendering in some chat UIs. No markdown at all
  loses scannability for ≥3-sub-query responses.
- **Reversibility**: Trivial — single-string format constant in the
  aggregator; change with `s/Part /Section /` and re-render. Reversible
  any time without state migration.

### Q2 — Add `final_status="partial"` literal?

**Recommendation: NO** — keep `final_status="executed"` for the partial-success
case and let the G-MultiIntent disclaimer carry the partial signal in
user-facing text.

- **Trade-off**: Schema change touches every caller of the graph's response
  payload — the Bridge service, NATS response subjects, observability
  dashboards, downstream API consumers. The partial signal is already
  computable from `len(succeeded_step_results) < len(state['subqueries'])`,
  which any caller can derive from the existing `step_results` channel.
  The disclaimer text already names what was/wasn't handled.
- **Reversibility**: ASYMMETRIC. Adding the literal later is cheap (one
  Literal value + dispatch table entry). Removing it later is expensive
  (every caller needs updating + migration). Lighter v1 schema is the
  safer default; if real callers ask for the machine-readable signal,
  add it then.

### Q3 — Cross-step state-injection scope for v1

**Recommendation: simple field references only.** The shape is
`{"sub_id": "s1", "field": "status"}` — one field from one prior sub-query's
`NodeOutcome.result`. No JSONPath, no template strings, no expression
evaluation, no conditional logic in the injection itself.

- **Trade-off**: Limits v1 to dependencies where s2 needs ONE field from
  s1. Compound conditions (`s2 if s1.status == "resolved" AND s1.priority == "P1"`)
  defer to v2. Most ITSM dependencies in practice are single-field
  ("close it if status is resolved"; "summarize the parent if linked")
  so this covers ≥90% of expected production cases.
- **Reversibility**: FORWARD-COMPATIBLE. The shape `{sub_id, field}` is a
  subset of any future richer ref language (JSONPath, expression tree).
  Adding JSONPath later doesn't break existing refs. Zero migration cost
  when v2 lands.

### Q4 — `interrupt()` resume token format

**Recommendation: LangGraph-native** (use `Command(resume=...)` with
LangGraph's built-in resume token).

- **Trade-off**: Standard documented pattern, integrates with the existing
  `Command(goto=...)` mechanism used by Gate A and margin disambiguation.
  Custom token format would require us to maintain serialization, UI parsing,
  recovery semantics, and version compatibility — all of which LangGraph
  already does.
- **Reversibility**: Reversible — interrupt payloads are at the graph
  boundary; swapping the token format affects only the resume edge. No
  state migration. If LangGraph's native format limits us later, we add
  a translation layer in the resume node without touching the interrupt
  payload.

---

**Each recommendation stands as the default for implementation unless
explicit pushback with new evidence.** No "we'll figure it out" framings.

---

## What this design does NOT cover

- UC-2 action handler implementation (D8 — separate batch)
- Eval set expansion (D3 — separate workstream)
- Continuous eval logging (D4 — blocked on P-FLIP)
- Decomposer prompt tightening (D2 — blocked on D3)
- Pronoun-in-disclaimer cosmetic (D5)
- **Ambiguity-handling architecture (v2 package, 2026-05-17).** **PHASE 5 IMPLEMENTATION IS GATED ON THE AMBIGUITY FIX LANDING FIRST.** Two adversarial-probe families (Family 3 + Family 1) found 9 distinct silent-failure modes across 15 probes (`docs/findings/family3-ambiguous-referent-2026-05-17.md`, `docs/findings/family1-focus-pivot-2026-05-17.md`). Research-grounded solution package: **`docs/design/ambiguity-fix-package-v2.md`** (canonical; v1 superseded). Key architectural commitment: fan-out without ambiguity infrastructure multiplies the silent-failure surface (N sub-queries each silently picking wrong instead of 1). The package introduces a persistent **entity ledger** state channel (ServiceNow Context Engine pattern), a **verifier model** for confidence calibration, and **per-UC clarification declarations** in UC-as-spec (MAC paper pattern). Estimated 10–16 weeks engineering. **Phase 5 design stands; its implementation waits.**

These deferred items remain deferred. Phase 5 implementing them is scope creep — push back if it appears.

---

## Review questions for the human reviewer (revised 2026-05-17 after Finding 1-5 resolution)

Before any code from this design lands, please confirm:

1. **LBD-1 through LBD-8 architectural decisions** — any of these wrong? (LBD-7 condition schema and LBD-8 legacy preservation are new in this revision.)
2. **Q1–Q4 open-question recommendations** — accept each verbatim or push back.
3. **Finding 1 resolution** — section-header format is `**Part N — text**` (Format A); fence-pairing analysis verifies the produced output renders correctly. Confirmed by 2026-05-17 rendering probe.
4. **Finding 2 resolution** — step 5.2's acceptance gate now includes the empty/non-empty `depends_on` distinction for Branch 1 firing.
5. **Finding 3 resolution** — LBD-7 specifies the `PlanStep.condition` schema; planner produces, executor evaluates.
6. **Finding 4 resolution** — VERIFIED via probe: LangGraph supports per-branch `interrupt()` in Send fan-out. Wave structure design stands.
7. **Finding 5 resolution** — LBD-8 makes legacy preservation explicit: `ROUTING_MODE=legacy` does not wire Phase 5 nodes.
8. **Cross-cutting** — step 5.1 includes concurrent-write smoke; step_id prefix wrap happens at executor, handlers stay unaware.
9. **Implementation order** — 12 steps each with falsifiable gate. Any step worth splitting or merging?

**No code lands until items 1–9 above have explicit human answers.** Same discipline as batch 1: design surface visible, scope statements explicit, prerequisites named.
