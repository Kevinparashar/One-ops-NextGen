# Slice 1 — Catalog Cleanup: Implementation Prep

**Status:** Prep only. Not started. Sprint planning happens separately.
**Parent design:** `docs/design/routing-layer-architectural-review.md` section (d) Slice 1.
**Effort budget:** 1 sprint. This prep verifies the work fits inside that envelope.

## Scope (exact)

Three catalog edits + one JSON-catalog cleanup + four ledger entries already filed.

### Edit 1 — UC-99 collapse to a single `conversational` row

**File:** `tools/seed_uc_capabilities.py`
**Where:** `_PROGRAMMATIC_UC_DESCRIPTIONS["conversational_agent"]` (lines ~108-122)
**Today:** dict with one key (`conversational`) — but the seed walks `_handlers["conversational_agent"].capability_to_intent` which has 7 keys (`conversational, greet, thank, goodbye, help, capability_inquiry, confusion`), producing 7 rows in `uc_capabilities`, only 1 with curated principle.

**Two-part change:**
1. **Manifest (`src/oneops/use_cases/uc99_conversational/__init__.py`):** collapse `capability_to_intent` to a single entry `{"conversational": "conversational"}`. Handler internals continue to recognize the 6 sub-intents (`greet`, `thank`, `goodbye`, etc.) for response styling — those are handler-internal, not routing-exposed.
2. **Seed:** no edit needed; once the manifest collapses, the seed's Pass 1 over `_handlers` will emit only one row for UC-99.

**Net effect on `uc_capabilities`:** 7 UC-99 rows → 1 UC-99 row.

### Edit 2 — UC-3 drop alias rows, add curated principles for missing canonicals

**File:** `tools/seed_uc_capabilities.py` (`_PROGRAMMATIC_UC_DESCRIPTIONS["kb_lookup_agent"]`, lines ~123-161) **+** `src/oneops/use_cases/uc03_kb_lookup/__init__.py`

**Today:** UC-3's manifest declares 8 `capability_to_intent` keys (`lookup_kb`, `kb_search`, `kb_lookup`, `find_related_kb`, `find_related_kb_for_incident`, `find_related_kb_for_ci`, `field_read`, `get_kb_article`, `open_kb`). 4 have curated principles; 4 are weakly-described aliases.

**Two-part change:**
1. **Manifest:** keep `capability_to_intent` as-is (alias-to-canonical map is useful for planner output normalization — no behavioral change to handler dispatch). Drop alias keys from the seed run, *not* from the manifest.
2. **Seed:** change `_PROGRAMMATIC_UC_DESCRIPTIONS["kb_lookup_agent"]` so that only canonical capabilities have entries: `lookup_kb`, `find_related_kb`, `find_related_kb_for_incident`, `find_related_kb_for_ci`, `field_read`, `get_kb_article`. Add curated principles for the two `find_related_kb_for_*` capabilities currently using the auto-generated handler fallback.
3. **Seed:** add a small filter in Pass 1 — when walking `manifest.capability_to_intent`, only emit a `uc_capabilities` row for capability keys that resolve to *distinct* intent values (the canonical set). Alias keys whose intent value is already covered by a canonical row are skipped.

**Net effect:** 8 UC-3 rows → 6 UC-3 rows, all with curated principles. (Drops `kb_search`, `kb_lookup`, `open_kb`.)

### Edit 3 — UC-1 drop `uc01_summarize` duplicate row

**File:** `tools/seed_uc_capabilities.py` + `src/oneops/use_cases/uc01_summarization/__init__.py`

**Today:** `capability_to_intent` has 3 keys: `summary → summary`, `uc01_summarize → summary`, `field_read → field_read`. Seed emits 3 rows; `summary` and `uc01_summarize` have identical-intent principles competing in retrieval.

**Two-part change:**
1. **Manifest:** keep `capability_to_intent` as-is (the alias normalizes planner output that uses the older `uc01_summarize` form).
2. **Seed:** same filter as Edit 2 — alias keys with already-covered intent values get skipped. `uc01_summarize` row is no longer emitted.
3. **Seed:** drop `uc01_summarize` from `_PROGRAMMATIC_UC_DESCRIPTIONS["summarization_agent"]` (lines ~180-185).

**Net effect:** 3 UC-1 rows → 2 UC-1 rows.

### Edit 4 — JSON catalog decorative-fields cleanup

**File:** `registries/agent-catalog-registry.json`

**Today:** every agent has `description`, `personal`, `goals`, `skills`, `ingress_binding`. None are read by routing — the shortlister consumes `uc_capabilities.principle_description` from the DB. These JSON fields drift silently against the seed's curated text.

**Change:** delete `personal`, `goals`, `skills` from every agent entry. Keep `description` if any tool / dashboard reads it (verify via grep before deleting — see Open Prerequisite 1 below). Keep `ingress_binding` (used by orchestrator).

### Edit 5 — Drift detection scaffolding (carryover from Slice 2)

**Defer to Slice 2.** Slice 1 lands the catalog cleanup; Slice 2 adds the CI drift check. Mentioning here only so reviewers know it isn't being dropped — see Slice 2 in the design doc.

## Test surface

### Pre-edit baseline (record now)

Re-run before any edit, capture numbers:
- `tests/demo/batch_scenarios.py` — full 70-scenario batch, current 151/194 = 77.8% executed.
- `tests/demo/probe1_iss012.py` — 5/5 currently.
- `tests/demo/probe2_iss012.py` — 3/5 currently (p2-f1 + p2-f4 fail).
- Phase H (`tests/stress/phase_h_*`) — currently 8/8.
- Unit suite — `pytest tests/unit/ -q` baseline.

### Post-edit gates

**Must pass without regression:**
- Unit suite: same count as baseline, all passing.
- Phase H: 8/8.
- `tests/unit/test_rerank_margin_gate.py`: 7/7 (ISS-012 fix preserved).

**Expected improvement:**
- 70-batch: ~3-5% absolute lift in executed rate (per design doc Slice 1 expectation). Specific anticipated wins:
  - UC-99-routed conversational turns: fewer false-positive UC-99 rows competing → cleaner shortlist.
  - Cross-UC borderline cases where a UC-3 alias row or UC-1 duplicate row used to muddy the rerank: cleaner decisions.
- Probe 1: still 5/5.
- Probe 2: **unchanged or marginal**. The two failing cases (p2-f1, p2-f4) are upstream-of-routing (rewriter) and substrate (retrieval-scoring fragility on novel phrasings) respectively. Slice 1 catalog cleanup does *not* close ISS-013 or ISS-014; those wait for Slice 3 and a separate ticket.

**Drift sanity check:**
- Run `seed_uc_capabilities.py --dry-run` and verify the proposed row set: UC-1 = 2 rows, UC-3 = 6 rows, UC-99 = 1 row. (Other agents from JSON registry remain on their current row counts.)
- Verify `uc_capabilities` row count drops by exactly 7 + 3 + 1 = 11 active rows after running with `--force`.

### Rollback

Pure data change. Re-running the previous seed script restores the prior catalog. No graph topology or code-path change in Slice 1, so rollback is one seed re-run plus reverting the 3 manifest `__init__.py` edits.

## Effort estimate (within 1-sprint budget)

| Task | Effort |
|---|---|
| Manifest edits (3 files) — UC-1, UC-3, UC-99 `capability_to_intent` changes | 0.5 day |
| Seed script edits — alias-filter logic + drop `uc01_summarize` entry + add 2 UC-3 curated principles | 1 day |
| JSON catalog cleanup — grep usage, delete decorative fields | 0.5 day |
| Baseline recording (run all gates pre-edit) | 0.5 day |
| Validation run (post-edit gates + dry-run sanity check) | 1 day |
| Drift-check stub (carry to Slice 2) — placeholder test | 0.5 day |
| Buffer for unexpected regressions on the batch | 1 day |
| **Total** | **~5 days = 1 sprint** |

Comfortably inside the 1-sprint budget. Buffer is real, not nominal.

## Open prerequisites / blockers

1. **Grep `agent-catalog-registry.json` consumers.** Before deleting `personal / goals / skills`, confirm no tool / dashboard / runbook reads them. The doc claims they are decorative; verify with `grep -r '"personal"\|"goals"\|"skills"' src/ tools/ tests/ docs/`. If any consumer surfaces, decide: delete and update consumer, or keep the field. **~30 min to verify.**

2. **Alias-filter semantics.** The seed-filter for "alias keys whose intent value is already covered" needs a tiebreaker rule: if two capability keys both map to the same intent and *neither* has a curated principle, which is canonical? Proposed rule: prefer the key whose name matches the intent value (so `summary → summary` wins over `uc01_summarize → summary`); fall back to alphabetical. Document in seed script comments.

3. **UC-99 sub-intent behavior preservation.** UC-99's handler internally branches on `greet` / `thank` / `goodbye` / etc. for response styling. Collapsing `capability_to_intent` to `{"conversational": "conversational"}` means the planner emits `conversational` as the capability and the handler must derive the sub-intent from the message text itself. Verify the handler does this already (likely yes — see `src/oneops/use_cases/uc99_conversational/node.py`) before landing the manifest collapse. **~30 min to verify.**

4. **No prerequisite on Slice 2/3.** Slice 1 ships standalone. Slice 2 (intent-class manifest field) doesn't depend on Slice 1, but ordering is correct: clean catalog first, then add taxonomy on top.

## What this slice does NOT do

- Does not introduce `intent_class` field on `register_uc_handler`. That's Slice 2.
- Does not change routing graph topology. That's Slice 3.
- Does not flip routing behavior. Catalog cleanup is data-only; the existing routing pipeline reads cleaner rows.
- Does not close ISS-008, ISS-013, or ISS-014. ISS-008 and ISS-014 are separate tickets; ISS-013 closes structurally in Slice 3, not Slice 1.
- Does not address Probe 2's failures. Those are Property 3 substrate failures; Slice 1 reduces noise in the retrieval input but doesn't fix the asymmetry.

## Deliverables when Slice 1 lands

- 3 manifest edits, 1 seed script edit, 1 JSON catalog edit.
- Pre/post 70-batch numbers recorded in a closeout doc at `docs/findings/slice-1-catalog-cleanup-closeout.md`.
- 4 ISS files updated where appropriate: ISS-012 status confirmed `fixed` and ledger linked; ISS-013/014/008 unchanged (still active / deferred per their files).
- `docs/planning/phase-status.md` row count for `uc_capabilities` updated.
- This file moved to `docs/findings/` (or annotated as "completed") to reflect that prep is consumed.

---

**Decision pending:** when does Slice 1 ship — this sprint or next? That's the sprint-planning decision, separate from this prep.
