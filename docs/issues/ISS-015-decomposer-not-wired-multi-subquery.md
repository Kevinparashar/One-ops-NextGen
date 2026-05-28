# ISS-015: LlmDecomposer not wired in the API factory; multi-sub-query messages collapse to one plan step

**Trigger:** UC-1 multi-sub-query battery (2026-05-26) — 16 patterns
covering compound messages ("summarize INC0001001 and INC0001002",
"priority of INC0001001, status of REQ0002001, owner of AST0001001",
"compare INC0001001 and INC0001002"). Result: **PASS=5 / PARTIAL=11 /
FAIL=0**. Every PARTIAL shares the same fingerprint — `step_results`
length is 1, and only the first entity ID appears in the final reply.

**Wrong behavior:** Compound user messages that carry distinct jobs are
resolved as a single sub-query. The router produces one plan step, the
executor runs one UC against the first ID, the aggregator stitches a
one-entity reply. The user's second/third request is silently dropped
— no error, no clarification, no acknowledgement of the missed work.

**Right behavior:** A compound message is split into N atomic
sub-queries, each routed independently through the funnel; the plan
contains N steps (subject to dependency edges); the aggregator returns
a single reply that addresses every sub-query. Per v4 product shape
(`v4_single_engine_multi_subquery.md` in memory): "A query can contain
N sub-queries routed to N UCs. Cross-UC orchestration is Day-1."

**Root cause:** `src/oneops/api/app.py:263` constructs `Router(...)`
without a `decomposer=` argument. The Router constructor defaults to
`PassthroughDecomposer()` (see `src/oneops/router/decompose.py:51`),
which treats every message as one sub-query. The production class
`LlmDecomposer` exists in the same file (line 73), is fully prompt-,
policy-, and cache-shaped, and is never instantiated by the factory.

The router's sub-query→plan-step fan-out is already correct
(`src/oneops/router/router.py:125 for sq in subqueries`). The
aggregator already iterates `step_results`. The only break in the
pipeline is the missing wiring step.

**Generalization:** Whenever a production-grade LLM-backed seam exists
alongside a deterministic Passthrough sibling, the API factory's
gateway-up branch must select the LLM-backed seam — for *every* such
pair, not just the ones the original wiring author remembered. The
factory currently selects the LLM variant for Rewriter, Disambiguator,
and BoundaryResponder (three separate `if gateway:` branches), and
silently skips the matching Decomposer wiring. The future-proof shape
is one selection point that walks the seam list, so adding the next
seam (e.g. an LLM Glossary) cannot regress this way.

**Fix:**

1. In `app.py`'s gateway-up branch, instantiate
   `LlmDecomposer(gateway, model=chosen_model)` next to `LlmRewriter`.
2. Pass it to `Router(..., decomposer=...)`.
3. Add the same gateway-down fallback (`PassthroughDecomposer`) so
   tests without an LLM gateway keep their single-step semantics.
4. The router and aggregator do not need changes — the fan-out already
   works; this is a missing constructor argument, not a refactor.

**Out of scope for this issue:**
- Cross-UC sub-queries (sub1=UC-1, sub2=UC-3) — that depends on UC-3
  being data-backed (issue #20 PostgresKbStore).
- Bare-digit sub-queries ("0001001 and INC0001002") — that depends on
  Phase N (task #34).
- Targeted field-read sub-queries — UC-1 today returns the whole
  summary; field-read is a separate UC slot.

**Test pinning:**

- Pre-fix battery `/tmp/uc1_multisub_battery.py` records `steps=1` and
  `ids=(True, False, …)` on every multi-ID pattern.
- Post-fix expectation: ≥12 of 16 patterns PASS (two-INC, three-INC,
  mixed-type, comparative, two-fields-one-entity, three-field-reads).
  Remaining PARTIAL acceptable: those depending on Phase N or UC-3
  data (out-of-scope above).
- New unit test: factory-level — when `LLM_GATEWAY_URL` is set, the
  built Router's `_decomposer` is an instance of `LlmDecomposer`; when
  unset, it is `PassthroughDecomposer`. Catches future re-regressions
  of this exact wiring gap.
- New integration test: with the gateway up, "summarize INC0001001 and
  INC0001002" produces `len(plan.steps) >= 2` and both IDs appear in
  the aggregated reply.

**Devil's advocate (post-fix checks to run):**

- Decomposer hallucinates an extra sub-query → run "summarize
  INC0001001" alone; must still produce exactly 1 step (not 2).
- One sub-query fails / RBAC-denied → aggregator must report the
  failure for that sub-query, succeed on the others, no full-message
  abort.
- Five+ sub-queries — does the executor honor `EXECUTOR_MAX_STEPS` or
  similar attention budget? (Moveworks principle — graceful cap.)
- Cross-UC mix (sub1=UC-1 summarize, sub2=UC-3 KB) — confirm the
  service-compat filter (stage-3) still runs per sub-query, not on the
  whole message.
- Dependent sub-queries ("find oldest P3 and escalate it") — depends_on
  edges in `SubQuery` must round-trip through the plan-DAG; verify the
  executor honors execution order.

**Status:** active. RCA confirmed (file:line cited). Fix is a wiring
change of single-digit LOC plus tests; landed before this issue closes.

**Related issues:**

- [[v4_single_engine_multi_subquery]] — product principle.
- [[feedback_descriptions_principle_not_phrases]] — applies to
  `_DECOMPOSE_PROMPT`; current prompt already follows the principle.
- [[ISS-014]] — different rewriter wiring; not the same defect, same
  shape (Passthrough sibling left wired in production).
