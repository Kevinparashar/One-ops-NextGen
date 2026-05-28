# ISS-013: Gate A returns low_confidence on novel field phrasings

**Trigger:** Probe 2 `p2-f4` (2026-05-18) — `"What category does this fall under?"` after `"I need details for INC0001002"`. The rewriter correctly resolved `this → INC0001002`, but the shortlister returned no candidate above Gate A's confidence threshold; rerank never ran. Result: `final_status=clarification_required`, `gate_verdict=low_confidence`.

**Wrong behavior:** Gate A's `low_confidence` verdict fires identically for two structurally different cases:
1. The user's query genuinely doesn't map to any registered UC (legitimate "no_match" / clarify).
2. The query maps clearly to a registered UC, but the phrasing's token surface doesn't overlap the UC's `principle_description` strongly enough to score above Gate A's threshold.

Case 2 is what `p2-f4` exhibits. UC-1's `field_read` principle enumerates representative fields (`"priority, status, assigned owner, SLA, parent problem, related changes, approval state..."`) but the phrasing `"what category does X fall under"` shares neither the field-name tokens (`category` isn't listed) nor a strong embedding neighborhood with that enumeration. The candidate either fails to surface or surfaces below threshold.

**Right behavior:** Gate A should distinguish the two cases. "No candidates at all" → clarify. "Weak candidates against a strong query" → still attempt to route based on structural state, not on the collapsed scalar.

**Root cause: same lineage as ISS-007 and ISS-012 — routing on a collapsed signal.** Gate A's `low_confidence` scalar collapses "no candidates" and "weak retrieval against a well-formed query" into one verdict. The retrieval substrate's fragility on natural phrasings (Property 3 in the architectural review) is the *underlying* failure; Gate A's collapsed signal makes it indistinguishable from a legitimate no-match.

This is documented as **Property 3 (retrieval/scoring fragility on natural phrasings)** in `docs/design/routing-layer-architectural-review.md` section (a). The mechanism: the shortlister is hybrid retrieval (embedding + Postgres FTS + trigram) over `principle_description`, and all three signals reward token / sub-token overlap. Principle text is a *finite token surface* trying to cover an *infinite phrasing surface*; novel phrasings whose semantics are well-defined but whose tokens don't overlap fall below threshold.

**Fix:** **Closed structurally by the C+E architectural migration, Slice 3.** No targeted gate-by-gate fix is being filed.

Under the C+E recommendation (intent classification primary, retrieval as tiebreaker), Gate A is restated as `UCs(intent_class) == 0` — a deterministic check on whether the intent class has any registered UCs at all. The retrieval-scoring fragility that causes `low_confidence` today is moved out of the gate decision: retrieval only runs as a tiebreaker among ≤5 already-semantically-close candidates, where the principle-text / phrasing-surface asymmetry is much less damaging. See section (c) defense 4 of the design doc.

**Test pinning:**
- Pre-fix repro: `tests/demo/probe2_iss012.py` (`p2-f4`) — currently fails with `clarification_required`, `gate_verdict=low_confidence`.
- Post-Slice-3 expected: `p2-f4` executes via `field_read` after intent classification routes to the `field_read` intent class. Validation gate for closing this issue is Probe 2 re-run on Slice 3 build with `p2-f4` returning `executed`.

**Status:** **deferred-pending-C+E-migration.** Will be marked `fixed` when Slice 3 of `docs/design/routing-layer-architectural-review.md` lands and Probe 2 re-run confirms structural close. Until then, the symptom routes to clarification — degraded UX, not a wrong-answer failure mode.

**Related issues:**
- **ISS-007** — verifier boolean collapsed two cases; same lineage.
- **ISS-012** — rerank margin scalar collapsed two cases; same lineage.
- **ISS-014** — also a Probe 2 trigger, but separately scoped (rewriter problem, not routing).

**Generalization:** **Retrieval scoring over principle text rewards token overlap with a finite enumeration; user phrasings span an open set.** Any gate that fires when retrieval score crosses a threshold will surface this asymmetry as failure. The architectural fix is to reduce the surface area retrieval has to score — exactly what the C+E recommendation does. Gate-by-gate threshold tuning is the wrong shape; the substrate needs the structural change.

**Link to architectural commitment:** `docs/design/routing-layer-architectural-review.md`. ISS-013 is named in section (d) Slice 1 (ledger filing) and Slice 4 (closure when flag flips). Closes structurally per section (c) defense 4.
