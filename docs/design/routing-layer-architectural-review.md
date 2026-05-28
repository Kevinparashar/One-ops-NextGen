# Routing-Layer Architectural Review

**Date:** 2026-05-18
**Author:** Kevin (drafted with Claude)
**Status:** Design review — not implementation. No code lands until this is signed off.

This document is the input to next-sprint planning. It diagnoses the failure pattern observed this week across ISS-007, ISS-012, Probe 2, and the 70-scenario batch; surveys architectural responses; and commits to one. It is grounded in the recorded ledger, not in general principles.

---

## (a) Diagnosis

The routing layer's failures this week sit on **three** distinct architectural properties, not one. Conflating them produces the wrong fix.

**Property 1 — LLM doesn't apply stated rules.** ISS-001, ISS-002, ISS-003, ISS-005. Stating a rule in a principle/prompt does not guarantee its application. The LLM reads the rule and can still back-fill against a pre-formed verdict (ISS-001), let category-prose dominance override an inline priority directive (ISS-002), hallucinate a closed-class enum value (ISS-003), or apply category logic selectively by clause form (ISS-005). **Status: largely addressed.** The two-pass Rule Maker Pattern at the verifier (Component 1b) plus closed-class deterministic overrides (ISS-003's defense-in-depth) handle the bulk of this. ISS-005 remains deferred — Rule Maker at the verifier was necessary but not sufficient; closed-class pronoun-token override is still pending. Lesson: Rule Maker is the right shape, but every LLM-classifier stage that fills a closed-class field also needs a Python override.

**Property 2 — Routing on a collapsed signal.** ISS-007 and ISS-012. A single boolean or scalar at a conditional edge collapses multiple structurally-different decisions into one comparator. Verifier's `is_unambiguous=False` fired identically for "ambiguous referring expression" and "no referring expression at all" — same boolean, two different right answers (ISS-007). Reranker's `margin < threshold` fired identically for cross-UC ambiguity and intra-UC presentation choice — same scalar, two different right answers (ISS-012). **Status: both addressed in their specific spots.** Both fixes promoted the underlying structural state (classifier expression_type; uc_id equality) and branched on it. The generalization holds, but it has only been applied where a specific failure surfaced. Other gates in the pipeline (Gate A `low_confidence`, planner intent dispatch) have not been audited for the same shape.

**Property 3 — Retrieval/scoring fragility on natural phrasings.** Probe 2's `p2-f1` and `p2-f4`. The batch's list-style queries ("Show me all incidents", "List my open tickets") and bare-attribute follow-ups. The shortlist returns `low_confidence` or empty for phrasings whose meaning is well-defined but whose token surface diverges from the principle text. **Mechanism:** the shortlister is hybrid retrieval (embedding + Postgres FTS + trigram) over `uc_capabilities.principle_description`, and all three signals reward token / sub-token overlap with the principle. UC-1's `field_read` principle lists representative fields ("priority, status, assigned owner, SLA, parent problem, related changes, approval state...") but a phrasing like "what category does this fall under?" shares neither the field-name tokens nor a strong embedding neighborhood with that list. The candidate either fails to surface or surfaces with a score below Gate A's threshold — and the rerank never gets to disambiguate it. Property 1's failures were the LLM ignoring a stated rule; Property 3's failures are the retriever not surfacing the right row at all, because principle text is a *finite token surface* trying to cover an *infinite phrasing surface*. **Status: never directly addressed.** This is the open architectural work. It has two distinct inputs: (i) a discipline gap at the scoring layer — shortlist and rerank are the only stages of the routing pipeline that *haven't* received Rule Maker treatment; they are purely retrieval scores and an LLM judgment, with no structured intermediate state and no deterministic guards; and (ii) dirty data at the catalog layer (see catalog audit). UC-99 today has six rows in `uc_capabilities` (greet, thank, goodbye, help, capability_inquiry, confusion), only one (`conversational`) with a curated principle — the other five carry auto-generated boilerplate. UC-3 has five rows where the principle is the canonical-cap text plus four weakly-described aliases (`kb_search`, `kb_lookup`, `open_kb`, `find_related_kb_for_*`). UC-1's `uc01_summarize` row is a literal duplicate of `summary` competing in the same retrieval. And there are **three notional sources of truth** for what an agent is — the runtime `register_uc_handler` call, the DB `uc_capabilities.principle_description`, and `registries/agent-catalog-registry.json` — with only one (the DB) actually consumed by routing. The JSON catalog's `skills/goals/description` are decorative. The seed script is the de facto contract.

The honest reading: Properties 1 and 2 are matters of pattern application — the right shape has been found, it just needs to be carried to one more class of failures (closed-class override at every classifier, structural-state branching at every gate). Property 3 is matters of substrate — the retrieval scoring layer was built without the discipline that the verifier got, and the catalog inputs to it are noisy.

**Urgency ranking.** Of the three properties, **Property 3 is the highest-urgency open work**: it produces a ~22% clarification rate in the 70-scenario batch (151/194 executed) and the failures are directly user-visible. Property 1's residual (ISS-005) routes to clarification safely — a degraded UX, not a wrong answer. Property 2's audit is hypothetical until a failure surfaces in an unaudited gate. The migration sequence in (d) should reflect this — Property 3 work loads the early slices.

## (b) Options

This pattern is not unbounded gate-by-gate work. It converges on one architectural commitment with two prerequisites. The options below survey the candidates before (c) commits to one.


**Option A — Continue gate-by-gate.** File ISS-013 for Gate A low_confidence, ISS-014 for bare-attribute rewriter, ISS-015 for the next probe failure. Patch each.
- *Track record:* This strategy shipped ISS-001, ISS-002, ISS-003, ISS-006, ISS-007, and ISS-012 — six of seven issues this week. It works for Properties 1 and 2: local failures with bounded scope where a targeted fix at one layer resolves the issue without architectural change. **It is the right strategy for those problems.**
- *Where it fails:* It is the wrong strategy for Property 3, where the failures are substrate problems (retrieval scoring fragility, dirty catalog data) that recur as new phrasing classes. Each new natural phrasing surfaces as a new gate failure and a new issue file. The backlog is unbounded by construction — there are more natural English phrasings than there are sprints.
- *Trade-off:* Gains low-blast-radius, easy-rollback fixes and continuous shippable improvement; loses architectural progress on the substrate problem.
- *Cost:* ~1 sprint per Property 3 instance discovered; ~6-10 sprints to address known instances (Probe 2 failures, batch list-style gaps, catalog dirt); unbounded for future instances.
- *Risk:* Risk that one gate-by-gate fix masks a deeper structural issue and we only discover it when the masking fix needs to be reverted later. Cumulative; grows with each patch on the substrate layer.

**Option B — Replace routing with single LLM intent classifier.** One LLM call: `{intent, target_uc, entities, ambiguity_flag, clarification_question}`. Code validates against registries.
- *Addresses:* Properties 1 and 3 if the classifier is good enough.
- *Why this option is architecturally incompatible:* Option B contradicts ISS-003's generalization — *"for ANY closed-class field, add a Python set-membership check — defense in depth."* Single-LLM routing is the OOB ITSM agent pattern; it exhibits the silent-wrong-answer failure mode ISS-003 was filed to prevent — the LLM picks a value outside the closed class without any Python override catching it. This is not a scaling problem solvable by chunking. It is an architectural incompatibility with what OneOps committed to two weeks ago when the Rule Maker Pattern was adopted at the verifier. The 50-UC ceiling is a symptom; the defense-in-depth abandonment is the diagnosis.
- *Trade-off:* Gains a single eval surface and minimal code; loses defense-in-depth, loses per-stage defensibility (which stage failed?), loses the ability to fix routing without re-tuning the LLM contract.
- *Cost:* 3 sprints to build single-LLM router + 2 sprints eval calibration; ongoing prompt tuning beyond.
- *Risk:* Risk that context budget breaches around UC-50 with no graceful degradation; eval tooling is not yet built; OOB-pattern history shows silent-failure rates in the ~10-15% range at scale; the architectural incompatibility above is the hard ceiling, not the cost of building.

**Option C — Apply Rule Maker Pattern to retrieval/scoring layer. Catalog cleanup as prerequisite.** Two-pass at the shortlist+rerank boundary: LLM emits a structured intent classification + entity hypothesis; Python rule engine validates against registered UCs, applies tenant/RBAC/service-prefix invariants, and selects. Catalog cleanup (slice 1) eliminates the weak/duplicate rows that make retrieval noisy.
- *Addresses:* Property 3 directly (substrate). Closes Property 2 generalization at the gates not yet audited. Reinforces Property 1 (closed-class override for the intent enum).
- *Trade-off:* Gains defense-in-depth at the routing layer matching what the verifier already has; loses some recall on edge phrasings the embedding retriever alone would have surfaced (rule engine is stricter); the `intent_class` judgment itself remains LLM-fallible — bounded by closed-class override, not eliminated.
- *Cost:* 1 sprint catalog cleanup + 2 sprints Rule Maker at retrieval = 3 sprints. Validation-heavy.
- *Risk:* Risk that the structured output schema for the rerank LLM needs revision after Slice 3 validation if the model cannot reliably emit `chosen_intent_class` under strict-mode JSON — adds one sprint. Pattern itself is proven (verifier shipped on this discipline); substrate (DB, gateway, policy layer) already exists.

**Option D — Continuous-eval flywheel.** Production traffic logs verdicts; thresholds and principle text are re-calibrated weekly.
- *Addresses:* Property 3 slowly, by reducing variance over time.
- *Trade-off:* Requires production traffic to exist. Doesn't help pre-launch. Stationary failures (UC-99 weak rows, UC-1 duplicate row) don't get fixed by re-calibration; they get fixed by data hygiene.
- *Cost:* 1 sprint harness + ~0.5 sprint per quarter for taxonomy upkeep and harness drift, ongoing.
- *Risk:* Risk that eval substrate becomes its own maintenance burden — ~0.25 FTE ongoing for harness drift, principle text upkeep, and threshold re-calibration. Orthogonal to A/C: complements them but does not replace either.

**Option E — Reframe routing as intent-class primary, retrieval as tiebreaker.** Today, routing is retrieval-first: every query hits hybrid embedding+FTS+trigram retrieval over `uc_capabilities.principle_description`, and the top-N candidates are reranked. Option E inverts the primary mechanism: a closed-class LLM intent classifier (≤15 classes — `summary`, `lookup`, `list`, `action`, `conversational`, `field_read`, ...) is the primary routing decision; the registered UCs are partitioned by intent class at registration time; retrieval is invoked only as a tiebreaker within the candidates of the chosen class. Concretely: query → Pass 1 LLM emits `intent_class` (closed-class enum) + entity hypothesis → Pass 2 rule engine applies closed-class override on `intent_class`, looks up `UCs(intent_class)` (small-N, typically 1-5) → if N=1, route directly; if N>1, retrieve+rerank within that small set.
- *Addresses:* Property 3 at the root. The finite-principle-token / infinite-phrasing-surface asymmetry diagnosed in (a) becomes much less damaging when retrieval is scoped to ≤5 already-semantically-close candidates rather than the full UC catalog. Reinforces Property 1's open instance (ISS-005-shape failures) because `intent_class` is a closed-class LLM judgment — the kind two-pass + override handles reliably.
- *Trade-off:* Gains structural reduction in retrieval search space and a smaller, eval-able LLM judgment surface (10-15 classes vs the open UC set growing toward 1000); loses the embedding retriever's ability to surface "this query doesn't fit any registered intent class but is semantically close to UC-X" — those queries now route to clarification rather than soft-matching. Requires a taxonomy commitment: intent classes are a product-level decision, not just engineering, and every new UC must declare a registered class.
- *Cost:* 1 sprint for taxonomy + UC manifest migration (each UC declares its intent class). Couples to C's Slice 3 — the Pass 1 classifier in C and the intent classifier in E are the same LLM call.
- *Risk:* Risk that the intent taxonomy needs revision when a new UC class doesn't fit — taxonomy is a product commitment, and a botched taxonomy is harder to roll back than a botched scoring threshold (UCs would need re-classification across the registry).

## (c) Recommendation: Option C+E — intent classification primary, retrieval as tiebreaker

**Make the LLM intent classifier the primary routing decision. Partition the UC registry by intent class at registration time. Invoke retrieval only as a tiebreaker when `UCs(intent_class)` returns N > 1. Catalog cleanup is the prerequisite slice. The Rule Maker discipline that landed at the verifier — Pass 1 LLM judgment + Pass 2 Python rule engine with closed-class override — is the substrate for the new routing decision.**

This restructures routing around the smallest reliable LLM-judgment surface (a closed-class enum of ≤15 intent classes) and uses retrieval only where it has the best signal-to-noise: among ≤5 already-semantically-close candidates. It is the right answer for production ITSM at 1000-UC scale.

Defending specifically:

1. **The pattern is proven at the verifier.** ISS-001/002/003/005 taught that "LLM judgment + Python invariants" is the only architecture that converges on closed-class fields. The verifier ships with 80 deterministic rule tests, an override path, and a closed-class enum guard. `intent_class` is a closed-class field with ≤15 values, fitting exactly that shape — Pass 1 LLM classifies, Pass 2 Python validates membership and looks up eligible UCs. Applying a proven pattern is cheaper and safer than inventing a new one.

2. **Intent classification scales where retrieval scoring does not.** Section (a) named retrieval's failure mode: a finite principle-token surface trying to cover an infinite phrasing surface. That problem grows with the UC catalog. Intent classification does not — the class set is bounded (≤15), eval-able exhaustively, and small enough that prompt+override discipline keeps hallucinations at the ~ISS-003 rate (low single digits) that defense-in-depth handles cleanly. Retrieval, scoped to ≤5 same-class candidates, operates in its strong regime — among semantically-close alternatives where the token-surface asymmetry is much less damaging.

3. **It folds in the catalog cleanup that's blocking everything else.** UC-99's six weak rows, UC-3's alias rows, UC-1's duplicate row — these are not bugs in the scoring layer, they are bad inputs to it. No architectural change can outperform clean inputs. Catalog cleanup is Slice 1 because it's a prerequisite, not because it's cheap.

4. **It generalizes Property 2 to the gates not yet audited.** Gate A's `low_confidence` collapses "no candidates" and "weak candidates against a strong query" — the same shape as ISS-007 and ISS-012. Once `intent_class` is the primary routing decision, Gate A is restated as `UCs(intent_class) == 0`, a deterministic check that doesn't need scalar collapse. Same lineage as the ISS-012 fix.

**What the new pipeline looks like, concretely:**

```python
# Pass 1: LLM emits structured output, closed-class judgment only.
class RoutingClassification(BaseModel):
    intent_class: Literal["summary","lookup","list","action",
                          "conversational","field_read","other"]
    entity_hypothesis: list[str]          # ID-shaped tokens
    referring_focus: Literal["named","focus","none"]
    model_reasoning: str                  # audit only, never branched on

# Pass 2: Python rule engine. intent_class is the primary routing decision.
def resolve_route(c: RoutingClassification, state, registry) -> RouteDecision:
    # 1. Closed-class override — same defense-in-depth as ISS-003.
    if c.intent_class not in REGISTERED_INTENT_CLASSES:
        return RouteDecision(verdict="clarify", reason="unknown_intent")

    # 2. Lookup eligible UCs by class. Closed-class enum lookup,
    #    deterministic, ≤5 candidates by construction.
    candidates = registry.ucs_for_intent(c.intent_class)
    if not candidates:
        return RouteDecision(verdict="clarify", reason="no_uc_for_intent")

    # 3. Entity hypothesis validation — prefix → service_id contract.
    if c.referring_focus == "named":
        entities = [e for e in c.entity_hypothesis
                    if prefix_router.recognized(e)]
        if not entities:
            return RouteDecision(verdict="clarify",
                                 reason="entity_not_recognized")

    # 4. PRIMARY PATH: N=1 routes directly, no retrieval. The common case
    #    once intent_class is correctly classified.
    if len(candidates) == 1:
        return RouteDecision(verdict="route",
                             chosen=candidates[0], reason="single_class_uc")

    # 5. TIEBREAKER PATH: N>1 → retrieve+rerank within the small class
    #    set. The existing shortlister+reranker code runs here, scoped to
    #    `candidates` instead of the full UC catalog. ISS-012's intra-UC
    #    margin fix already handles same-UC closeness.
    return rerank_within(candidates, c, state)
```

Retrieval is no longer the primary routing mechanism. It is a tiebreaker within a constrained candidate set. The hybrid embedding+FTS+trigram substrate keeps working, but its role changes: it disambiguates among ≤5 same-class candidates instead of scoring the full catalog. The principle-text/phrasing asymmetry from (a) shrinks to a much smaller error budget. Cross-class ambiguity is structurally impossible — eligibility is closed by `intent_class`.

**What this gives up — three things, named clearly:**

1. *Embedding-rescue on out-of-taxonomy queries.* Today, a phrasing whose semantic neighborhood is close to UC-X but whose intent doesn't match any registered class would still surface UC-X via retrieval. Under the new pipeline, that query routes to clarification (`unknown_intent` or `no_uc_for_intent`). Soft semantic matching across the catalog is gone. This is the right trade for production — silent wrong-class routing is worse than an explicit "I'm not sure what you mean" — but it is a real loss for exploratory queries.

2. *Taxonomy as a product commitment.* The intent class set is small, but it's load-bearing. Every new UC must declare a registered class at `register_uc_handler` time. A botched taxonomy decision (wrong granularity, missing class, overlapping classes) is harder to roll back than a botched scoring threshold — UCs would need re-classification across the registry, and Slice 4's flag-flip becomes the safe rollback point. The taxonomy design is the highest-stakes decision in this proposal.

3. *Determinism on the `intent_class` judgment itself.* The classifier is an LLM. ISS-005 told us classifier failures by clause form are real and not fully prevented by prompting alone. The mitigations are (i) the Pass 2 closed-class override (ISS-003 pattern), (ii) an eval set per intent class (Slice 3a — pre-launch validation; Slice 5's production-traffic flywheel is the post-launch counterpart, Option D), and (iii) the existing closed-class override pattern adapted for `intent_class`. This is not "free determinism" — it is bounded determinism at the rule layer, with auditable failures at the classifier.

## (d) Migration path

Five slices. Each is a ship-and-validate cycle. Each leaves the system in a working state.

**Slice 1 — Catalog cleanup. (1 sprint.)**
- Drop UC-99 to a single `conversational` row in seed.
- Drop UC-3 alias rows (`kb_search`, `kb_lookup`, `open_kb`); keep canonical capabilities only. Add curated principles for `find_related_kb_for_incident` / `_for_ci`.
- Drop UC-1's `uc01_summarize` duplicate row.
- Delete `description / skills / goals` from `agent-catalog-registry.json`. **Architectural outcome (Slice 1 + Slice 2 together):** the three-sources-of-truth problem (handler registry / DB principle text / JSON catalog) is resolved by making `register_uc_handler` the canonical source. DB seed is downstream of the registry. JSON catalog's decorative fields are removed.
- File the missing ledger entries:
  - **ISS-008** (planner over-routes when entity dominates rewrite) — separate ticket, not closed by this migration.
  - **ISS-012** (rerank margin gate fix) — shipped; ledger entry pending.
  - **ISS-013** (Gate A low_confidence on novel phrasings — Probe 2 p2-f4 trigger) — **closed structurally by Slice 3.** Under C+E, Gate A is restated as `UCs(intent_class) == 0` (section (c) defense 4), a deterministic check rather than a scalar collapse.
  - **ISS-014** (bare-attribute rewriter not binding to focus — Probe 2 p2-f1 trigger) — **out of scope for this migration.** Rewriter is upstream of routing; the C+E architecture does not address it. File as a separate ticket for targeted rewriter work.
- **Probe 2 → slice mapping:** p2-f4 closes when Slice 3 lands (via ISS-013 structural close); p2-f1 needs a separate rewriter ticket (ISS-014) outside this migration.
- **Validation:** Re-run 70-batch + Probes 1-5. Expect ~3-5% absolute improvement in executed rate purely from input cleanup. No regressions.
- **Rollback:** Seed table is idempotent; re-run prior seed reverts.

**Slice 2 — Add intent-class taxonomy and per-UC declaration. (1 sprint.)**
- Define closed-class intent set: `summary`, `lookup`, `list`, `action`, `conversational`, `field_read`, `other`. ≤10 classes. Pin to design doc.
- Each UC declares supported intent classes in its `register_uc_handler` manifest. Migrate the seed script to require it.
- **Source-of-truth resolution:** `register_uc_handler` is canonical. **YAML manifests are explicitly *not* the resolution** — reviewers should not expect a per-UC YAML file. The runtime registry stays the single source.
- **Drift detection mechanism:** DB seed is generated from the runtime registry at deploy time (seed script reads `_handlers` and writes `uc_capabilities`). If the registry and the live DB diverge, the next deploy resets the DB. CI runs the seed in `--dry-run` mode and fails the build if the proposed diff is non-empty against a freshly-seeded test database — i.e. the registry must be the only writer.
- No routing change yet. Intent class is metadata, not yet consulted by the router.
- **Validation:** Schema-test that every registered UC declares ≥1 intent class. CI drift check passes. No behavioral change expected.
- **Rollback:** Drop the field from the manifest; routing ignored it anyway.

**Slice 3 — Rule Maker at routing (intent classification primary). (2 sprints.)**

**Graph topology change.** Before: `rewriter → verifier → shortlister → reranker → planner`. After: `rewriter → verifier → classifier → resolver → planner`. The shortlister and reranker are removed as graph nodes. Their code is preserved as an internal function `rerank_within(candidates, classification, state)` that the resolver invokes only when `len(candidates) > 1`.

- Implement `classifier_node` (Pass 1): one LLM call producing `RoutingClassification` (closed-class `intent_class` + entity hypothesis + referring focus), via gateway's strict-mode JSON schema. Replaces no existing node; sits after the verifier.
- Implement `resolver_node` (Pass 2): the `resolve_route` Python rule engine (~150 LOC) from section (c). Replaces the shortlister and reranker as separate nodes.
- `rerank_within` is a function on the resolver, not a graph node. Internally it calls the existing shortlister + reranker code paths against the small candidate set returned by `registry.ucs_for_intent(c.intent_class)`. ISS-012's intra-UC margin fix continues to apply inside `rerank_within`.
- **Slice 3a — Eval set per intent class (within Slice 3 budget):** before flag flip, construct ~150-200 labeled examples per intent class (curated, version-controlled in `tests/eval/intent_class/`). This is the pre-launch safety net for the LLM-fallibility trade-off named in section (c). See Slice 5 for the production-traffic flywheel (separate artifact).
- Behind a flag (`ROUTING_LAYER=rule_maker` vs `legacy`) for the entire sprint.
- **Validation:**
  - Flag-off = byte-identical to today.
  - Flag-on = re-run unit suites, 70-batch, Probes 1-5, plus a new `tests/unit/test_resolve_route.py` (target ~30 deterministic rule tests, mirroring `test_verifier_rules.py`). Phase H 8/8 mandatory.
  - **Eval-set gate:** classifier accuracy ≥95% on each intent class's eval set. This is the gating criterion for Slice 4's flag flip.
- **Rollback:** Flag off. Code stays; behavior reverts. Shortlister + reranker code paths are preserved (now as `rerank_within`'s internals), so flag-off re-uses them in their old graph-node position via a thin compatibility shim.

**Slice 4 — Promote intent-class to default + retire weak shortlist paths. (1 sprint.)**
- Flag default → `rule_maker`. Legacy path stays accessible for emergency rollback for 2 weeks, then removed.
- **ISS commitment (resolved, not hedged):**
  - **ISS-013** (Gate A low_confidence) — closed structurally by Slice 3, per section (c) defense 4 (Gate A becomes `UCs(intent_class) == 0`, a deterministic check). No separate fix needed; mark `fixed` in ledger when Slice 4 lands.
  - **ISS-014** (rewriter bare-attribute binding) — out of scope for this migration. Files a separate ticket for upstream rewriter work; not blocked on this slice, not unblocked by it.
- **Validation:** classifier accuracy on the Slice 3a eval set held at ≥95% per class for the duration of the soak; 2-week production / staging soak watches end-user outcome rates (executed / clarification / no_match) against the pre-flag baseline. The Slice 5 production-traffic flywheel is *not* yet built; Slice 4 validates against the eval set (Slice 3a) plus baseline-diff metrics from existing telemetry.
- **Rollback:** Flag off; revert is one config line.

**Slice 5 — Production-traffic flywheel (Option D). (1 sprint, then ongoing.)**

The eval substrate has **two artifacts** with different purposes — do not conflate them:

- **(a) Eval set per intent class** — built in Slice 3a, *before* Slice 4 flag flip. ~150-200 labeled examples per class, curated and version-controlled. Used as the pre-launch gate criterion (≥95% per-class accuracy required before flag flip). Already covered by Slice 3.
- **(b) Production-traffic flywheel** — built in this slice (Slice 5), *after* flag flip. Post-launch drift detection from real query logs. Ongoing.

This slice builds **(b)**:

- Production routing decisions logged with classifier verdict, rule-engine decision, end-user outcome (executed / clarification / no_match). Schema and storage designed in this slice (see open question 7).
- Weekly review job surfaces drift: intent classes where rolling 7-day accuracy drops below 90%, principles where retrieval scores collapsed, novel phrasings producing `unknown_intent`.
- **Thresholds (committed):**
  - Gate criterion for Slice 4 flag flip (eval set, Slice 3a): **≥95% correct classification per class.**
  - Operational threshold for Slice 5 flywheel (production traffic): **escalate to engineering when any intent class falls below 90% on a rolling 7-day window.** Engineering response: review misclassified samples, update prompt or principle text, add to eval set, re-validate.
- **Validation:** Logging schema lands; weekly review job runs; alerting wired to the 90% rolling threshold. Ongoing.
- **Rollback:** N/A — pure observability layer.

Total: 6 sprints. Critical path is Slice 1 → Slice 3a. Slice 2 carries the highest-stakes single decision in the proposal (the intent-class taxonomy) plus the drift-detection CI gate. Slice 4 is the flag flip with the eval-set accuracy gate (≥95% per class) and a 2-week soak. Slice 5 is the post-launch operational substrate.

## (e) Open questions

1. **Intent-class taxonomy.** Listed ≤10 classes. What's the actual count? Should `list` be its own class or a sub-mode of `lookup`? Should `field_read` be a class or only a UC capability within `summary`? This needs design before Slice 2.

2. **Conversation-control intent.** Confirmation responses ("yes", "the first one"), corrections ("no, the other one"), topic-closures ("thanks, that helps") — do they belong in their own intent class, or are they handled by the verifier-output state and never reach routing? Component 5 (topic-closure) intersects.

3. **Multi-intent queries.** A query containing N sub-queries (v4's stated product shape) — does each sub-query get its own intent classification, or does the decomposer choose a primary intent? Phase 5 (Send fan-out) intersects.

4. **Backwards compatibility for the legacy path.** Slice 4 removes legacy after 2 weeks. Is 2 weeks the right soak window, or should it be 4? Production traffic volume determines this.

5. **The verifier's place in the new pipeline.** Today: verifier runs before shortlist. After this change: should the verifier still run as a separate stage, or does Pass 1 of `RoutingClassification` subsume it? They overlap on entity hypothesis extraction. Worth thinking about whether to merge or keep separate. Risk: merging is convenient but couples two different judgment problems (ambiguity vs intent) into one prompt. Probably keep separate; revisit in Slice 3.

6. **ISS-005 follow-through.** The Rule Maker Pattern at the verifier did not catch ISS-005 — imperative-pronoun classification. Pass 2 of the new retrieval layer should include the same closed-class pronoun-token override (ISS-003 lesson). Should it land here, or as its own slice?

7. **Eval substrate — two artifacts, two purposes.** The migration distinguishes (a) eval set per intent class — pre-launch validation that classifier meets the ≥95% threshold per class before Slice 4 flag flip; ~150-200 labeled examples per class, curated, version-controlled; and (b) production-traffic flywheel — post-launch drift detection from real query logs, ongoing weekly review against the 90% rolling-7-day threshold. **Open question:** who owns curation of (a) — engineering, product, or a labeling team? What's the refresh cadence as the taxonomy evolves (every Slice 2 taxonomy revision invalidates some eval examples)? Storage schema for (b) needs design before Slice 5.

8. **Confidence signal on `RoutingClassification`.** The current design omits a confidence/uncertainty field on the Pass 1 output. Should `RoutingClassification` emit a confidence signal between two close intent classes, or is closed-class enum membership sufficient?
   - *Argument for omission (current design):* LLM self-reported confidence is unreliable and tends toward overconfidence on wrong answers; the eval set per class (Slice 3a) catches stability empirically; closed-class enum membership is binary by construction.
   - *Argument for inclusion:* low-confidence cases (genuine ambiguity between e.g. `list` and `summary`) could route to clarification rather than executing on a marginal classification; Pass 2 would add a rule branch for `confidence < threshold → clarify`.
   - *Resolution path:* defer until Slice 3a eval-set construction surfaces whether per-class accuracy is the bottleneck (no confidence needed) or whether between-class uncertainty drives the residual error (confidence needed). Decide before Slice 4 flag flip.

---

**Honest finding:** the gate-by-gate strategy was not wrong — Properties 1 and 2 were genuinely point-fix problems and the fixes worked. Property 3 is structurally different and needs the architectural commitment above. This is not a rebuild; it is the last application of a pattern that has already been validated at the verifier.

The recommendation is specific. The cost is six sprints. The risk is bounded by feature-flagged rollout. The eval substrate is the long-term safety net.

Awaiting review.
