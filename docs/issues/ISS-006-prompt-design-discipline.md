# ISS-006: Prompt-design discipline — three rules for LLM-call prompts

**Trigger:** Pattern recognition across ISS-002 and ISS-003 + deep
literature search (2026-05-18). Three independent failure modes in LLM
classification prompts share a common shape: the prompt looks correct
in isolation but produces wrong outputs in production. Each failure has
a documented research backing AND was empirically reproduced in this
session.

**Wrong behavior (general):** LLM classifiers produce wrong outputs when:
1. Category descriptions are length-imbalanced — longer descriptions dominate (ISS-002 type_word over-weighting).
2. Categories are described in isolation without contrastive examples — the LLM cannot distinguish boundary cases (ISS-002 priority-order failures, ISS-003 OOV type filter).
3. The instinct to add more prompt text when classification fails — paradoxically degrades reliability (today's E3 rescue attempt).

**Right behavior (general):** Treat prompt design as constrained engineering, not creative writing. Three discipline rules below.

**Root cause:** **Documented LLM behavior, not project-specific.** Each
failure mode has a name and a paper:
1. **Label length bias** — labels with longer descriptions are over-selected. Documented in *Mitigating Label Biases for In-context Learning* (arxiv 2305.19148).
2. **Contrastive prompting beats positive-only prompting** for category boundary cases. Documented in *Contrastive In-Context Learning* research.
3. **More detailed prompts can degrade reliability.** Documented in instruction-following papers: "more detailed prompt design, particularly with those requiring explanations and proposed corrections, leads to higher misjudgment rates."

These aren't observations from one session — they're consensus findings across the literature. We empirically reproduced all three this week without knowing the names. The names matter because they let future maintainers find the research.

**Fix:** Three discipline rules to apply on every new LLM-call prompt in this codebase:

---

## Rule 1 — Keep category descriptions parallel in length

When a classification prompt has multiple categories (pronoun, type_word,
ordinal, etc.), every category's description must be roughly the same
length. Asymmetry creates label length bias: the LLM over-selects the
category with the longest description.

**Reference:** Mitigating Label Biases for In-context Learning (arxiv 2305.19148) — "Labels of different lengths are treated inconsistently, even after standard length normalization."

**Practical test:** count lines per category. If one category is 3x
longer than others, collapse it. Move clarifying details to a separate
section ("Important rules" / "Edge cases") that applies across categories,
not into one category's description.

**This was the root cause of ISS-002.** The type_word category description
grew to 6 lines after Fix 1 (OOV-types directive). The LLM over-applied
type_word as a default. The fix in the (B) refactor collapsed it back to
parity and moved the OOV-handling directive to Step 3 instead.

---

## Rule 2 — Use contrastive examples for category boundaries

When two categories could legitimately apply (priority-order cases,
overlapping shape combinations), include explicit examples showing one
winning over the other. Pure positive examples ("Example 1: type_word
case") are insufficient — the LLM needs to see the *contrast* to apply
boundary logic.

**Reference:** Contrastive In-Context Learning research — "Contrastive examples provide positive examples that illustrate the true intent, along with negative examples that show what characteristics we want LLMs to avoid."

**Practical pattern:**
- For each priority-order pair (A > B), include one example demonstrating A winning when both apply.
- For each "DO NOT do this" failure mode, include a negative example with a "WRONG / CORRECT" split (your Example 5's "DO NOT REPRODUCE" pattern).
- Don't just state the rule in prose — anchor it with a concrete example.

**This was the fix in (B)+(C) for ISS-002 / ISS-003.** Examples 8 (ordinal beats type_word), 9 (negation beats type_word), 10 (OOV → other) demonstrated priority order in action. Result: 13/13 ambiguous adversarial cases match prediction at 3/3 consistency.

---

## Rule 3 — Resist adding more text when classification fails

When a classification result is wrong, the instinct is to add more prompt
text explaining the rule more clearly. **This often makes things worse.**
The research shows more detail can degrade instruction-following.

**Reference:** "More detailed prompt design, particularly with those requiring explanations and proposed corrections, leads to higher misjudgment rates." (arxiv instruction-following research)

**Practical pattern when a classification is wrong:**
1. First check Rule 1 (is the category description length-imbalanced?)
2. Then check Rule 2 (are there contrastive examples for this boundary?)
3. Only if both 1 and 2 are clean, consider adding prose — and add minimum text.
4. If a closed-class field is being hallucinated, prefer the (C) override pattern (ISS-003): deterministic post-validation in Python, not more prompt text.

**Anti-pattern:** keep adding clarifying sentences each time a case fails. The prompt becomes a phrase catalog (violates `feedback_descriptions_principle_not_phrases.md` from memory), grows long enough to trigger label length bias on its own category, and develops non-local interactions where fixing case A breaks case B.

**This was caught proactively during the (B) restructure** — when the type_word description was about to be padded with more text to handle OOV cases, we collapsed it instead and moved the directive to Step 3. ISS-003's defense-in-depth override (C) was the alternative when even tighter prompting wouldn't be enough.

---

**Test pinning:** No direct test pins this issue — it's meta-discipline
that informs how Components 3 / 5 / 7 prompts will be designed. The proof
is in subsequent components NOT recapitulating ISS-002 / ISS-003 / E3
patterns when their prompts ship.

**Status:** active discipline (2026-05-18). Apply on every LLM-call prompt going forward. Reference this issue in design docs when writing new prompts.

**Related issues:** ISS-001 (architectural — rule application in code, not prompt; Rule 3 here is the prompt-engineering counterpart), ISS-002 (label-length-bias case), ISS-003 (defense-in-depth as Rule 3's escape hatch when prompting won't fully bind).

**Generalization:** Prompt design is constrained engineering with documented failure modes, not free-form writing. The three rules above are the practical distillation of a much larger research literature on instruction-following, in-context learning, and LLM calibration. Treat the rules as load-bearing: they will recur in Components 3 / 5 / 7's prompts. Document the rule that applies whenever a new prompt or prompt edit is reviewed.

**External references (for future maintainers):**
- Mitigating Label Biases for In-context Learning — https://arxiv.org/abs/2305.19148
- Rule Maker Pattern — https://tessl.io/blog/the-rule-maker-pattern/
- OpenAI Instruction Hierarchy — https://arxiv.org/abs/2404.13208
- MA-DST (multi-domain DST architecture, relevant to D15 1000-UC scaling) — search "multi-attention dialog state tracking"
- MAC (Multi-Agent Clarification) — referenced in `docs/design/ambiguity-fix-package-v2.md`
