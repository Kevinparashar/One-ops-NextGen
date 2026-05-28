# Family 3 — Ambiguous Referent adversarial findings (2026-05-17)

**Harness:** `/tmp/family3_ambiguous_referent.py`
**Routing mode:** `three_stage`
**Result:** 2/8 genuinely survived. 6/8 exposed silent-failure bugs.

## Findings

### Gap 1 — Gate A only catches cold-start ambiguity
**Evidence:** P1 `"do the thing"` and P2 `"yes do it"` fired Gate A's `low_confidence` and returned clarification. ✓
**Anti-evidence:** Every other probe had focus present, so confidence went high and Gate A passed even when the rewrite was semantically wrong. Gate A does not gate on context-relative ambiguity.

### Gap 2 — Rewriter Branch 1 substitutes pronouns without antecedent validation
**Evidence:** P3, P4, P5, P6 all show the rewriter substituting current focus into the pronoun slot regardless of validity.
- P3: substituted INC despite verbal topic-closure ("thanks")
- P4: substituted current focus (CHG) without flagging that INC was an equally valid antecedent
- P5: substituted INC even though two recent entities (INC + KB result) were both candidates for "the one I mentioned"
- P6: substituted INC for plural "them" that referred to comments, not the entity → produced `"who wrote INC0001001?"` → field-resolver returned out-of-scope refusal

### Gap 3 — No topic-closure signal handling
**Evidence:** P3 — user said "thanks" between summary and the next message. State carried focus forward; rewriter treated "summarize that" as if "thanks" never happened.

### Gap 4 — No multi-antecedent disambiguation path
**Evidence:** P4, P5 — when two entities are equally valid referents, system silently picks one (typically most-recent focus). No "two valid referents → ask" path exists.

### Gap 5 — Plural pronoun + singular focus collision
**Evidence:** P6 — `"who wrote them?"` referring to comments in the previous summary. Rewriter substituted singular INC for plural "them", producing nonsense. Then the wrong rewrite caused field-resolver to refuse a legitimate commenters question.

### Gap 6 — Negation-of-focus not recognized
**Evidence:** P8 — `"not that one — the other"` — rewriter passthrough preserved the message, but UC-1 still executed against ambient focus and returned the full focus summary. The user explicitly rejected focus; the system gave focus.

### Gap 7 — Passthrough doesn't equal safety
**Evidence:** P7 (`"more"`) and P8 (`"not that one — the other"`). Even when rewriter correctly leaves a message unchanged, the downstream graph still executes against ambient focus. Today's binary is:
- rewrite-substitute-and-execute, OR
- rewrite-passthrough-and-execute

The missing third option is **rewrite-uncertain-and-clarify**. Today's system has no path that ends in clarification when context is ambiguous but not absent.

## Common root cause

Most of these gaps converge on one architectural decision: **focus is interpreted as a hard binding, not a soft candidate.** Once focus exists, the rewriter treats it as the authoritative antecedent for any pronoun, and the executor treats it as the authoritative target for any ambient-focus turn. There's no "focus is a candidate; if uncertain, ask" path.

## Implication for P-FLIP

Today's `three_stage` mode behaves as-tested. Flipping `ROUTING_MODE=three_stage` to production default without addressing at least Gaps 1, 2, and 7 risks shipping confident wrong answers on a class of real-user queries that don't appear in regression tests because regression tests use well-formed inputs.

These gaps should be:
- Added as P-FLIP prerequisites alongside P-1 through P-5, OR
- Explicitly accepted as known-issues with a remediation plan, OR
- Fixed before P-FLIP

Recommendation: **add a new prerequisite P-6 — "Ambiguous-referent gaps (Family 3 findings) have been triaged: each gap is either fixed, documented as accepted-risk, or has an owner + ETA."**
