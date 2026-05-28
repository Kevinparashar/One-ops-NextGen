# Family 1 — Focus Pivot Mid-Conversation findings (2026-05-17)

**Harness:** `/tmp/family1_focus_pivot.py`
**Routing mode:** `three_stage`
**Result:** 4/7 survived. 3/7 failed silently. **1 additional latent bug surfaced (P2 clarification text).**

## Findings

### New Gap 8 — Ordinal references not supported
**Evidence:** P1 — `"summarize the first one again"` after mentioning INC then CHG. Rewriter substituted current focus (CHG) and re-summarized. The word "first" carried zero meaning to the rewriter.
**Architectural implication:** Ordinal expressions ("the first", "the second", "the previous one", "the earlier ticket") are entirely absent from the rewriter's vocabulary of referring expressions. Closed-class linguistic feature, easy to add detection for.

### New Gap 9 — Type-disambiguation is fragile, not absent
**Evidence:**
- P2 `"what was the SLA on the incident?"` after INC + CHG → Gate A fired `ambiguous` (correctly detected ambiguity)
- P5 `"the incident's owner?"` after INC + KB → correctly resolved to INC ✓
- **P4 `"and the change?"` after INC + CHG + PBM → collapsed to current focus PBM (not CHG)** ✗

**Inconsistency:** the same construction works in 2-entity conversations but fails in 3-entity conversations. The system *has* the capability (P5 proves it) but doesn't apply it reliably. Likely cause: the LLM-based rewriter loses track of older entities once 3+ are in history, or the bare-attribute branch (which P4 took) doesn't consult the same disambiguation logic as the pronoun branch (which P5 took).

### Extension of Family 3 Gap 4 — Three-entity silent pick
**Evidence:** P6 — `"what's its priority?"` with INC + CHG + PBM in history → silently picked PBM (most recent). Family 3 P4 already showed this for 2 entities; P6 extends it to 3. **Each additional entity multiplies wrong-answer surface.**

### New latent bug — Clarification text generator emits identical options
**Evidence:** P2 — Gate A correctly fired `ambiguous` and asked for clarification, BUT the question rendered as:
> *"I see two ways to help here — did you want uc01_summarization or uc01_summarization? Could you confirm which?"*

Both options resolved to the same UC (`uc01_summarization`) because the rewriter detected two valid candidates (INC and CHG) but they routed to the same handler. The clarification-text builder didn't distinguish "two candidates of same UC type" from "two different UCs" — and produced an option-list of one option repeated.

**Implication:** When Family 3's recommended `clarification_node` gets built, its text generator must distinguish "UC ambiguity" from "entity ambiguity" — and for entity ambiguity within one UC, list the entity IDs, not the UC names.

## What worked (also worth recording)

- **P3** — explicit re-mention re-pivots focus. `"INC0001001 again — what's its status?"` correctly answered INC's status even after CHG had been the focus. The decomposer/rewriter recognize explicit entity IDs in the message and update focus accordingly.
- **P5** — type-disambiguation `"the incident's"` correctly resolves after a UC-3 detour. The bare-attribute branch handled the type word as a soft anchor.
- **P7** — focus across off-domain interjection. INC focus held through `"what's the weather in NYC?"` (which fired Gate A clarification but didn't clear focus). On the next turn, `"what about it?"` correctly substituted INC.

**Net architectural read:** the system has partial capability for cross-turn entity tracking. The failures are in specific places (ordinals, 3+ entity disambiguation, multi-antecedent), not in the basic mechanism. This makes the fix more targeted than a full rewrite.

## Mapping to the Family 3 ambiguity-fix package

These findings extend, not replace, the package in `docs/design/ambiguity-fix-package.md`:

| Finding | Component in fix package |
|---|---|
| Gap 8 (ordinals) | Component 2 — add a deterministic ordinal-token detector (closed lexical class: "first", "second", "third", "previous", "earlier", "original"). When detected, the rewriter must consult the *list of mentioned entities*, not just current focus. |
| Gap 9 (fragile type-disambig) | Component 2 — make the type-disambiguation logic explicit and consistent across the pronoun branch and the bare-attribute branch (P5 vs P4 inconsistency). Both should call the same entity-search routine that filters by service type. |
| Extended Gap 4 (3+ entity ambiguity) | Component 4 — conditional edge must count valid antecedents, not just check "is focus set." Trigger clarification when N>1 candidates regardless of N. |
| Clarification-text generator bug | Component 3 — clarification builder must distinguish UC-level ambiguity from entity-level ambiguity. Two candidates of same UC should produce *"INC0001001 or CHG0004007?"*, not *"uc01_summarization or uc01_summarization?"*. |

## Combined evidence for the engineering reviewer

Family 1 + Family 3 together: **2 families, 15 total probes, 9 silent-failure modes documented.** All converge on the same root architectural commitment: *routing decisions live in conditional edges, not in handler code, with a missing third option (clarify-instead-of-execute) added.*

Two families also produces a useful signal that one alone doesn't: the system has *partial* capability (P3, P5, P7 survived) — meaning the fix is targeted refactoring of existing logic, not invention of new infrastructure. That's a more tractable conversation with the reviewer than "the whole routing layer is broken."
