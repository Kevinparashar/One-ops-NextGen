# Capability-Class Routing — implementation plan

**Status:** proposal (no code changed yet)
**Author:** 2026-06-12
**Problem it solves:** terse/ambiguous queries (e.g. "database payroll issue") route
NON-DETERMINISTICALLY between uc02 (similar-tickets) and uc03 (KB) — the LLM
disambiguator coin-flips because both look valid, and the semantic cache then
freezes whichever way the coin landed. Root cause: the uc02 similar-by-text
feature widened uc02 to be a standing candidate for *any* problem statement, so
uc02 and uc03 now overlap for bare problems. (See session memory
`project_oneops_v11_routing_rag_session_2026_06_12`.)

**Why not the obvious fixes:**
- *Tune the cards/prompt* → whack-a-mole; can't make a true 50/50 deterministic.
- *Pairwise priority table (uc02 > uc03)* → O(n²), hand-authored; fine at 5
  agents, impossible at 500.

**The fix (scale-invariant):** resolve overlap between a small, fixed set of
**capability classes** ("kinds of need"), not between agents. The class set does
NOT grow with agent count, so it holds at 5 or 500 agents. This is the
hierarchical "classify by biggest differentiator first" pattern (Vonage intent
hierarchy; arXiv 2509.07571 two-layer routing; iCARE ontology-guided routing).

---

## 1. The taxonomy (data, ~5 values, bounded)

A new platform registry file: `registries/v2/platform/capabilities.json`.
Each entry is DATA: an id, a one-line PRINCIPLE for when a query is this kind
(not a keyword list — §2.1), and a `priority` used only to break a cross-kind
tie deterministically.

```json
{
  "version": 1,
  "capabilities": [
    {"id": "knowledge",        "priority": 50,
     "principle": "the user wants an ANSWER, how-to, explanation, or fix — including a bare problem/symptom stated with no other ask"},
    {"id": "record_retrieval", "priority": 40,
     "principle": "the user wants a SET of existing records handed back that match a described problem or a named record"},
    {"id": "record_summary",   "priority": 60,
     "principle": "the user wants the facts/status/fields of ONE specific identified record"},
    {"id": "fulfilment",       "priority": 55,
     "principle": "the user wants to OBTAIN, order, provision, or request something new"},
    {"id": "record_action",    "priority": 45,
     "principle": "the user wants to triage, classify, assign, or modify a record"}
  ]
}
```

`priority` resolves a cross-kind tie when the classifier is torn between two
kinds (higher wins). The single most important consequence:
`knowledge` (50) > `record_retrieval` (40) → **a bare stated problem defaults to
KB, never to similar-tickets.** This is the one policy that fixes the payroll
case, and it is written ONCE over kinds — never per agent-pair.

> Not an "axis". The rejected axis was a hardcoded, incomplete 4-way baked into
> the prompt that orphaned uc02/05/08. This is per-agent declared DATA, complete
> (every agent maps to exactly one kind), and extensible (add a kind as data).

---

## 2. One new field per agent card (data)

Add `capabilities` (a LIST of closed-vocabulary kind ids) to each agent version
body. It lives OUTSIDE `skills`, so it does NOT change `content_hash`
(= `digest(body->'skills')`) and triggers NO re-embed.

Why a list, not a single string: the kind is one DIMENSION (the deliverable),
and almost every agent has exactly one value — but a few genuinely serve two
needs (uc08 is `fulfilment` yet its "what can I request?" intake is half
`knowledge`; a future explain-and-fix agent is `knowledge` + `record_action`).
A list (usually length 1) avoids forcing those agents to mislabel and is
future-proof to 500 agents with zero schema change. An agent is eligible for a
query when its `capabilities` INCLUDE the classified kind. The list does NOT
encode domain/sub-type (network-KB vs HR-KB) — that within-kind distinction is
handled by retrieve-then-decide over the full cards. Values are validated
against `capabilities.json` at registry load (closed vocabulary, not free text).

| agent | capabilities |
|---|---|
| uc01_summarization | `["record_summary"]` |
| uc02_similar_tickets | `["record_retrieval"]` |
| uc03_kb_lookup | `["knowledge"]` |
| uc08_fulfillment | `["fulfilment"]` |
| (future uc05_triage) | `["record_action"]` |

Synced to `itsm.agent.body` via the existing `database/agent/sync.py` (body
upsert; hash-gated, so no spurious re-embed since skills are unchanged).

---

## 3. The classifier — how a query gets its kind (cascade, cheap, scale-flat)

A new pre-decision stage `kind_classifier`. Cascade (per the research — fast
path first, LLM only when unsure):

1. **Embedding match (deterministic, fast):** embed the query once (already done
   for retrieval — reuse the vector), compare to each kind's `principle`
   embedding (5 centroids), take the nearest. If the top kind beats the runner-up
   by a clear margin → use it. No LLM call.
2. **LLM tie-break (only on low margin):** a tiny bounded prompt listing the ~5
   kind principles → pick one. Bounded by the taxonomy, NOT the agent count, so
   it stays cheap and accurate at 500 agents (this is the whole point).
3. Output: one `kind` (+ confidence). For multi-intent queries the existing
   decomposer has already split them, so the classifier runs PER sub-query.

Cost: 5-centroid cosine (free, reuses the query vector) + an occasional small
LLM call. Independent of agent count.

---

## 4. Where it slots into the existing funnel

Current: `decompose → rewrite → (2) retrieve top-K → (3) activation filter →
(3.4) preroute → (3.5) single-survivor → (4) LLM disambiguate → post-4 guard`.

Add **Stage 2.5 — capability filter**, between retrieve and Stage 3:

```
(2) retrieve top-K agents
        │   e.g. "database payroll issue" → {uc02, uc03, uc08}
        ▼
(2.5) classify kind  →  keep only candidates whose capability == kind
        │   kind=knowledge → {uc03}
        │   SAFETY: if that empties the set, KEEP the unfiltered candidates
        │   (the kind is a PRIORITY, never a hard gate → can't orphan an agent,
        │    the failure mode that killed the old axis)
        ▼
(3..3.5) activation / preroute / single-survivor   → uc03 is sole survivor
        ▼
(4) disambiguate ONLY runs when >1 SAME-kind candidate remains
    (genuinely needs a within-kind decision — its correct job)
```

Net effect: the LLM disambiguator stops being asked cross-kind coin-flips; it
only ever decides BETWEEN agents of the same kind (which are genuinely distinct
services). Most queries become a deterministic single-survivor after 2.5 →
faster AND consistent.

---

## 5. The focus-pronoun fix (folded in — the INC0001009 case)

"how to solve this issue" after focusing INC0001009 classified as `knowledge`
but searched the KB with the literal pronoun text → no match. Fix: when the
sub-query is a bare reference ("this", "this issue") AND a record is in focus,
the knowledge route resolves the reference to the focus record's symptoms and
uses the by-ticket KB path (`search_kb_by_ticket` / symptom fallback) instead of
the literal string. This is a binding fix on the uc03 handler input, orthogonal
to the capability layer but shipped together.

---

## 6. Cache-version bump (folded in)

Routing-logic changes must invalidate the semantic turn cache (else it serves
stale frozen decisions, as seen). Bump `PIPELINE_CACHE_VERSION` so all cached
turns invalidate automatically on deploy — no manual flush.

---

## 7. Migration (current 5 agents)

1. Add `capability` to the 5 agent JSON cards.
2. Add `registries/v2/platform/capabilities.json`.
3. `python database/agent/sync.py` (body upsert; no re-embed — skills unchanged).
4. No data migration; no embedding refresh; Supabase untouched.

---

## 8. Rollout & testing (flag-gated, reversible)

- Flag `ONEOPS_ROUTER_CAPABILITY_FILTER` (default OFF). OFF = today's behaviour
  exactly (safe rollback).
- **Unit:** classifier (each kind's principle → right kind), Stage-2.5 filter
  (incl. the empty-set safety fallback), priority tie-break.
- **Integration:** the payroll/terse battery (bare problems → knowledge;
  explicit retrieval → record_retrieval), cache-flushed, RUN TWICE to prove
  determinism (no run-to-run flip).
- **Regression:** the 100-query system baseline (`scripts/routing_eval_system100.py`)
  must hold ≥ its current 95–96/100, cache-flushed, flag ON vs OFF compared.
- **Smoke/devils:** existing routing smoke + adversarial probes.
- **Edge:** empty retrieval, classifier low-confidence, multi-sub-query, focus
  follow-ups, off-domain (control gate still first).
- Live-verify in Langfuse that a new `router.stage2_5.capability_filter` span
  shows the kind + survivors (today's observability work makes this visible).

---

## 9. Why this scales to 500 agents (the point)

- The **kind set stays ~5** regardless of agent count → the classifier prompt and
  the priority table never grow.
- Overlap is resolved at the **kind level (O(kinds)), never the agent level
  (O(agents²))** → no per-pair authoring, ever.
- **Retrieve-then-decide** (already built) keeps the shortlist bounded as the
  pool grows; the capability filter narrows it further by kind.
- Within a kind, agents are genuinely distinct → the LLM's within-kind pick is a
  real decision, not a coin-flip.
- At volume, the authored `priority` can be replaced by **learned win-rates** from
  production outcomes (iCARE/MAC pattern) — same hook, no schema change.

---

## 10. Risks & mitigations

| risk | mitigation |
|---|---|
| Mis-classified kind drops the right agent | 2.5 is a PRIORITY not a hard gate: empty result → keep unfiltered set; never orphans an agent |
| Classifier adds latency | embedding-first (reuses query vector, ~free); LLM only on low margin |
| A query spans two kinds | decomposer already splits; classify per sub-query |
| Wrong `priority` choice | it's data in one file; tune once, applies everywhere |
| Stale cache hides the change | cache-version bump (§6) |

---

## 11. Phased delivery

1. **P1 — taxonomy + cards (data only):** `capabilities.json` + `capability` on
   5 cards + sync. No behaviour change yet. (~½ day)
2. **P2 — classifier + Stage 2.5 (flag OFF):** build + unit-test in isolation.
   (~1–1.5 days)
3. **P3 — validate + flip:** run the determinism battery + 100-query baseline
   flag ON vs OFF; flip default ON when green. (~½ day)
4. **P4 — fold in focus-pronoun fix + cache-version bump.** (~½ day)
5. **P5 — (later) learned win-rates** to replace authored priority at volume.

Total for P1–P4: ~3 days. Each phase independently revertible (flag/data).
