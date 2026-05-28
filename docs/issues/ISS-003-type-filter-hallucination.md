# ISS-003: Type_filter hallucination on OOV type words — silent-wrong-answer

**Trigger:** User messages containing type-word-shaped phrases for entity
types NOT registered in the system's service catalog. Confirmed cases (20-
case adversarial eval, 2026-05-17, P2.1-P2.4):

- `"the document"` — "document" is not a registered service_id (only
  `incident`, `change`, `problem`, `knowledge` are).
- `"the user"` — same; "user" is not a service_id.
- `"the alert"` — same.
- `"the report"` — same.

**Wrong behavior:** All four cases produced 3/3 consistent
`expression_type=type_word` with hallucinated `type_filter_applied`
values:
- `"the document"` → `type_filter="other"` (the string literal "other", not
  a valid service_id)
- `"the user"` → `type_filter="incident"` (hallucinated)
- `"the alert"` → `type_filter="incident"` (hallucinated)
- `"the report"` → `type_filter="incident"` (hallucinated)

**This is silent-wrong-answer in the exact shape Component 1b was built to
prevent.** The rule engine downstream of the classifier filters active
entities by `service_id=type_filter`. With `type_filter="incident"`, a
message about "the alert" gets resolved to incident-typed entities. The
user gets a confident answer about an incident when they asked about
something else entirely.

**Right behavior:** Classify as `expression_type=other` when the type word
doesn't match a registered service_id. `type_filter_applied=""`. Route to
clarification. The system should ask "what kind of alert?" or "I don't
recognize that type — could you clarify?" rather than silently mapping
it to incident.

**Root cause:** **Two layers — prompt-level AND model-level.**

1. **Prompt-level (same as ISS-002):** The principle's Fix 1 directive
   added 2026-05-18 says "If the type word does NOT match a registered
   service_id, classify as `other`." The LLM ignored this. The added
   text didn't bind because the type_word category description grew long
   and the new directive sat alongside other category prose rather than
   leading the decision.

2. **Model-level: hallucination of a closed-class field.** Even with
   tighter prompting, the LLM will sometimes produce `type_filter`
   values that aren't in the registered set. This is a categorical
   failure mode for LLMs on enum-typed fields: they hallucinate
   plausible-looking values rather than refusing to fill the field. No
   amount of prompt tuning eliminates this entirely; it can only reduce
   the rate.

**Fix:** **(B) prompt restructure + (C) deterministic post-classifier
validation. Defense in depth.** In progress 2026-05-18.

(B) prompt-level — same package as ISS-002:
- Lead with priority order, demote category prose to parity
- Add Example 10: `"the alert"` → `other`, `type_filter=""` — explicit
  demonstration of the OOV path

(C) code-level — `_classify()` in `src/oneops/routing/verifier.py`:
- After parsing the LLM response into `RewriteClassification`, run a
  Python check:
  ```python
  _VALID_SERVICE_IDS = frozenset(_registered_prefix_map().values())
  if (classification.expression_type == "type_word"
          and classification.type_filter_applied
          and classification.type_filter_applied not in _VALID_SERVICE_IDS):
      # Hallucinated type_filter. Override to other; log warning.
      _logger.warning("verifier.classifier.override", ...)
      classification = RewriteClassification(
          referring_expression=classification.referring_expression,
          expression_type="other",
          type_filter_applied="",
          model_reasoning=f"[overridden] LLM produced type_filter="
                          f"{classification.type_filter_applied!r} not in "
                          f"registered service_ids. Original reasoning: "
                          f"{classification.model_reasoning}",
      )
  ```

(C) is defense-in-depth, not prompt overreach. It enforces a contract
the principle already states. Architectural rationale matches ISS-001's
two-pass split: deterministic where the class is closed (the set of
registered service_ids is finite and known), LLM where semantic judgment
is required (which category the expression belongs to).

**Test pinning:**
- `tests/adversarial/eval_adversarial_20cases.py` cases P2.1-P2.4 — must
  produce `expression_type=other`, `type_filter=""` after fix.
- Will add a `tests/unit/test_verifier_classifier_override.py` unit test
  for the (C) override: hand-construct a `RewriteClassification` with
  hallucinated `type_filter`, verify it gets overridden to `other`.

**Status:** fixed (2026-05-18).

**Verification:**
1. ✓ 20-case adversarial re-run: P2.1-P2.4 all classify as `expression_type=other`
   at 3/3 consistency. Previously: 3/3 `type_word` with hallucinated `type_filter`
   values (`"alert"` → `"incident"`, `"user"` → `"incident"`, etc.).
2. ✓ Unit tests for the override path: `tests/unit/test_verifier_classifier_override.py`
   covers (a) override fires on hallucinated type_filter, (b) does NOT fire on
   valid type_filter, (c) does NOT fire on non-type_word classifications, (d)
   does NOT fire on empty type_filter. All 4 tests pass.
3. ✓ Audit log shape verified: `verifier.classifier.override` warning fires
   with original_expression_type, original_type_filter, override_to, reason,
   original_message — preserving the full trail for debugging.

**Note on which fix caught what:** the prompt-level (B) restructure alone
appears to have caught all four P2 cases — the LLM now classifies them as
`other` upstream and the (C) override does not fire. The override is still
valuable as defense-in-depth: when prompt drift, model upgrades, or unusual
inputs eventually produce a hallucinated `type_filter`, (C) catches it
deterministically. The override's value is preserved because the unit tests
exercise its specific case directly, independent of LLM behavior.

**Test pinning:**
- `/tmp/eval_adversarial_20cases.py` cases P2.1, P2.2, P2.3, P2.4 — all 3/3 `other`
- `tests/unit/test_verifier_classifier_override.py` — 4 tests covering override boundary cases

**Related issues:** ISS-001 (architectural pattern: deterministic where
closed-class, LLM where judgment), ISS-002 (same prompt-level root cause +
same fix package B).

**Generalization:** For ANY closed-class field the LLM fills (enum types,
known-set values), add a deterministic post-validation check. Prompt
tightening reduces hallucination rate but doesn't eliminate it. The cost
of a Python set-membership check is microseconds; the cost of a
hallucinated value reaching production is potentially catastrophic.

This is the **defense-in-depth pattern**: prompts express intent, code
enforces invariants. Either alone is insufficient; together they
collapse the silent-wrong-answer surface to near-zero.
