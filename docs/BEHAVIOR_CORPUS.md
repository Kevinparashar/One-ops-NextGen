# BEHAVIOR_CORPUS.md — Acceptance Spec for the OneOps AI Engine

**Status:** Authoritative behavioral spec. The system is "done" for a UC family
when it handles that family's corpus rows AND the relevant C-transcripts below.
**Source:** product-owner corpus, 2026-05-20.
**Use:** the C1–C30 transcripts become **replay-test fixtures** (Phase 4); the
intent taxonomy seeds the agent registry; the routing shapes drive router tests.

> The owner's directive: *"end of the day I want the system doing all this
> functionality — not only this, more — as we build the UCs."* This corpus is
> the floor, not the ceiling.

---

## 1. Intent taxonomy (registry seed)

Each category is a **UC family**; each family expands to many registry agents.

| # | Family | Routing shape | Determinism dial |
|---|---|---|---|
| 1 | Password / account access | single-agent + action | high (auth) |
| 2 | Incidents — report | single-agent / slot-fill | medium |
| 3 | Incidents — status & lookup | single-agent read / multi-read | low |
| 4 | Incidents — updates & actions | single-agent action (confirm-gated) | high |
| 5 | Service requests | slot-fill → action | medium |
| 6 | Approvals | single/bulk action, policy-reasoned | high |
| 7 | Change management | slot-fill journey + CAB approval | high |
| 8 | Problem management | multi-agent dependent | medium |
| 9 | CMDB / asset queries | single/federated read | low |
| 10 | Onboarding / offboarding | long-running journey, fan-out | high |
| 11 | On-call / paging | action + notification fan-out | high |
| 12 | SLA / reporting / analytics | read + progressive drill-down | low |
| 13 | Knowledge base / how-to | single-agent retrieval | low |
| 14 | Ambiguous / context-dependent | clarify OR session-resolve | n/a (router) |
| 15 | Conversational / off-topic | platform boundary responder (not an agent) | low |

Cross-cutting modes that overlay any family: **compound multi-step**,
**bulk/admin RBAC-gated**, **multilingual**, **injection/abuse**, **vague**.

---

## 2. The six routing shapes (router must produce all six)

1. **Single-agent** — "reset my password", "status of INC0048213".
2. **Multi-agent parallel (independent)** — "show my open incidents, my pending
   approvals, and my active service requests" → 3 independent reads, fan-out.
3. **Multi-agent dependent (sequential chain)** — "find INC0048213's related
   change and tell me who approved it" → read → read → read, DAG edges.
4. **Ambiguous / context-dependent** — "close it", "approve", "same as last
   time" → resolve from session/entity-ledger OR clarify. Never guess.
5. **Slot-filling journey** — "raise an incident", "onboard a new hire" →
   multi-turn guided flow, resumable.
6. **RBAC/ABAC-gated** — "approve emergency change", "grant prod admin",
   "bulk-close 90-day incidents" → eligibility decided before execution.

---

## 3. Behavioral invariants (extracted from C1–C30 gold transcripts)

Each line is a testable assertion. The C-id is the replay fixture.

| Invariant | Fixture |
|---|---|
| Every state-changing action is **confirmed before execution**. | C1, C3, C6, C7 |
| Stale recovery data (old phone) → **offer alternatives, never silently fail**. | C2 |
| Vague start → **progressive narrowing**, one question at a time. | C3, C19 |
| Known-issue match → offer **fix-or-ticket** choice, don't auto-ticket. | C3, C24 |
| **Mid-flow correction** ("it started yesterday") updates slot state, re-confirms. | C4 |
| "close it" / "approve" with N candidates → **disambiguate, list the options**. | C5, C10 |
| "same as last time" → **resolve from history**, show what was resolved, confirm. | C6 |
| Compound one-sentence request → decompose, **resolve each entity, confirm chain**. | C7 |
| **Pivot mid-flow** (ask freeze status during a change flow) → answer, resume. | C8 |
| Abandoned flow → **saved as a durable draft**, resumable days later by name. | C9, C17 |
| Bulk approval → engine applies the **policy threshold itself**, names what qualified. | C10 |
| Frustrated user → **de-escalate**, act, log that the user flagged the delay. | C11 |
| Federated lookup → **fan-out across systems**, single stitched answer. | C12, C25 |
| Non-English input → **same-language reply** (Hindi, French, German). | C13 |
| Onboarding → enumerate the standard bundle, **fan out to N sub-tasks**, digest. | C14 |
| Out-of-scope ticket → **no confirm/deny, no info leak**; offer the access path. | C15 |
| Injection in ticket data → **treated as data, never instruction**; offer SOC flag. | C16, C26 |
| Long-running flow → **pausable and resumable** with explicit checkpoints. | C17 |
| Two intents in one conversation → **handle both, sequence sensibly**. | C18 |
| User asks for a human → **handoff with context**, no repetition required. | C19 |
| Emergency → **break-glass path**: time-boxed, audited, linked to a P1. | C20 |
| User pushes back on policy → **policy holds**; offer the legitimate path. | C21 |
| Reporting → **progressive drill-down**; each follow-up refines the prior result. | C22 |
| Outage signal → offer **P1 + page on-call + bridge channel**; act on confirm. | C23 |
| Recurring incident → detect the pattern, offer a **problem record**. | C24 |
| Data-exfil attempt → **refuse**, offer to raise a security incident. | C26 |
| Multi-system status mismatch → **detect, surface, offer remediation ticket**. | C27 |
| Off-topic → brief, in-character redirect; **no refusal theatre**. | C28 |
| Lookup → action → confirmation **chains cleanly** in one turn-set. | C29 |
| "cancel … actually wait" → **no side effect until final confirm**; reversible. | C30 |

---

## 4. Capabilities this corpus ADDS to ARCHITECTURE.md

The corpus surfaces five capabilities the architecture must name explicitly.
These are folded into the registry/executor design — not new ADRs, but
build requirements:

1. **Durable drafts** (C9, C17, C30) — an abandoned slot-filling flow is saved
   as a user-addressable draft (`"resume my laptop request"`), distinct from a
   LangGraph run checkpoint. Draft = paused intent + collected slots; lives in
   the session store, tenant-scoped, policy-retained.
2. **Journeys / slot-filling as a first-class agent shape** (C4, C8, C14, C17)
   — a journey is a determinism-`high` agent whose definition declares ordered
   slots, per-slot validation, confirmation gates, and resume points. The
   executor drives it; the LLM only fills/clarifies slots.
3. **Mid-flow correction & pivot** (C4, C8) — within a journey, a turn may
   correct a prior slot or interject an unrelated read. The router must
   recognise "this turn edits the active journey" vs "this is a new intent".
4. **Human handoff** (C19) — a terminal action that escalates to L1/L2 with the
   full traced context attached, so the user never repeats themselves.
5. **Break-glass / emergency policy path** (C20, C23) — a policy-engine
   decision type: faster approval, mandatory P1 linkage, time-boxed grant,
   session recording, heavy audit.

---

## 5. Phase 4 mapping

- **C1–C30** → replay-test fixtures. Each replay asserts the invariants in §3
  for its C-id — not "did it return something", but "did it confirm before
  acting", "did it disambiguate", "did it hold policy".
- **Corpus rows** (the flat utterance lists) → router property tests: every
  row produces a valid plan DAG or an explicit clarify — never a silent wrong
  route; every RBAC/ABAC-gated row is denied for an ineligible caller.
- **Multilingual rows** → same-language-reply assertions.
- **Injection/abuse rows** → must classify as data/threat, never execute.
- **Vague rows** → must enter narrowing, never guess.

---

## 6. Scope discipline

This corpus is the **acceptance floor**. As UC families are built (P1 registry
onward), each family's agents are validated against its taxonomy rows and any
C-transcripts that touch it. A UC family is not "done" until its slice of this
corpus passes as real replay tests — not smoke tests.
