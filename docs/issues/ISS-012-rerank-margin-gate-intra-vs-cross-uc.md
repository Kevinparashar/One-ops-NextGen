# ISS-012: Rerank margin gate over-fires on intra-UC capability closeness

**Trigger:** Live 5-turn demo flow + 70-scenario batch on 2026-05-18. T2 follow-ups like `"What's its priority?"` after `"Summarize INC0001001"` intermittently returned `clarification_required` instead of executing the field-read. Probe 1 flow `p1-f2` (CHG0001001 → "Who is it assigned to?") was the deterministic reproducer.

**Wrong behavior:** In `rerank_node` (`src/oneops/routing/nodes.py`), the margin gate fired identically for two structurally different cases:

1. **Cross-UC ambiguity** (e.g. UC-1 `field_read` vs UC-3 `kb_search` on close scores) — clarification is the right route.
2. **Intra-UC presentation choice** (e.g. UC-1 `summarize_entity` vs UC-1 `field_read` on close scores) — should execute; the UC handler chooses presentation.

The gate read `margin < MARGIN_THRESHOLD` (default 2.0) and routed to the aggregator (clarification path) without distinguishing whether the two candidates were from different UCs (real ambiguity) or the same UC (presentation closeness). Result: legitimate same-UC field-read follow-ups were clarified instead of answered.

**Right behavior:** Branch on the structural property `ranked[0].uc_id == ranked[1].uc_id`. Same-UC close call → `intra_uc_pass` verdict, route through. Different-UC close call → existing clarify path unchanged. The UC handler's own logic decides which capability to use when capabilities sit within a single UC.

**Root cause: routing on a margin scalar isn't enough when the same scalar fires for different structural reasons.** A numeric margin collapses two distinct routing decisions (cross-UC ambiguity vs intra-UC presentation choice) into one signal. The routing layer needs to read the *underlying structural property* — `uc_id` equality between top two candidates — not just the scalar.

This is the same generalization as ISS-007 (boolean signal collapsing two structurally-different verifier decisions). Both belong to **Property 2: routing on collapsed signal** in the architectural review.

**Fix (`src/oneops/routing/nodes.py` `rerank_node`):**

Added an `intra_uc_close_call` short-circuit before the existing margin gate. When the close-call preconditions hold AND `ranked[0].uc_id == ranked[1].uc_id`, emit `Command(update={"routing_gate_verdict": "intra_uc_pass", ...})` with no `goto`. Emits structured log `routing.rerank.intra_uc_skip`. The existing margin gate now also emits a symmetric `routing.rerank.cross_uc_clarify` log for observability parity.

Code location: `src/oneops/routing/nodes.py`, ~lines 442+. Verified clean separation: POC copy 4 has 0 occurrences of the new identifiers (strict read-only rule held); MVP has 8.

**Test pinning:**
- `tests/unit/test_rerank_margin_gate.py` — 7/7 PASS. Covers intra/cross × within/outside margin, log emission, single/empty candidates.
- `tests/demo/probe1_iss012.py` — Probe 1, 5/5 PASS. `p1-f2` explicitly hit the new `intra_uc_pass` path on real traffic, confirming the fix is load-bearing, not just unit-mocked.

**Status:** **fixed (Phase 1 shipped 2026-05-18).** Strict scope: fix lives in `Oneops-MVP/` only; POC copy 4 is read-only for this work cycle.

**Related issues:**
- **ISS-007** — same lineage: routing on a collapsed signal isn't enough when the same signal fires for different reasons. ISS-007 promoted the verifier's classifier expression_type; ISS-012 reads `uc_id` equality at the rerank boundary.
- **ISS-013** — Gate A's `low_confidence` is a third instance of the same collapsed-signal pattern (no candidates vs weak candidates against strong query). Closed structurally by the C+E migration's Slice 3, per `docs/design/routing-layer-architectural-review.md` section (c) defense 4.

**Generalization:** **When a single scalar or boolean signal collapses multiple structurally-different routing decisions, the conditional edge must read the underlying structural state, not the collapsed signal.** Any gate that fires on a single comparator should be audited for "does this comparator collapse cases that need different downstream paths?" — and if yes, the gate should branch on the structural state instead.

This is now a confirmed pattern (verifier in ISS-007, rerank margin in ISS-012, Gate A pending in ISS-013). The architectural review (section (a), Property 2) commits to applying the generalization to remaining unaudited gates as those failures surface.

**Link to architectural commitment:** `docs/design/routing-layer-architectural-review.md`. ISS-012 is referenced in section (a) Property 2 as a representative instance and in section (d) Slice 1 as the shipped fix.
