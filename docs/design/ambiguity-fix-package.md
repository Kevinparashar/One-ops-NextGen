# Solving the ambiguity-handling problem in the OneOps router

**Status:** Engineering handoff package. Drafted 2026-05-17 from Family 3 findings. Awaiting engineering reviewer.

---

## The load-bearing architectural commitment

> **Routing decisions live in conditional edges, not in handler code.**

Handlers (UC-1, UC-3, future UCs) only execute when called. The routing layer makes every clarification decision. UC authors must never need to reason about ambiguity handling. **This is the line that prevents the Family 3 gaps from recurring at 1000 UCs. If reviewer pushes back on anything, push back hardest on this principle.**

---

## Context

OneOps is a multi-tenant ITSM query router built on LangGraph. Today it routes user queries to use-case handlers (UC-1 summarization, UC-3 KB lookup, UC-99 conversational). Phase 5 design (multi-part query fan-out) is in `docs/design/phase-5-fan-out.md`, awaiting review.

Adversarial testing 2026-05-17 (probes in `/tmp/family3_ambiguous_referent.py`, findings in `docs/findings/family3-ambiguous-referent-2026-05-17.md`) found six architectural gaps in ambiguity handling. **2 of 8 probes survived; 6 of 8 produced confident-wrong answers.** All six gaps converge on one root cause: **the system has no path for "I'm uncertain — ask the user." It always picks an interpretation and acts.**

## The six specific gaps

1. **Gate A only catches cold-start ambiguity.** Fires on missing context; passes everything once focus exists.
2. **Rewriter substitutes pronouns blindly.** No antecedent-validity check.
3. **No topic-closure signal handling.** "Thanks" doesn't clear focus.
4. **No multi-antecedent disambiguation.** Two valid referents → system silently picks one.
5. **Plural/singular pronoun collision unhandled.** "Them" against singular focus → wrong rewrite.
6. **Negation-of-focus unrecognized.** "Not that one" → system gives them that one anyway.

---

## Solution architecture — five components using existing LangGraph primitives

### Component 1: Confidence-aware output schemas

Every component that interprets user input returns confidence in addition to its output:
- Rewriter returns `(rewrite, confidence, ambiguity_reason)` not just `rewrite`.
- Planner returns `(plan, confidence, ambiguity_reason)` not just `plan`.

Confidence is a structured field on a Pydantic output schema enforced by `with_structured_output`.

**Calibration warning — non-trivial.** Naive LLM self-rating is unreliable; models are systematically overconfident about their own outputs. Plan to use one of:
- **Self-consistency** — run the rewriter 3-5 times at temperature > 0, use disagreement as the confidence signal.
- **Verifier model** — second LLM call critiques the first rewrite ("is this substitution justified given the conversation?").
- **Token-level logprobs** — where applicable (per-token decisions).

Choose the approach during step 1 of implementation order. **This adds 1-2 weeks to the estimate.**

### Component 2: Ambiguity-detection logic

For each gap pattern, build a detector that runs alongside the rewriter. Detection is split by whether the linguistic class is closed or whether semantic judgment is required:

**Deterministic detectors (linguistic class is closed):**
- **Pronoun number mismatch** — grammar check: if pronoun is plural and focus is singular (or vice versa), mark `ambiguity_reason = "number_mismatch"`.
- **Negation** — small fixed lexicon ("not that", "not this one", "the other", "different one") → mark `ambiguity_reason = "negation"`.

**LLM-based detectors with structured-output schemas (semantic judgment required):**
- **Topic-closure detection** — must handle phrasings like "we can move on", "that's all for now", "anything else interesting?", not just literal "thanks". Closed-class regex would miss most real closures. Schema: `{is_topic_closure: bool, confidence: float}`.
- **Multi-antecedent validity** — assessing whether multiple recently-mentioned entities are *still in scope* (vs. one was dismissed or resolved) requires understanding the conversation. Schema: `{candidates: list[entity_id], dismissed: list[entity_id], confidence: float}`.

This split keeps cost down where rules suffice and honesty up where they don't. It also aligns with the project principle that detectors describe semantic principles, not phrase catalogs.

### Component 3: A new `clarification_node`

When any `ambiguity_reason` is set, route to a new graph node. This node:
1. Reads `ambiguity_reason` + conversation state.
2. Builds a specific clarification question based on the reason:
   - `multi_antecedent`: *"Did you mean INC0001001 or CHG0004007?"*
   - `topic_closed`: *"Just to confirm — you'd like me to do this for INC0001001?"*
   - `number_mismatch`: *"Are you asking about the comments or the ticket itself?"*
   - `negation`: *"Which one did you mean instead of INC0001001?"*
3. Calls LangGraph's `interrupt()` to pause and present the question.
4. On resume via `Command(resume=user_answer)`, applies the disambiguation and continues execution.

### Component 4: Conditional edges replacing direct edges

Replace `rewriter → planner` with:

```
rewriter → (confidence >= threshold AND ambiguity_reason is None) → planner
        → (otherwise)                                              → clarification_node
clarification_node → (after resume) → rewriter (re-runs with disambiguation)
```

Apply the same pattern at every other routing decision point.

### Component 5: Topic-closure state reducer

State reducer responds to closure signals (detected by Component 2's LLM-based topic-closure detector). When triggered, the reducer clears focus to `None`. Fixes Gap 3 directly.

---

## The scaling commitment

To prevent these bugs recurring as use cases multiply:

**UC-as-spec.** Each use case is defined by a YAML manifest, not by routing code. The manifest declares:
- Required inputs
- Expected ambiguity patterns
- Clarification questions to ask
- Confidence threshold to require

The routing layer reads the manifest and wires conditional edges automatically.

**No routing in handlers.** Handlers receive a fully-disambiguated request and execute it. If a handler ever needs to ask the user something, that's a design failure — routing should have caught the ambiguity first.

Adding UC-1001 = writing a YAML spec + a handler. Handler inherits all six gap fixes automatically.

---

## Implementation order

1. **Add confidence to rewriter output schema** (Component 1). Pick calibration approach. Verify scores are useful, not vibes.
2. **Build the four ambiguity detectors** (Component 2). Two deterministic, two LLM-based with structured-output schemas. Unit-test against Family 3 probe set.
3. **Build the `clarification_node`** (Component 3). Test each `ambiguity_reason` produces a sensible question.
4. **Replace direct edges with conditional edges** (Component 4). Re-run Family 3 probes; all 6 gaps should now trigger clarification.
5. **Add topic-closure reducer** (Component 5). Re-run P3 specifically: "thanks" must now clear focus.
6. **Update UC spec schema.** Declare ambiguity-pattern fields in YAML. Wire routing to read them.
7. **Migrate UC-1, UC-3, UC-99 to spec format.** Prove the pattern on existing UCs before any new ones.

## Acceptance criteria

- All 8 Family 3 probes show "system survived" (clarification or correct closure handling).
- A new **false-positive suite** (clear input that should NOT trigger clarification) passes — system doesn't pester users.
- A new UC can be added by writing only a YAML spec + handler, with no routing-code changes, and inherits all six gap fixes.

## Estimated effort (revised after correction-pass)

- Components 1-5: **5-8 weeks** (was 3-6 before honest accounting of LLM-based detection + calibration work).
- Components 6-7 (UC-as-spec migration): **2-4 weeks**.
- **Total: 7-12 weeks** depending on calibration approach + clarification-question generation sophistication.

## What this is NOT

- **Not new LangGraph features.** All primitives (`interrupt()`, `Command(resume=...)`, conditional edges, state reducers) already in use elsewhere in this codebase.
- **Not model retraining.** Routing + prompts only.
- **Not a Phase 5 blocker by itself.** Phase 5 (fan-out) and this work can ship in either order. **Reviewer should evaluate the order:** doing this first reduces the ambiguity surface Phase 5's fan-out would otherwise inherit and multiply.

## Reference materials

- Bugs: `docs/findings/family3-ambiguous-referent-2026-05-17.md`
- Probe harness: `/tmp/family3_ambiguous_referent.py`
- Phase 5 design (now links these findings): `docs/design/phase-5-fan-out.md`
- Channel-addition runbook: `docs/runbooks/state-channel-additions.md`

## Industry framing

Today the system is at industry-level-1 ("guess and act") — the LLM default. Production-grade systems are at level-2 (confidence threshold + clarification path). The work is **catching up to industry standard, not inventing.** If reviewer is uncertain about scope, this framing makes the conversation easier.
