# ISS-002: Priority order in classifier prompt not honored — type_word over-weighted

**Trigger:** Messages where multiple expression categories could legitimately
apply, and the principle file's priority order is supposed to decide which
wins. Two confirmed cases (20-case adversarial eval, 2026-05-17, P1.3 and P4.1):

- `"the latest incident"` — ordinal AND type_word both fit. Priority says
  ordinal > type_word. Expected: `expression_type=ordinal`,
  `type_filter_applied=""`.
- `"the other incident"` — negation AND type_word both fit. Priority says
  negation > type_word. Expected: `expression_type=negation`,
  `type_filter_applied=""`.

**Wrong behavior:** Both cases produced 3/3 consistent `expression_type=type_word`
with `type_filter_applied="incident"`. The LLM consistently chose type_word
across runs at temp=0 — stable but principle-violating. Priority order is
explicitly stated in the principle ("`explicit_id > negation > ordinal >
type_word > pronoun > other > no_referent`") and demonstrated in Examples 5
and 6, but neither was sufficient to make the LLM apply it for these
combinations.

**Right behavior:** When multiple categories apply, the priority-order winner
is the classification. The type word's presence in the message text does NOT
override the priority order. `type_filter_applied` is gated on
`expression_type`, not on whether a type word appears in the message.

**Root cause:** **Prompt-level, with two contributing factors:**

1. **Priority order is embedded as one line inside Step 2.** It's stated, but
   not given structural prominence. The classifier reads through the category
   descriptions first; by the time it gets to the priority-order line, it has
   already implicitly selected type_word for type-word-shaped messages.

2. **The type_word category description grew long after Fix 1.** Adding "type
   must match registered service_id, else classify as other" turned a 1-line
   description into a 6-line section. The category-prose imbalance (type_word
   ~6 lines vs others ~1-2 lines each) causes the LLM to over-weight type_word
   as a default classification for ambiguous cases. The peer flagged this risk
   proactively before the eval ran; the data confirmed it.

This is the **same root cause pattern as ISS-001**: structured prompts
don't enforce structured reasoning. Stating a rule doesn't guarantee its
application; the model can read the rule and still choose a different
path if other prompt elements pull it that way.

**Fix:** **(B) Restructure the prompt to give priority order structural
prominence + add counter-examples.** In progress 2026-05-18.

Specific changes:
1. Promote priority order to Step 1 (was embedded in Step 2). New Step 1
   says: "Before classifying, apply this priority check. If multiple
   categories could apply, the priority-order winner is the answer — do
   not default to any category."
2. Demote type_word category description back to parity with other
   categories. Move the "must match registered service_id" guidance to a
   separate principle section so it doesn't bloat the category list.
3. Add three counter-examples demonstrating priority order in action:
   - Example 8: `"the latest incident"` → `ordinal` (NOT type_word)
   - Example 9: `"the other incident"` → `negation` (NOT type_word)
   - Example 10: `"the alert"` (OOV type) → `other`, `type_filter=""`

**Test pinning:**
- `tests/adversarial/eval_adversarial_20cases.py` cases P1.3, P4.1 — these
  must classify as ordinal and negation respectively after the fix.
- Re-run after fix; if still type_word, prompt-only approach has failed
  and a deterministic post-classifier override may be needed (same
  pattern as ISS-003 fix C).

**Status:** partial-fix (2026-05-18 re-run after (B)+(C) landed).

**Verification:**
- P1.3 `"the latest incident"` → 3/3 `ordinal` at temp=0 (was 3/3 `type_word`). FIXED.
- P4.1 `"the other incident"` → 3/3 `negation` at temp=0 (was 3/3 `type_word`). FIXED.

**New regression introduced by the fix:** P1.2 `"summarize INC0001001 — not that one"`
went from passing (`explicit_id`) before (B)+(C) to failing (`negation`) after.
The restructured prompt made negation more prominent and the LLM now over-applies
it against the explicit_id priority. The principle is correct (explicit_id beats
negation per priority order); the LLM isn't following it on this specific
mixed-shape input.

**Next iteration:** add Example 11 demonstrating explicit_id beating negation
using the P1.2 shape. Specifically: `"summarize INC0001001 — not that one"`
→ `explicit_id`, `referring_expression="INC0001001"`. ~5 minutes. Will move
status to `fixed` after a re-run shows P1.2 returns to `explicit_id` AND P1.3 /
P4.1 stay correct.

**Failure mode is safe pending fix:** P1.2 misclassified as negation routes
downstream to clarification (negation + INC as focus → exclude focus →
no candidates → is_unambiguous=False → clarify). Not a silent-wrong-answer.
P1.2 won't ship a wrong answer; it'll ask the user to clarify a weird query.

**Test pinning:**
- `/tmp/eval_adversarial_20cases.py` cases P1.3 ✓, P4.1 ✓, P1.2 ✗ (pending fix)

**Related issues:** ISS-001 (same root-cause pattern: structured prompt
elements don't enforce structured behavior), ISS-003 (same fix package
B+C, similar pattern of LLM ignoring explicit principle text).

**Generalization:** When a prompt has a "decision rule" alongside category
descriptions, the descriptions can override the rule. Either lead with the
rule (structural prominence) or move the rule into code (deterministic
enforcement). Don't expect the LLM to apply a rule that's competing with
inline-prose distractors.
