# ISS-001: Fake chain-of-thought — structured fields look walked but aren't

**Trigger:** Verifier prompt designed as a structured CoT walk through a
9-field Pydantic schema. The schema's intermediate fields
(`candidates_before_filter`, `candidates_after_filter`, `valid_candidates`)
appear to enforce a step-by-step walk because the model has to fill them
in. The hypothesis: forcing structured intermediate fields forces the LLM
to apply rules step-by-step rather than jumping to a verdict.

**Wrong behavior:** Diagnostic A (2026-05-17) on Family 3 P2 case
(`"not that one — the other"` with single-entity ledger, focus excluded by
negation rule). Pre-edit prompt produced 3/3 identical responses where:
- `expression_type = "negation"` (correctly classified)
- `candidates_before_filter = ["INC0001001"]` (rule says EXCEPT focus, so should be `[]`)
- `valid_candidates = ["INC0001001"]` (back-filled to match)
- `is_unambiguous = true`, `rewrite_matches_intent = true` → silently picks focus

The model classified correctly, then **back-filled the intermediate fields
against a pre-formed conclusion** ("user said negation; only INC in ledger;
INC is the answer") instead of applying the negation rule (exclude focus →
empty). The structured CoT was a decoration on a single integrated
judgment, not a walk.

**Right behavior:** When `expression_type=negation` and only the focus is
in the ledger, `candidates_before_filter` must be `[]` (focus excluded).
`valid_candidates` must be `[]`. `is_unambiguous` must be False
(`len(valid_candidates) != 1`). Routing must clarify.

**Root cause:** **Structural — schema-CoT-through-Pydantic does not enforce
the walk.** The LLM can fill in fields in any order, and when its
high-confidence verdict differs from the rule-walk outcome, it back-fills
intermediate fields against the verdict. Forcing field-by-field output in
a single LLM call doesn't force field-by-field reasoning.

This is a distinct failure mode from "prompt is unclear" or "LLM is
inconsistent." The rule was explicit; the model was consistent; the model
just didn't apply the rule because the architecture didn't require it to.

**Fix:** Two-pass architecture (Component 1b refactor 2026-05-17). LLM
classifies only (Pass 1, produces `RewriteClassification` with 4 fields:
referring_expression, expression_type, type_filter_applied,
model_reasoning). Python rule engine applies the walk (Pass 2, in
`src/oneops/routing/verifier_rules.py`). The LLM cannot back-fill against
a conclusion because the schema has no conclusion field for it to back-fill
toward. The rule engine produces the conclusion deterministically.

Code shipped:
- `src/oneops/routing/verifier_rules.py` — 8 rule functions
- `src/oneops/routing/verifier.py` — orchestration
- `src/oneops/routing/prompts/verifier_classifier.md` — classification-only principle

**Test pinning:**
- `tests/unit/test_verifier_rules.py` — 80 unit tests covering every rule function
- `tests/unit/test_verifier_rules.py::TestApplyRulesIntegration::test_P2_negation_single_entity` — the specific canary
- `tests/adversarial/step_1b_a_classifier_integration.py::[C]` — end-to-end on real LLM

**Status:** fixed (2026-05-17). All 80 rule-engine tests pass deterministically; integration test confirms negation case routes to clarify end-to-end.

**Related issues:** ISS-002 (priority-order-not-honored), ISS-003 (type-filter-hallucination) — both demonstrate the broader pattern: structured prompts don't enforce structured reasoning; the LLM can ignore stated rules. The fix pattern is the same: when a rule must be applied reliably, apply it in code, not in a prompt.

**Generalization:** Whenever a system relies on the LLM "following a walk"
or "applying a rule," ask: can the LLM produce schema-compliant output that
nevertheless violates the rule? If yes, the rule belongs in code, not in
the prompt. The LLM should classify; deterministic code should apply.

**Literature anchor:** This failure mode is recognized in industry as the
rationale for the **Rule Maker Pattern** (Tessl.io 2025, "The Rule Maker
Pattern: Beyond AI Bots and Tools"). The pattern's stated rationale matches
this issue's root cause verbatim: *"When you run the same LLM prompt twice,
you might get different results. That's fine for creative writing, but
catastrophic for updating a production database or modifying critical
infrastructure. By using AI to generate rules that execute
deterministically, you get the same result every time."* The two-pass
architecture in `verifier.py` + `verifier_rules.py` is a direct
implementation of this pattern. Future maintainers reading this should
understand the architecture isn't idiosyncratic — it's a named, validated
industry approach. Reference: https://tessl.io/blog/the-rule-maker-pattern/

Related literature: OpenAI's **instruction hierarchy** paper
(arxiv 2404.13208) describes the same class of failure at the
fine-tuning level — LLMs treat instructions of different priorities as
equivalent. Our production-grade fix (deterministic rules + contrastive
examples + closed-class overrides per ISS-006) is the practical
equivalent of what OpenAI's research-grade fix achieves via fine-tuning.
