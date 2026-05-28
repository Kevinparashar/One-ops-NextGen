# Solving the ambiguity-handling problem in OneOps — v2 (research-grounded)

**Status:** Canonical engineering handoff. Drafted 2026-05-17 incorporating industry research, two adversarial-probe families (15 probes, 9 documented silent-failure modes), and feedback on v1. **Supersedes `ambiguity-fix-package.md` (v1) — kept in place for traceability.**

---

## Load-bearing architectural commitments

> **1. Routing decisions live in conditional edges, not in handler code.**
> Handlers (UC-1, UC-3, future UCs) execute only when called. The routing layer makes every clarification decision. UC authors must never reason about ambiguity handling.

> **2. Ambiguity infrastructure must land BEFORE Phase 5 (Send fan-out) implementation.**
> v1 left the order to the reviewer. The research is unambiguous: fan-out without ambiguity infrastructure multiplies the silent-failure surface (instead of one ambiguous rewrite per turn, you get N silent-failures per turn — one per sub-query). Phase 5's design doc remains valid; its **implementation** is gated on this package's Components 1–6 landing.

> **3. The state model is the primary fix, not the rewriter.**
> v1 framed this as a routing/clarification fix. v2 frames it as a **state-model fix with a clarification layer on top**. The persistent entity ledger (Component 6) is the structural change that collapses most of the 9 gaps into one foundation.

---

## Context

OneOps is a multi-tenant ITSM query router on LangGraph. v1 ships UC-1 (summarization), UC-3 (KB Lookup), UC-99 (conversational). Phase 5 design (multi-part query fan-out) is in `docs/design/phase-5-fan-out.md`, awaiting review.

Two adversarial-probe families on 2026-05-17 (`docs/findings/family3-ambiguous-referent-2026-05-17.md`, `docs/findings/family1-focus-pivot-2026-05-17.md`) found **9 distinct silent-failure modes** across 15 probes. **6 of 8 Family 3 probes failed silently; 3 of 7 Family 1 probes failed silently. One additional latent bug surfaced (P2 clarification-text generator emits identical options).**

The 9 gaps:
1. Gate A only catches cold-start ambiguity
2. Rewriter substitutes pronouns without antecedent validation
3. No topic-closure signal handling ("thanks" doesn't clear focus)
4. No multi-antecedent disambiguation (extends to 3+ entities — see F1.P6)
5. Plural/singular pronoun collision unhandled
6. Negation-of-focus unrecognized
7. Passthrough at rewriter still results in execution against ambient focus
8. Ordinal references unsupported ("the first one", "the previous", "earlier")
9. Type-disambiguation is fragile, not absent — works in 2-entity conversations, collapses in 3+ entity conversations

All nine converge on one root cause: **the system has no path for "I'm uncertain — ask the user," and it has no rich state to be uncertain *with*.** Today's state model tracks one entity (`focus`). Real conversations contain many. Research on real assistant traffic shows **~70% of daily user turns contain omissions and coreferences** — the cases we found are the main case, not edge cases.

---

## Research anchors (industry validation)

This package's architectural choices map to three independent production / research patterns:

1. **Elastic's published LangGraph HITL pattern** (legal AI, contract analysis). Conditional graph paths with explicit "request clarification" nodes via `interrupt()`. Quote: *"If the LLM detects ambiguities... it returns a flag indicating that clarification is required, along with a list of the specific pieces of information that must be provided."* Customer-support triage is named in their docs as the canonical use case. ITSM ticket routing is structurally identical. Validates Components 3 and 4 below.

2. **ServiceNow's Context Engine** (dominant ITSM player). Their answer to this exact problem is **a persistent entity-relationship graph**, not a smarter rewriter. Every interaction enriches the graph; every AI decision reads from the graph instead of inferring context fresh per turn. Validates Component 6 — and is the substantive thing v1 was missing.

3. **MAC paper — Multi-Agent Clarification** (Dec 2025, academic). Coordinated, targeted clarification by per-domain experts beats generic clarification. Reported 7.8% absolute success-rate increase (54.5% → 62.3%) plus **shorter conversations** (6.53 → 4.86 turns avg). Counter-intuitive: asking more targeted questions reduces total dialogue length. Validates Component 7 — per-UC clarification declarations, not a single generic node.

---

## Solution architecture — seven components

### Component 1: Confidence-aware output schemas

Every component that interprets user input returns confidence + ambiguity signal in addition to its primary output:
- Rewriter returns `(rewrite, confidence, ambiguity_reason)`.
- Planner returns `(plan, confidence, ambiguity_reason)`.

Confidence is a structured field on a Pydantic output schema enforced by `with_structured_output`.

### Component 1b: Two-pass verifier — LLM classification + Python rule engine

**STATUS (2026-05-17): SHIPPED.**

Original v2 design called for a single-pass verifier model. Diagnostic A on
2026-05-17 showed the LLM produced "walk-shaped" output without actually
walking the rules — it classified the expression correctly, then back-filled
`candidates_before_filter` against a pre-formed conclusion. The CoT fields
looked filled in but the model wasn't bound by them. Same case ("not that
one — the other" with single-entity ledger, focus excluded by negation
rule) produced different `rewrite_matches_intent` values across temp=0 runs.

**Resolution: two-pass architecture.**

- **Pass 1 (`_classify` in `src/oneops/routing/verifier.py`):** LLM call that
  produces only `RewriteClassification` — referring_expression + expression_type
  + type_filter_applied + model_reasoning. No verdict, no candidates, no
  rule application. The LLM cannot back-fill against a pre-formed conclusion
  because there's no conclusion field in this schema.
- **Pass 2 (`apply_rules` in `src/oneops/routing/verifier_rules.py`):**
  Pure Python rule engine. Reads the classification + entity ledger + focus
  + proposed rewrite. Computes `candidates_before_filter` → `_after_filter`
  → `valid_candidates` → `is_unambiguous` → `rewrite_matches_intent`
  deterministically. 67 unit tests cover every rule function in isolation.

**Policy-layer integration:** `_classify` uses
`compose(Profile.FEATURE_AGENT_JSON, ...)` against
`src/oneops/routing/prompts/verifier_classifier.md` — same pattern as
decomposer / rewriter / uc_reranker. Multi-tenant safety, RBAC, audit trail,
anti-fabrication blocks all applied. **Originally missed in the first
implementation; caught during verifier code review.**

**Cost outcome:** **~0.7-0.9x baseline** per verifier call, not 2x as v2
originally estimated. The classification-only schema is smaller than the
original 9-field verdict schema, so prompt prefix caching still applies but
response tokens are lower. Net: cost reduction, not cost trade.

**Test coverage (2026-05-17):**
- 80 unit tests on the rule engine (pure Python, 2 seconds)
- 13 classifier-correctness tests (LLM-dependent, ≥4/5 stability tolerance)
- 4 integration tests (full Pass 1 + Pass 2 orchestration)
- 5 strict-mode JSON schema tests (catches Pydantic schema regressions)
- Sanity verified: `verify_rewrite()` degrades gracefully with empty/missing
  `request_ctx`

**Files landed:**
- `src/oneops/routing/verifier.py` — orchestration + `_classify` + audit log
- `src/oneops/routing/verifier_rules.py` — rule engine + schemas
- `src/oneops/routing/prompts/verifier_classifier.md` — principle (composed via policy layer)
- `src/oneops/gateway/structured_output.py` — strict-mode helper
- `tests/unit/test_verifier_rules.py`
- `tests/unit/test_strict_structured_output.py`
- `tests/adversarial/step_1b_a_classifier_correctness.py`
- `tests/adversarial/step_1b_a_classifier_integration.py`

---

### Component 1b (HISTORICAL — superseded): single-pass verifier model

**Industry research is clear: naive LLM self-rating is unreliable.** Three options were evaluated:

| Method | Reliability | Cost | Provider lock-in | Verdict |
|---|---|---|---|---|
| LLM self-rating | Unreliable (systematic overconfidence) | 1x | None | **Reject** |
| Self-consistency sampling (3-5 generations, measure disagreement) | High (SOTA) | 3-5x | None | Too expensive at v1 scale |
| Token-level logprobs | High where available | ~1.1x | OpenAI / Anthropic only for some endpoints | Provider risk |
| **Verifier model (2nd LLM critiques 1st)** | High | 2x | None | **CHOSEN** |

**Decision: use a verifier model.** A second LLM call critiques the first rewrite ("Given this conversation and this rewrite, is the antecedent unambiguous? List all valid candidates."). Verifier output is structured: `{is_unambiguous: bool, valid_candidates: list[entity_id], reasoning: str}`. The verifier's output drives the routing decision.

**Cost trade-off documented:** every rewrite that hits Branch 1 (pronoun) or Branch 2 (bare attribute) becomes 2x LLM cost. Mitigations: cache verifier results keyed on `(rewrite, recent_entity_ledger_snapshot)`; skip verifier when entity ledger has ≤1 entity (no possible ambiguity).

Re-evaluate the method when traffic scales — at high volume, self-consistency may amortize better.

### Component 2: Ambiguity detection (hybrid)

Detection split by linguistic class:

**Deterministic detectors (closed lexical class, ~10% of detection work):**
- Pronoun number-mismatch: grammar rule comparing pronoun number against focused entity (now: entity ledger candidates).
- Negation lexicon: small fixed set ("not that", "not this one", "the other", "different one").
- Ordinal token detector (NEW from Family 1 Gap 8): closed class — "first", "second", "third", "previous", "earlier", "original". When detected, the rewriter must consult the entity ledger by mention-order, not by current focus.

**LLM-based detectors with structured output (semantic judgment, ~60% of detection work):**
- Topic-closure detection: must handle "thanks", "that's all for now", "we can move on", "anything else interesting?". Schema: `{is_topic_closure: bool, confidence: float}`.
- Multi-antecedent validity (the verifier model from 1b doubles as this detector).
- Type-disambiguation consistency (NEW from Family 1 Gap 9): make the type-word logic ("the incident", "the change") explicit and route both pronoun branch AND bare-attribute branch through the same entity-ledger query filtered by service_type.

**State-based detectors (entity-ledger queries, ~30% of detection work):**
- Multi-candidate counter: if entity ledger has N≥2 active entities of compatible type for the referring expression, mark `ambiguity_reason="multi_antecedent"` regardless of pronoun shape.
- Recency-vs-explicit-mention conflict: if the message contains an explicit entity ID that differs from `last_mentioned`, the explicit mention wins (closes Family 1 P3-style cases by design).

### Component 3: `clarification_node`

When any `ambiguity_reason` is set, route to a new graph node that:
1. Reads `ambiguity_reason`, entity ledger, and the per-UC clarification declarations (Component 7).
2. Builds a specific clarification question — **never generic.** Examples:
   - `multi_antecedent` with 2 incidents: *"Did you mean INC0001001 or INC0001002?"*
   - `multi_antecedent` with 3 mixed types: *"Which one — the incident (INC0001001), the change (CHG0004007), or the problem (PBM0003003)?"*
   - `topic_closed` (verifier disagrees with focus): *"Just to confirm — you'd like me to do this for INC0001001?"*
   - `number_mismatch` (plural ref against singular focus): *"Are you asking about the comments on INC0001001, or the incident itself?"*
   - `negation`: *"Which one did you mean instead of INC0001001?"*
   - `ordinal`: *"By 'the first one' do you mean INC0001001 (mentioned earliest in this conversation)?"*
3. Calls LangGraph's `interrupt()` to pause and present.
4. On `Command(resume=user_answer)`, applies the disambiguation by updating the entity ledger (mark resolved-entity as active, others as dismissed) and re-runs the rewriter with the now-unambiguous state.

**Latent bug to fix in this component (Family 1 P2):** today's nascent clarification text generator can emit *"did you want uc01_summarization or uc01_summarization?"* — same UC twice because both candidates happen to route to the same handler. Distinguish UC-level ambiguity (two different UCs) from entity-level ambiguity (two entities, same UC). For entity-level, list entity IDs; for UC-level, list UC display names.

### Component 4: Conditional edges replacing direct edges

Replace `rewriter → planner` with:

```
rewriter → verifier (Component 1b) → (unambiguous AND confidence >= threshold) → planner
                                    → (otherwise)                                → clarification_node
clarification_node → (after resume) → rewriter (re-runs with ledger updated)
```

Apply the same pattern at every other routing decision point.

**Cost mitigation to wire in this component (deferred from Component 1b):**
Skip the verifier node when `len(active_ledger_entries) <= 1`. With zero or one active entity there's no antecedent ambiguity possible, so the verifier's LLM call is pure overhead. Add as a conditional-edge predicate at the same time as the routing-on-verdict logic, not before — during 1b validation, the verifier must run on every case so we can see if it correctly handles single-entity inputs too.

### Component 5: Topic-closure state reducer

State reducer responds to the LLM-based topic-closure detector (Component 2). When triggered, the reducer marks the *current focus* in the entity ledger as `status=dismissed` rather than clearing focus blindly. The ledger retains the history; future references can still resolve to dismissed entities if the user explicitly re-mentions them (closes Family 1 P3 design intent).

### Component 6 (NEW from research) — Persistent entity ledger

**This is the structural change v1 missed.** Replace single-field `focus` state with a tracked list of entity mentions per session. State channel:

```python
class EntityMention(TypedDict):
    entity_id: str               # "INC0001001"
    service_id: str              # "incident"
    first_mentioned_turn: int    # 1
    last_mentioned_turn: int     # 3
    status: Literal["active", "dismissed", "stale", "resolved"]
    dismissed_reason: Optional[str]   # "user said 'not that one'" | "topic closed" | "user re-pivoted"
    handler_outputs: list[dict]  # which UC ran on this entity, when, what result

entity_ledger: Annotated[dict[str, EntityMention], merge_dict_reducer]
# Key = entity_id; reducer same shape as the dict-merge pattern verified in step 5.1
```

**Why this collapses most gaps:**
- Gap 4 (multi-antecedent): trivial — count `active` entries.
- Gap 5 (plural/singular collision): filter ledger by compatible types.
- Gap 6 (negation): mark explicitly-rejected entity `dismissed_reason="explicit_negation"`; verifier excludes it.
- Gap 8 (ordinals): sort ledger by `first_mentioned_turn`.
- Gap 9 (type-disambig fragility): filter ledger by `service_id` — same routine whether 2 or 30 entities.
- Family 1 P3 (re-mention re-pivots): explicit-mention path updates `last_mentioned_turn` and `status=active`.

**State-channel runbook compliance:** the dict-merge pattern is already verified safe under v2 → v2.1 channel addition (step 5.1 smoke, 2026-05-17). Adding `entity_ledger` requires running the Path-B-shape smoke per `docs/runbooks/state-channel-additions.md` R6 (3 minutes).

### Component 7 (NEW from research) — Per-UC clarification in UC-as-spec

**Generic "please clarify" hurts users. Targeted questions help.** MAC paper shows +7.8% success / -25% dialogue length when each domain expert can ask its own questions.

Each UC's YAML manifest declares its clarification capabilities:

```yaml
# Example: uc01_summarization manifest
clarification:
  ambiguity_patterns:
    - reason: "multi_antecedent"
      question_template: "Which one — {entity_list_with_types}?"
      examples_for_llm: ["Which one — the incident (INC0001001) or the change (CHG0004007)?"]
    - reason: "topic_closed_then_referenced"
      question_template: "Just to confirm — summary for {focus_entity}?"
    - reason: "ordinal_unclear"
      question_template: "By '{ordinal_token}' do you mean {entity_by_order}?"
  confidence_threshold: 0.75   # below this, route to clarification
```

The `clarification_node` reads the relevant UC's spec and constructs the question. UC-1001 will inherit the framework — its manifest declares its own ambiguity patterns; routing handles the rest.

---

## Scaling commitment

Adding a UC = writing a YAML manifest + a handler. The handler receives a fully-disambiguated request. **If a handler ever needs to ask the user something, that's a design failure — the routing should have caught it.**

This keeps the Family 3 + Family 1 gap classes from recurring at UC #1001 because:
- Entity ledger is shared state, populated by routing, read by every UC.
- Clarification logic lives in the conditional edges, parameterized by UC manifest.
- No new UC can introduce a 10th gap-class without it being a manifest declaration first.

---

## Implementation order

1. **Entity ledger state channel** (Component 6). State-channel addition + Path-B-shape smoke per runbook. Populates from existing rewriter + decomposer + UC handler outputs. **~1.5 weeks.**
2. **Verifier model** (Component 1b). Build the verifier prompt + structured output schema. Calibrate threshold against Family 3 + Family 1 probe set. **~1.5 weeks.**
3. **Hybrid ambiguity detectors** (Component 2). Two deterministic + two LLM-based + two state-based, all reading the entity ledger. Unit tests against probe set. **~2 weeks.**
4. **`clarification_node`** (Component 3). Per-UC question building; fix the same-UC-listed-twice latent bug. **~1.5 weeks.**
5. **Conditional edges** (Component 4). Replace direct edges; re-run all 15 adversarial probes; **all 9 gaps should now trigger clarification or correct resolution.** **~1 week.**
6. **Topic-closure reducer** (Component 5). LLM-based detector + ledger status update. Re-run Family 3 P3. **~0.5 weeks.**
7. **UC-spec schema update** (Component 7). YAML field additions + spec validator. Migrate UC-1, UC-3, UC-99. **~2 weeks.**
8. **False-positive eval suite.** Clear inputs that must NOT trigger clarification. Tune thresholds. **~1 week.**

**Total: ~11 weeks core (Components 1–6). +2 weeks for Component 7. +1 week for FP tuning. = 14 weeks engineering time.**

Realistic range with calendar slack: **10–16 weeks.**

---

## Acceptance criteria

- All 8 Family 3 probes show *system survived* (clarification asked OR correctly handled closure signal).
- All 7 Family 1 probes show *system survived* — particularly P1 (ordinal), P4 (3-entity type-disambig), P6 (3-entity ambiguity).
- A new false-positive suite: ≥100 clear queries; clarification triggers on ≤5% (industry-standard tuning).
- A new UC can be added with manifest + handler only; no routing-code changes; inherits all 9 gap fixes automatically.
- Entity ledger state channel passes Path-B-shape smoke per state-channel runbook.

---

## What this is NOT

- **Not new LangGraph features.** Every primitive (interrupt, Command(resume), conditional edges, state reducers, structured-output schemas) already in use.
- **Not model retraining.** Routing + prompts + state model only.
- **Not optional.** v1 framed Phase 5 ordering as reviewer's choice. v2 commits: ambiguity infrastructure precedes Phase 5 implementation. Inverting the order multiplies bug surface.

---

## Cross-references

| Artifact | Purpose |
|---|---|
| `docs/findings/family3-ambiguous-referent-2026-05-17.md` | Original 6 gaps with probe evidence |
| `docs/findings/family1-focus-pivot-2026-05-17.md` | 3 additional gaps + latent clarification-text bug |
| `docs/design/phase-5-fan-out.md` | Multi-part query fan-out design — implementation gated on this package |
| `docs/runbooks/state-channel-additions.md` | Procedure for adding `entity_ledger` channel (Component 6) |
| `docs/design/ambiguity-fix-package.md` (v1) | Original package — kept for traceability |
| `/tmp/family3_ambiguous_referent.py` | Re-runnable probe harness |
| `/tmp/family1_focus_pivot.py` | Re-runnable probe harness |

---

## Gap-to-component map (for the engineering reviewer)

| Gap | Source | Resolved by |
|---|---|---|
| 1. Gate A only catches cold-start | F3 | Component 4 — conditional edges everywhere, gated on verifier output |
| 2. Rewriter substitutes blindly | F3 | Components 1b + 6 — verifier consults ledger |
| 3. No topic-closure | F3 | Components 2 + 5 — LLM detector + ledger status update |
| 4. No multi-antecedent disambig | F3 (2-entity), F1 P6 (3-entity) | Component 6 — ledger counter + Component 3 — targeted question |
| 5. Plural/singular collision | F3 | Component 2 + Component 6 type filter |
| 6. Negation unrecognized | F3 | Component 2 deterministic + Component 6 dismissed status |
| 7. Passthrough still executes against focus | F3 P7/P8 | Component 4 — verifier runs on passthrough too |
| 8. Ordinals unsupported | F1 P1 | Component 2 ordinal detector + Component 6 ordered ledger |
| 9. Type-disambig fragile | F1 P4 | Component 2 — unify pronoun + bare-attribute branches on ledger type-filter |
| (latent) Clarification-text bug | F1 P2 | Component 3 — distinguish UC vs entity ambiguity in builder |

---

## Industry framing for reviewer conversation

Today the system operates at **industry-level-1** ("guess and act") — the default LLM behavior. Production-grade ITSM systems operate at **level-2** (confidence threshold + clarification path) with the state model of **level-3** (persistent entity context, the ServiceNow Context Engine pattern). This package brings OneOps to level-2 with a level-3 state foundation.

**Framing:** catching up to industry standard. Not inventing.
