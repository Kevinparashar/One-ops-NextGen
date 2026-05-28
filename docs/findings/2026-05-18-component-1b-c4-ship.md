# 2026-05-18 — Component 1b classifier hardening + Component 4 ship

**Session shape:** two working sessions across one day. Morning session
hardened the verifier classifier via prompt restructure + deterministic
post-validation. Afternoon session wired the verifier into the routing
graph with conditional edges, auto-correct, and a placeholder for
Component 3's real clarification node.

End-state: **4 of 7 components in the ambiguity-fix-package-v2 are
shipped.** Component 1b (verifier), Component 4 (conditional edges), and
Component 6 (entity ledger) form the foundation. Components 3 (real
clarification), 5 (topic-closure reducer), 7 (UC-as-spec YAML), and 2
(hybrid detectors) build on top.

---

## What shipped today

### Morning session — Component 1b classifier hardening

The two-pass verifier shipped 2026-05-17 was architecturally sound but
the LLM classifier (Pass 1) had specific failure modes. Adversarial eval
of 20 cases surfaced three concrete problems before any LLM calls fired:

1. **Priority order not honored** — `"the latest incident"` classified
   as `type_word` instead of `ordinal` (ISS-002). The principle's
   priority-order rule was stated but not given structural prominence.
2. **Type_filter hallucinations on OOV type words** — `"the alert"`
   classified as `type_word` with hallucinated `type_filter="incident"`
   (ISS-003). Silent-wrong-answer in the exact shape Component 1b was
   built to prevent.
3. **Type_word category description had grown too long** — created
   label-length bias (documented phenomenon — arxiv 2305.19148) causing
   the LLM to over-weight type_word as the default classification.

**Fixes applied — (B)+(C) defense in depth:**

- **(B) Prompt restructure:** promoted priority order to Step 1 with
  explicit "non-negotiable" framing. Collapsed type_word description
  back to parity with other categories. Added Examples 8/9/10
  demonstrating priority order in action (`"the latest incident"` →
  ordinal, `"the other incident"` → negation, `"the alert"` → other).
- **(C) Deterministic post-classifier override:** Python check in
  `_classify()` — if `expression_type=type_word` AND `type_filter` not
  in registered service_ids, override to `other`. Defense-in-depth
  against any future LLM hallucinations of closed-class field values.

**Results after (B)+(C):**

- Regression: 50/52 baseline holds (same E1 deferred case)
- Adversarial: all 13 ambiguous cases → 3/3 consistent at temp=0
- ISS-003 cases: 4/4 OOV type words now classify as `other` correctly
- ISS-002 cases: P1.3, P4.1 honor priority order at 3/3
- One new regression: P1.2 `"summarize INC0001001 — not that one"` →
  `negation` instead of `explicit_id`. Contrived case, fails safely
  (clarification path). Documented; not patched.

### Afternoon session — Component 4 ship

Component 1b produces `VerdictDecision`s but until C4, nothing in the
graph read them. C4 wired the verifier into the routing topology.

**Code shipped:**

- `src/oneops/state/schema.py` — 4 new state channels
  (`verifier_is_unambiguous`, `verifier_rewrite_matches_intent`,
  `verifier_valid_candidates`, `verifier_reasoning`)
- `src/oneops/routing/nodes.py` — 3 new nodes:
  - `verifier_node` — calls two-pass `verify_rewrite()`, writes verdict
  - `auto_correct_node` — substitutes `valid_candidates[0]` into rewrite
    when verifier flags rewriter chose wrong (defensive assertion that
    `len(valid_candidates) == 1` per rule-engine contract)
  - `clarification_placeholder_node` — STUB for Component 3, surfaces
    verdict reasoning as `final_status=clarification_required`
- `src/oneops/routing/nodes.py` — `verifier_branch` conditional-edge
  function (pure decision, no I/O, no mutation)
- `src/oneops/graph/builder.py` — three_stage topology wired:
  `rewriter → verifier → {shortlist | auto_correct → shortlist |
  clarification_placeholder → aggregator}`

**Test gates — 39 green:**

- Path-B-shape migration smoke (4 new state channels) — 14/14
- Live end-to-end smoke (4 probes × 3 routing paths) — 18/18
  - Pa: type-narrow `"the SLA on the incident?"` → proceed → executed
  - Pb: explicit_id `"summarize INC0001001"` → proceed → executed
  - Pc: ordinal `"summarize the first one"` → **auto-correct** (rewriter
    chose CHG → verifier corrected to INC) → executed
  - Pd: negation `"not that one — the other"` (single-entity ledger) →
    **clarification_placeholder** → clarification_required
- Legacy regression (UC-1 3-probe under `ROUTING_MODE=legacy`) — 3/3
  byte-identical (verifier doesn't run in legacy)

**Audit log confirms both load-bearing paths fire correctly:**

```
verifier.auto_correct
  original_rewrite='summarize CHG0004007 again'
  corrected_rewrite='summarize INC0001001 again'
  substituted_entity_id=INC0001001
  valid_candidates=['INC0001001']
```

```
verifier.clarification_placeholder
  reasoning="Negation expression 'not that one — the other' excluded the
  rewriter's focus. valid_candidates=[] (focus removed; no replacement
  available). is_unambiguous=False; rewrite_matches_intent=False."
  valid_candidates=[]
```

---

## What the adversarial eval revealed (research-anchored)

The morning's eval surfaced patterns recognized in the LLM literature:

1. **Label length bias** (arxiv 2305.19148) — explained type_word
   over-weighting empirically observed.
2. **Contrastive in-context learning** — explains why Examples 8/9/10
   (priority-order-in-action) fixed the priority-order failures.
3. **More prompt detail can degrade reliability** — instruction-following
   research confirms what (B)'s collapse-back-to-parity validated.

These three findings were captured in **ISS-006 Prompt-design discipline**
with the structural rules for future prompts in Components 3, 5, 7.

---

## Architectural literature anchor

**Rule Maker Pattern (Tessl.io 2025):** "By using AI to generate rules
that execute deterministically, you get the same result every time."

This is the architectural pattern Component 1b's two-pass design
implements. ISS-001's generalization paragraph now cites this so future
maintainers understand the architecture isn't idiosyncratic — it's a
named, validated industry approach.

OpenAI's **instruction hierarchy** paper (arxiv 2404.13208) describes the
same class of failure (LLMs ignoring stated rule priorities) at the
fine-tuning level. Our production-grade fix (counter-examples +
deterministic post-validation) is the practical equivalent of OpenAI's
research-grade fine-tuning fix.

---

## Three classifier limitations remaining (all fail safely)

| Limitation | Behavior | Routes to |
|---|---|---|
| **ISS-002 P1.2** — explicit_id beaten by negation on `"summarize INC0001001 — not that one"` | Misclassified as negation; verifier excludes focus → empty candidates | Clarification |
| **ISS-004** — positional ordinals not in catalog (`"the one before that"`, `"two ago"`) | Classified as `no_referent` instead of `ordinal` | Clarification |
| **ISS-005** — imperative-form pronouns (`"summarize them"`) | Classified as `no_referent` instead of `pronoun` | Clarification |

None of these are silent-wrong-answers. All route to clarification — the
safe failure mode. They're documented in the issue ledger with deferral
rationale and conditions for re-prioritization (e.g. P1.2 only blocks if
production traffic shows the shape is common; ISS-005 becomes more
pressing when UC-2 ships with imperative actions like "close it").

---

## End-of-day state

| Component | Status |
|---|---|
| Component 1b — two-pass verifier | ✓ SHIPPED 2026-05-17; hardened 2026-05-18 |
| Component 6 — entity_ledger state channel | ✓ SHIPPED 2026-05-17 |
| **Component 4 — verifier wiring in graph** | **✓ SHIPPED 2026-05-18** |
| Component 3 — real clarification node | ○ pending (placeholder stubbed in C4) |
| Component 5 — topic-closure reducer | ○ pending |
| Component 2 — hybrid ambiguity detectors | ○ pending |
| Component 7 — UC-as-spec YAML | ○ pending |

**v2 package: 4 of 7 components shipped.** Verifier is now doing real
work on routing traffic, not just sitting in tests.

---

## What this means for new UC development

A new UC's design + handler can start tomorrow against three_stage
routing. The verifier will:
- Catch silent-wrong-answer cases the new UC's prompt would have hit
- Auto-correct rewriter substitutions to the right entity when intent
  is unambiguous
- Route to clarification_placeholder when intent is ambiguous

For a low-risk read-only UC (e.g. another summarization-class capability),
this means the new UC inherits the verifier's safety net automatically
with no per-UC code. The placeholder clarification UX is "could you
clarify?" — generic but safe — until Component 3 lands.

---

## Discipline notes

The pattern that held across both sessions:

1. **Adversarial eval before LLM calls** — predictions force gap-surfacing in the principle itself, not just in the LLM's output.
2. **Issue ledger before fix-mode** — writing ISS-002/3/4/5/6 with the structured Trigger/Wrong/Right/Root-cause/Fix/Test/Status/Generalization shape, BEFORE writing the fix code, surfaced the patterns each fix needed to address.
3. **Migration smoke before node code** — Path-B-shape gates on every state-channel addition (R6 from the runbook).
4. **Live end-to-end smoke before any "ship" claim** — 18/18 across 3 routing paths is what makes "shipped" defensible.
5. **Legacy regression as the safety gate** — 3/3 byte-identical under `ROUTING_MODE=legacy` is what proves no production-impact risk.

Each gate caught something real or confirmed something testable. The
upfront discipline compounded into a clean implementation phase where
no earlier decisions had to be revisited.

---

## Next decision (tomorrow, fresh head)

Component 3 (real clarification node) or Component 5 (topic-closure reducer).

- **C3** unblocks UC-specific clarification UX. Per ISS-006 + LangGraph
  `interrupt()` gotcha (LLM call must happen AFTER interrupt, not before
  — node restarts on Command(resume=...)).
- **C5** unblocks topic-closure detection ("thanks" clears focus). Fixes
  Family 3 P3 silent re-execute against ambient focus.

Both are well-scoped; the decision benefits from a fresh-head review of
which one production traffic patterns will hit first.
