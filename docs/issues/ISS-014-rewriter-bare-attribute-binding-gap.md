# ISS-014: Rewriter does not always bind bare-attribute follow-ups to focus

**Trigger:** Probe 2 `p2-f1` (2026-05-18) — `"Who is the assignee?"` after `"Tell me about INC0001001"`. Expected: rewriter binds `"the assignee"` to the active focus and emits `"Who is the assignee of INC0001001?"`. Actual: rewriter returned a passthrough or partial binding, the shortlist produced no candidates, and the routing graph emitted `final_status=clarification_required` with an *empty* `gate_verdict` (failure occurred upstream of every gate).

**Wrong behavior:** Bare-attribute follow-ups with no pronoun token (`"Who is the assignee?"`, `"Where was it created"`, `"By when is it due?"`) do not reliably trigger the rewriter's bare-attribute branch when the attribute noun isn't a registered field-name keyword. The rewriter has a deterministic pronoun-substitution path (handles `it / its / they / them`) and an LLM-driven bare-attribute path (handles attribute-shaped follow-ups against focus). The latter is what should fire here; in `p2-f1` it didn't.

**Right behavior:** When focus is active and the message is a question whose attribute noun is plausibly a field of the focused entity, the rewriter must bind to focus — by entity injection (`"the assignee" → "the assignee of INC0001001"`) or by full rewrite (`"Who is the assignee of INC0001001?"`). Empty rewrites that leave focus unbound on bare-attribute follow-ups are wrong; the downstream routing layer cannot recover without a focus reference in the message text.

**Root cause:** **Rewriter's bare-attribute branch is under-triggered.** The LLM-driven branch decides whether to bind by reading the focus state and the message. When the attribute noun isn't a familiar field-name keyword (`assignee` vs `priority` / `status` — the latter appear in many examples; the former less so), the LLM fails to recognize the bare-attribute shape and falls through to passthrough. The rewriter then emits the user's message unchanged, downstream stages see no entity, retrieval can't find a UC, and the gate verdict comes out empty because the failure is upstream of every gate.

This is **upstream of routing** — the rewriter is stage -1, before verifier / classifier / shortlister / rerank. The architectural migration (`docs/design/routing-layer-architectural-review.md`) operates on Stages 1-5; it does not address the rewriter's binding gap.

**Fix:** **Not in scope for the C+E architectural migration.** Files as a separate ticket for targeted rewriter work. Two paths to consider when this is picked up:

- **Path 1 — broaden the rewriter's bare-attribute principle.** Lead with the structural rule: "if focus is active and the message is interrogative and contains no entity reference, bind to focus." Move attribute-noun examples to a non-exhaustive list. Reduces the LLM's reliance on keyword matching. Same shape as ISS-002's prompt-restructure fix.
- **Path 2 — deterministic post-rewriter override.** If focus is active, the message is interrogative, and no entity token survives the rewrite, the rule engine injects the focus entity. Same defense-in-depth pattern as ISS-003's closed-class override. Pairs with Path 1.

Decision deferred to a separate sprint. The architectural migration may *reduce* the symptom incidentally (intent classification might route `"Who is the assignee?"` directly to `field_read` via the intent class, bypassing some of the retrieval-binding problem), but the rewriter's bare-attribute branch still needs the dedicated fix for handler delivery and audit clarity.

**Test pinning:** Probe 2 `p2-f1` is the deterministic reproducer:
- Pre-fix: `"Who is the assignee?"` (T2 with `INC0001001` in focus) returns `clarification_required`, empty `gate_verdict`, rewriter `branch=passthrough` or `changed=False`.
- Post-fix: rewriter emits a focus-bound rewrite; downstream routing executes the field-read.

When the fix lands, the test surface should also include:
- Bare-attribute follow-ups across the full UC-1 field catalog (assignee, due-date, requester, location, parent-problem, etc.) — not just `priority` / `status` which currently pass.
- Mixed cases: bare-attribute question with stale focus, bare-attribute question with no focus (must remain clarify), bare-attribute question with multiple potential antecedents in the ledger.

**Status:** **active** — repro available (Probe 2 `p2-f1`); **out of scope for the C+E architectural migration**. Will need its own sprint allocation; not blocked on the migration, not unblocked by it.

**Related issues:**
- **ISS-008** — adjacent rewriter contract issue (planner over-routes when rewriter makes the rewrite entity-dominant). ISS-008 fires when rewriter binds *too aggressively*; ISS-014 fires when rewriter binds *not enough*. Both are rewriter-contract issues.
- **ISS-013** — also a Probe 2 trigger, but routing-layer scoped (closes structurally by C+E migration). ISS-014 is rewriter-layer, separate.

**Generalization:** **The rewriter's binding decision must be structural (focus active + interrogative + no entity token → bind), not keyword-driven (attribute noun in known list → bind).** The current LLM-driven bare-attribute branch relies on the model recognizing attribute-shaped phrasings, which is brittle to vocabulary variance — the same Property-3 asymmetry (finite enumeration vs infinite phrasing surface) that affects the retrieval layer.

The fix discipline is the same as the architectural migration's: structural property + closed-class override beats prompt-only LLM judgment for binding decisions.

**Link to architectural commitment:** `docs/design/routing-layer-architectural-review.md`. ISS-014 is named in section (d) Slice 1 as a separate ticket explicitly *not* closed by the migration.
