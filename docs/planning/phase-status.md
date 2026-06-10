# OneOps v4 — Phase Status

**Last updated:** 2026-05-18 (**routing-layer architectural review APPROVED** — see `docs/design/routing-layer-architectural-review.md`; next-sprint priority is Slice 1 catalog cleanup)
**Source of truth:** this file. Conversation context evaporates; the repo doesn't.

> **Architectural commitment (2026-05-18):** the routing layer migrates to **C+E — intent classification primary, retrieval as tiebreaker** over 6 sprints. Slice 1 (catalog cleanup) is the next sprint's load-bearing work. The full 5-slice migration (catalog cleanup → intent-class taxonomy + drift detection → Rule Maker at routing with eval set per class → flag flip with ≥95% per-class accuracy gate → production-traffic flywheel) is committed in `docs/design/routing-layer-architectural-review.md`. Ledger entries ISS-008, ISS-012, ISS-013, ISS-014 filed in `docs/issues/`. The design doc is the canonical architectural reference; this file tracks shipping progress against it.

> **v2 package status: 4 of 7 components shipped (Components 1b + 4 + 6 are the foundation).**
> Components 1b (two-pass verifier), 4 (conditional edges in graph), and 6 (entity_ledger state channel) form the load-bearing foundation. Component 1b's downstream contract is now honored end-to-end: a real user message under `ROUTING_MODE=three_stage` flows through `rewriter → verifier → {proceed | auto_correct | clarify}`. Silent-wrong-answer cases (rewriter substituting wrong entity on ambiguous referents) are caught and routed deterministically. Legacy mode remains byte-identical (verifier doesn't run under `legacy`). Components 3 (real clarification node), 5 (topic-closure reducer), 2 (hybrid detectors), and 7 (UC-as-spec) are polish on top. See `docs/findings/2026-05-18-component-1b-c4-ship.md` for full close-out.

---

## Production state

| Component | Status |
|---|---|
| `ROUTING_MODE` production default | `legacy` (UC-1 path byte-identical to today) |
| `ROUTING_MODE=three_stage` | Staging-only opt-in, gated behind P-FLIP's 5 prerequisites. **Now includes verifier wiring (Component 4 of ambiguity-fix-package-v2).** |
| UC-1 (summarization) | Production-grade — v12 stress 100% real pass, 30-min soak passed |
| UC-3 (KB Lookup) | Production-grade — Phase 4 batch 1 shipped; runs under both routing modes |
| UC-99 (conversational) | Production-grade — autodiscovery registered |
| UC-2 (action UCs) | NOT BUILT — blocked on D1 (Send fan-out) then on D8 implementation |
| Verifier (1b) + entity ledger (6) + conditional edges (4) | **SHIPPED 2026-05-17/18 to `three_stage`** — does real work on routing traffic. 84 unit tests + 18 end-to-end + 3 legacy regression all green. |

---

## Shipped artifacts (durable, in the repo)

### Code
- `src/oneops/routing/decomposer.py` — Stage -1, structural-trigger gated, faithfulness post-check, over_cap-aware
- `src/oneops/routing/structural_trigger.py` (v1.0.2) — pure predicate, input-contract docstring (option C: no history)
- `src/oneops/routing/rewriter.py` — three-branch (pronoun deterministic / bare-attribute LLM / passthrough)
- `src/oneops/routing/uc_shortlister.py` + `prefix_router.py` — hybrid retrieval + RRF + prefix-boost
- `src/oneops/routing/uc_reranker.py` — LLM cross-encoder + Gate A + margin disambiguation
- `src/oneops/routing/uc_embedder.py` + `uc_capability_catalog.py` — capability index + reducer-safe
- `src/oneops/routing/nodes.py` — LangGraph node wiring under `three_stage`
- `src/oneops/graph/nodes.py:_maybe_partial_answer_note()` — two disclaimer paths with self-correcting invariants

### Tests & guards
- `tests/stress/_verdict_guard.py` — mechanical optimism-bias safeguard (atexit + sentinel + 4 exit-path coverage)
- Self-incriminating bypass: `_disable_guard_THIS_IS_A_BYPASS_ONLY_USE_IN_SELF_TEST`

### Docs / runbooks
- `docs/runbooks/state-channel-additions.md` — R1–R6 PR-review rules + three explicit untested-pattern caveats
- `docs/planning/phase-status.md` — this file
- `docs/design/phase-5-fan-out.md` — design committed; **implementation gated on ambiguity-fix package landing first**
- **`docs/design/ambiguity-fix-package-v2.md` — CANONICAL.** Research-grounded solution for 9 documented silent-failure modes. Supersedes v1 (kept for traceability). 7-component package (entity ledger + verifier model + per-UC clarification) estimated 10–16 weeks engineering. **Engineering reviewer should read v2, not v1.**
- `docs/findings/family3-ambiguous-referent-2026-05-17.md` — 6 ambiguity gaps with probe evidence
- `docs/findings/family1-focus-pivot-2026-05-17.md` — 3 focus-pivot gaps + latent clarification-text bug
- **`docs/issues/`** — canonical issue ledger. Start here when investigating new bugs that look like patterns we've seen. README explains the structure. Current entries: ISS-001 fake-CoT (fixed), ISS-002 priority-order (partial-fix), ISS-003 type_filter-hallucination (fixed), ISS-004 positional-ordinals (active), ISS-005 imperative-blocks-pronoun (deferred), ISS-006 prompt-design-discipline, ISS-008 planner-over-routes-entity-dominant (active, separate ticket), ISS-012 rerank-margin-gate (fixed 2026-05-18), ISS-013 gate-a-low-confidence (deferred — closes structurally by C+E Slice 3), ISS-014 rewriter-bare-attribute-binding (active, out of architectural-migration scope).
- **`docs/design/routing-layer-architectural-review.md`** — APPROVED 2026-05-18. Canonical architectural commitment for the routing layer. Six-sprint migration to intent-classification-primary routing with retrieval as tiebreaker.

---

## Task tracker — completed (17)

| # | Item |
|---|---|
| 1 | UC-1 cache layer (Dragonfly summary cache) |
| 2 | Port hallucination validator from POC3 |
| 3 | UC-1 field-read mode |
| 4 | UC-1 multi-turn focus continuation |
| 5 | Real LLM planner with registry-discovery tools |
| 7 | UC-3 KB Lookup end-to-end |
| 8 | Stress harness skeleton + initial probes |
| 9 | A1 — `add_messages` channel in OneOpsState |
| 10 | A2 — `compose()` takes `dynamic_context` |
| 11 | A3 — autodiscover UCs at `build_graph_async` |
| 12 | A4 — discovery union: `_handlers ∪ JSON registry` |
| 13 | B1 — UC-99 dynamic prompt + register |
| 14 | B2 — Smoke + stress rerun after Phase A+B1 |
| 15 | B3 — Planner multi-Send for multi-entity asks |
| 17 | B5 — Focus transition via `Command(update=...)` |
| 18 | C1 — RetryPolicy on planner + uc_executor |
| 20 | B-Phase2 — Focus reducer + `Command(update={"focus":...})` |

## Task tracker — open (17, with dependency edges)

| # | Item | Status | Blocked by |
|---|---|---|---|
| 6 | Dependency-aware execution (wave-based Send) | pending | — (rolled into Phase 5) |
| 16 | B4 — UC-1 Send fan-out for linked records | pending | — |
| 19 | B-Phase1 — `with_structured_output` for planner | pending | — |
| 21 | B-Phase3 — `interrupt()` for clarifications | pending | — (relevant to UC-2 / D8) |
| 22 | B-Phase4 — Promote scope/subject classifiers to graph nodes | pending | — |
| **23 (D1)** | **Phase 5: Send fan-out per sub-query in `three_stage`** | design committed, **implementation blocked** | **ambiguity-fix-package-v2 (Components 1–6)** — see canonical doc |
| **D12** | **Ambiguity-fix package v2 — Component 6 (entity ledger) + Component 1b (two-pass verifier) SHIPPED 2026-05-17** | partial — Components 6 + 1b shipped; Components 2/3/4/5/7 pending | — |
| D12.C2 | Component 2 — Hybrid ambiguity detectors (deterministic + LLM-based + state-based) | pending | D12 (1b) shipped — unblocked |
| D12.C3 | Component 3 — `clarification_node` with per-UC question building + fix the "uc01_summarization or uc01_summarization" same-UC-twice bug. **Design constraint (2026-05-18 research):** LangGraph's `interrupt()` causes the node to restart from the top on `Command(resume=...)`. Per LangGraph docs (https://docs.langchain.com/oss/python/langgraph/interrupts) and HITL cheatsheets: any code BEFORE `interrupt()` runs twice. The LLM call that generates the clarification question MUST execute AFTER `interrupt()`, not before — otherwise we double-pay for question generation on every clarification turn. Apply ISS-006 Rule 2 (contrastive examples) when designing the per-UC clarification question templates. | pending | D12 (1b) shipped — unblocked |
| D12.C4 | **Component 4 — SHIPPED 2026-05-18.** Conditional edges wired: rewriter → verifier_node → {shortlist (proceed) / auto_correct → shortlist / clarification_placeholder → aggregator}. Verifier always runs under three_stage (no cost mitigation per design decision). 4 state channels + 3 new nodes + 1 conditional edge. End-to-end smoke 18/18 (all 3 routing paths verified against Family 1 + Family 3 probes). Legacy regression 3/3 byte-identical. Path-B-shape migration smoke 14/14. **Component 1b's downstream contract is now honored end-to-end in the graph.** | shipped | — |
| D12.C5 | Component 5 — Topic-closure state reducer (LLM-based detector clears focus on "thanks") | pending | D12 (1b) shipped — unblocked |
| D12.C7 | Component 7 — Per-UC clarification declarations in UC-as-spec YAML manifest | pending | D12.C3 |
| D13 | Routing-layer cleanup: `request_ctx: dict` vs typed `contracts.schemas.RequestContext` inconsistency. Every LLM caller in `oneops.routing` uses `dict | None`; bridge envelopes use the Pydantic model. Reconcile during scaling work — not blocking. | pending | — |
| D14 | E1 classifier semantic preference (2026-05-17 1b.a finding): LLM classifies `"what about it?"` with empty ledger as `expression_type=no_referent` instead of `pronoun`. Both classifications route to clarify (verified). **Decision deferred until Component 4 is live** — observe real routing traffic to determine whether the preference matters in production OR add an Option-B prompt tightening "classify syntactically, not pragmatically." Routing-correctness is not affected; this is a label-preference question only. | pending | D12.C4 |
| D15 | 1000-UC scaling: verifier classifier principle enumerates known service_ids (`incident`, `change`, `problem`, `knowledge`) inline. At scale the registered set grows. Principle should reference a canonical registry (probably the same `service_prefixes` map the prefix-router reads) instead of enumerating types in prompt text. Defer until at least one new service-typed UC ships. | pending | — |
| D16 | Multi-entity explicit_id handling: v1 classifier schema's `referring_expression: str` is singular. Messages with multiple entity_id tokens (e.g. `"summarize INC0001001 and CHG0004007"`) currently set `referring_expression` to the first token only. Auto-correct path can only substitute one entity. Address in v2 — likely needs `referring_expression: list[str]` and per-entity routing. | pending | D12.C4 (signals when this matters in production) |
| 24 (D2) | Decomposer prompt tightening (eliminate LLM pronoun pre-resolution) | pending | 25 |
| 25 (D3) | Eval set expansion 22 → 100+ probes with paraphrase variants | pending | — (ready to start) |
| 26 (D4) | Continuous eval logging from production traffic | pending | 33 (P-FLIP) |
| 27 (D5) | Cosmetic: pronoun in G-MultiIntent disclaimer text | **deferred-with-rationale (2026-05-17)** — v1 ships with Option A (leave pronoun as-is). The disclaimer's trigger condition (sub-query drop) is rare in v1 because only UC-1 + UC-3 + UC-99 are built; the natural pronoun-bearing drop scenarios require UC-2 actions. Revisit when D8 lands and the disclaimer becomes common. | 30 (D8) |
| 28 (D6) | Migration runbook caveat coverage (per-channel-pattern verification) | pending | triggered on new-pattern PR |
| 29 (D7) | Live-graph version of G-MultiIntent P5/P6 | pending | 23 (D1) |
| 30 (D8) | UC-2 action UC (close/assign/update) with `interrupt()` | pending | 23 (D1) |
| 31 (D9) | Port dynamic-field resilience to UC-1 | pending | — (15-min task) |
| 32 (D10) | Decomposer adversarial probe set | pending | 25 (D3) |
| 33 (P-FLIP) | Flip `ROUTING_MODE=three_stage` to production default | pending | 24, 25, 26 |
| 34 (D11) | Close-out document linter (shipped vs deferred consistency) | pending | — (afternoon of work) |

---

## P-FLIP gate — five prerequisites to flip `three_stage` default

| # | Prerequisite | Status |
|---|---|---|
| P-1 | Continuous eval logging deployed in staging for ≥7 days under `three_stage` | Blocked (needs traffic; circular with P-FLIP gate itself — resolves via staging canary) |
| P-2 | Eval set expanded to ≥100 probes with paraphrase variants (D3) | Ready to start |
| P-3 | Decomposer prompt tightening reduces LLM pronoun pre-resolution rate below threshold (D2) | Blocked on P-2 |
| P-4 | Canary deployment in staging for ≥7 days under `three_stage` with zero disclaimer false-alarm complaints | Blocked on P-1 + P-2 |
| P-5 | UC-1 v12-stress + 30-min soak under `three_stage` shows 100% real-pass, no latency regression | Ready to run (single env flip in staging) |

All five must clear. **Flipping the flag without all five clear is a deploy-runbook violation.**

---

## Phase 5 — design first, code second

**Active phase as of 2026-05-17.** First deliverable: `docs/design/phase-5-fan-out.md` (this commit's next move). No code lands until that design is reviewed.

Six architectural surfaces to spec:
1. Per-sub-query focus scoping
2. Dependency edges between sub-queries (read→action)
3. Confirmation gates + mixed read+action wave handling
4. Aggregator stitching pattern with worked output
5. Partial-failure semantics (authz / UC-missing / LLM-error)
6. New state channels classified against runbook R1–R6

---

## Discipline carried forward

Every smoke must route through `_verdict_guard.print_verdict()`. Every new state channel applies the runbook's R1–R6. Every batch close-out separates shipped from deferred with explicit headers (D11 will mechanize this). No "polish later" framings — every deferred item has a prerequisite that unblocks it.

---

## Reviewer-role gap (structural finding 2026-05-17)

The project owner is product-side, not engineering. Technical-design surface-by-surface reviews require an engineering reviewer who can evaluate LangGraph state-channel semantics, reducer concurrency, async fan-out, and conditional schemas.

**Current state:** Phase 5 design (`docs/design/phase-5-fan-out.md`) is awaiting technical sign-off before step 5.2 and beyond can proceed. Step 5.1 (dict-merge verification probe) is independently runnable because it tests one assumption and locks in nothing else.

**For whoever inherits:** any future phase whose design surface includes load-bearing architectural decisions needs an engineering reviewer in the loop BEFORE implementation steps that depend on the design. The product owner can drive direction, hold discipline on optimism bias, and gate on outcomes — but should NOT be asked to review technical schema choices unaided. That gate belongs to a technical reviewer.
