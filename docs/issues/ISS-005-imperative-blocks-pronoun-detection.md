# ISS-005: Imperative clause form blocks pronoun classification

**Trigger:** User messages where a pronoun appears inside an imperative
clause (`"summarize them"`, `"close it"`, `"check this"`) rather than an
interrogative or declarative one. Confirmed case (20-case adversarial
eval, 2026-05-17, P5.1):

- `"summarize them"` with ledger containing INC + CHG (both active),
  focus on CHG.

Unchanged across pre- and post-(B)+(C) runs, so this is independent of
the priority-order / type_filter fixes — separate failure mode.

**Wrong behavior:** Classified as `expression_type=no_referent` at 3/3
consistency. The word `"them"` IS explicitly listed in the principle's
pronoun list (`"it"`, `"its"`, `"they"`, `"them"`, `"their"`, `"this"`,
`"that"`). The LLM read the principle, saw "them" in the pronoun list,
but still chose `no_referent`.

The pre-(B)+(C) eval showed this same behavior. The (B) restructure
didn't change it. So it isn't a priority-order interaction — it's
something about the *clause type*.

**Right behavior:** Classify as `expression_type=pronoun` with
`referring_expression="them"`. The rule engine then computes
`candidates_before_filter` = all active entities, `valid_candidates`
= [INC, CHG] (both active), `is_unambiguous=False` (2 candidates), and
routes to clarification. End-user experience: "did you mean INC0001001
or CHG0004007?" — the right question.

Today's behavior — `no_referent` — also routes to clarification but
LOSES the information that the user used a pronoun. The clarification
text Component 3 will build is less useful: it can't say "you said
'them' but I'm not sure which" — it can only say "I didn't understand."

**Root cause:** **Model-level interaction with clause form.** The LLM
appears to apply "category logic" selectively based on sentence
structure:
- Interrogative pronouns (`"what about it?"`, `"who are they?"`) → classified as pronoun ✓
- Declarative pronouns (`"it is open"`) → mostly classified as pronoun ✓
- **Imperative-pronouns** (`"summarize them"`, `"close it"`) → classified as no_referent ✗

Possible mechanism: the principle's pronoun list is presented with
example phrases that read as questions or fragments. The LLM may
treat the pronoun category as "applicable when the user is asking
about something" rather than "applicable whenever a pronoun token
appears." Imperatives feel like commands-about-already-known-things,
which doesn't fit the question-asking frame the example pronouns suggest.

This is NOT a closed-class vs open-class problem (ISS-004's pattern).
The word "them" IS in the catalog. The LLM still doesn't apply the
category. This is closer to ISS-001's pattern: the LLM reads the rule
but applies it selectively based on context the rule didn't anticipate.

**Fix:** **Two paths, deferred — accept v1 behavior pending production data.**

Path 1 (principle-level): Reframe the pronoun category to be clause-agnostic.
Current text: "Bare anaphor with no type-narrowing." Add: "Applies in
ALL clause types: interrogative ('what about it?'), declarative ('it is
open'), AND imperative ('summarize them', 'close it'). The presence of
a pronoun token in any clause classifies as pronoun."

Path 2 (defense-in-depth — same pattern as ISS-003's (C) override):
Deterministic post-classifier check. If `expression_type=no_referent`
AND the message contains a token from the closed pronoun list
(`it`, `its`, `they`, `them`, `their`, `this`, `that`), override to
`expression_type=pronoun`. Closed-class pronouns are finite and known;
this is the same pattern as the type_filter override.

Path 2 is more reliable but adds another override path. Path 1 is the
cleaner fix but risks the same over-weighting that ISS-002 produced
(longer pronoun description → other categories degrade).

**Test pinning:**
- `/tmp/eval_adversarial_20cases.py` case P5.1 (`"summarize them"`) —
  currently 3/3 `no_referent`, target `pronoun`.
- If Path 2 lands: `tests/unit/test_verifier_classifier_override.py`
  would add an override test for the pronoun-in-imperative case.

**Status:** deferred (2026-05-17). P5.1 routes to clarification — safe
failure mode. Re-prioritize when:
- UC-2 actions ship (D8) — imperative pronouns become common ("close it",
  "assign them to me") and the misclassification gets worse user impact.
- Component 3's clarification quality work begins — pronoun-aware
  clarification text needs the correct classification upstream.

**Related issues:** ISS-001 (architectural pattern: LLM reads rule,
applies selectively), ISS-003 (Path 2 fix uses the same defense-in-depth
override pattern), ISS-004 (similar "principle says X, LLM does Y" shape
but different root cause — ordinal is a coverage gap, pronoun is an
application gap).

**Generalization:** LLM classifiers apply "category logic" selectively
based on sentence structure (imperative vs. interrogative vs. declarative).
A category's rule can be explicitly stated and still miss certain clause
types if the example phrases all share one form. **When designing
principle categories, ensure example phrases span all clause types where
the category should apply.** Alternatively, deterministic post-classifier
checks for closed-class members (pronouns, negation tokens) can enforce
the rule mechanically — the same defense-in-depth pattern ISS-003
established for closed-class field values.
