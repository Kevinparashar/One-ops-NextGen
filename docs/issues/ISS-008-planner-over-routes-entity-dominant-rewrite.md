# ISS-008: Planner over-routes when entity dominates the rewritten query

**Trigger:** Multi-turn flows where the rewriter resolves a bare-attribute follow-up by substituting the focus entity into the rewrite (e.g. `"What's its priority?"` → `"What's the priority of INC0001001?"`). Once the rewrite is entity-dominant, the planner's UC selection over-weights the named entity and routes to the broadest UC capability whose principle mentions that entity type, even when the intent is narrower (a single field read, a related-record lookup, etc.).

**Wrong behavior:** Planner picks `summarize_entity` for queries the rewriter has rendered entity-dominant, regardless of the *narrower* intent expressed in the original (pre-rewrite) message. The entity-dominant rewrite biases retrieval and reranking toward "anything that mentions this entity type" rather than "the specific capability the user asked for." This surfaces most clearly on field-read intents that get routed as summaries, and on `find_related_kb_for_*` intents that get routed as generic `summarize_entity`.

**Right behavior:** Planner should route on the *original intent* of the message, with the entity as scope, not as the dominant signal. A bare-attribute follow-up like `"What's its priority?"` (rewritten to `"What's the priority of INC0001001?"`) should route to `field_read`, not `summarize_entity`. The rewriter's job is to bind a focus reference; the planner's job is to dispatch the intent. Entity dominance in the rewrite is a confound, not a routing signal.

**Root cause: rewriter-planner contract is ambiguous about which signal is load-bearing for routing.** The rewriter adds the entity to make the query self-contained for downstream agents, but the planner reads the rewritten query as if it were a fresh user message. The planner has no signal that the entity was *injected* by the rewriter rather than *named* by the user. Token-frequency-biased retrieval (the shortlister's hybrid embedding + FTS + trigram over `principle_description`) then surfaces UCs whose principle text overlaps the entity tokens — which biases toward `summary` capabilities since those principles enumerate entity types.

This is a contract-level problem, not a prompt-level one. It is upstream of routing-layer fixes (ISS-007, ISS-012, the C+E migration). Even with intent classification primary (post-Slice 3), the classifier reads the rewritten query, so the same entity-dominance bias can affect intent classification.

**Fix:** Not yet shipped. Two paths to consider when this is picked up:

- **Path 1 — preserve the original message alongside the rewrite.** The planner / classifier reads both: original for intent signal, rewrite for entity binding. Decouples the two concerns. Lower-risk; preserves rewriter's downstream contract for handlers that need the bound entity.
- **Path 2 — re-prompt the rewriter to mark which tokens were original vs injected.** Structured rewrite output: `{rewrite, entity_added, original}`. The planner / classifier consults the original tokens for intent and the rewrite for delivery. Higher-touch; cleaner contract.

Decision deferred to a separate sprint; not part of the C+E architectural migration.

**Test pinning:** No test pins this today. When the fix lands, the test surface should include:
- Multi-turn flows where T1 names the entity and T2 is a bare-attribute follow-up — planner must route T2 to the narrower capability (field_read), not to summary.
- Multi-turn flows where T1 names the entity and T2 asks for related KB — planner must route to `find_related_kb_for_*`, not to summary.

**Status:** **active** — repro available (multi-turn batch flows demonstrate the over-routing pattern); separate ticket; **not closed by the C+E architectural migration** (`docs/design/routing-layer-architectural-review.md`). The C+E migration may *reduce* the symptom because intent classification scopes the candidate set, but the underlying rewriter-planner contract issue remains.

**Related issues:** ISS-007 (verifier no_referent over-clarify — same lineage of "downstream stage reads upstream output without enough context"), ISS-014 (rewriter bare-attribute binding — adjacent rewriter contract issue, also out of architectural-migration scope).

**Generalization:** **When one stage transforms the user's message for a downstream stage, the downstream stage must know which signal is load-bearing.** Routing decisions read the *intent* of the original message; entity binding is delivery, not intent. Stages that emit transformed messages should preserve the discrimination between "user-named" and "system-injected" tokens, or split the output into separate fields for separate downstream concerns.

This is the same shape as ISS-007's generalization at the verifier ("routing on a boolean isn't enough when the same boolean fires for different reasons") applied at the rewriter-planner boundary. The cross-stage contract is the load-bearing surface.

**Link to architectural commitment:** `docs/design/routing-layer-architectural-review.md`. ISS-008 is named in section (d) Slice 1 as a separate ticket not closed by the C+E migration.
