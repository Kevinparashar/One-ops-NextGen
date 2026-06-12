"""Pipeline cache version — bump on any render/filter rule change.

Every API-edge cache (chat-turn, fast-path, UC-2 button) embeds this
version in its key so a code change to the renderer (e.g. `_HIDDEN` in
`oneops.use_cases._shared.field_labels`, UC-2's `render.py`, the executor's
composer) auto-invalidates every cached entry without a manual flush.

Treat it like a database migration number: monotonic, never reused.

Changelog
---------
  v1 — initial
  v2 — 2026-05-30 — hide search_tsv + content_hash_* from operator-facing
                    summary card; auto-invalidate chat-turn / fast-path /
                    UC-2 edge caches alongside UC-1's cache_aside.
  v3 — 2026-05-31 — UC-2 semantic-confidence gate on metadata boost +
                    per-result discriminator labels; invalidate UC-2 cache.
  v4 — 2026-05-31 — UC-2 also gates the diagnosis_match +0.05 boost by
                    sem_trust so a strong diagnosis-trail hit on a weak
                    semantic candidate cannot climb into the top-K.
  v5 — 2026-05-31 — UC-2 min_similarity_score now applies to the composite
                    (matches the response field semantics) and defaults to
                    0.5 so the tail never includes <50% match items.
  v6 — 2026-06-01 — UC-1 summary format change (compact narrative + dated
                    bullets, key_details list hidden). Invalidates warm
                    turn-cache entries that hold the old paragraph shape.
  v7 — 2026-06-02 — Data-flow binding: produced-value compound queries now
                    decompose+bind+execute (previously inlined or blocked), and
                    a binding to an undeclared producer field drops at plan time
                    → some turns change outcome. Invalidate pre-fix cached turns
                    so the new execution path is not masked by a stale entry.
  v8 — 2026-06-12 — Stage-1 control gate restored the principle-based
                    `out_of_scope` scope decision (operational-ownership test,
                    policy-sourced). Off-domain turns that previously routed to a
                    weak agent match now refuse via the canonical scope refusal
                    (KB-backstop still rescues real IT how-tos). Invalidate
                    pre-fix cached turns so the new scope decision is not masked.
  v9 — 2026-06-12 — Router §470 compliance: the stage-4 disambiguator's
                    deliberate off-domain refusal (empty selection tagged
                    intents:["off_domain"]) is now honored — `_floor_dispatch`
                    no longer force-routes it. Off-domain that leaks past the
                    control gate is caught at the router (second scope layer)
                    instead of force-routed to a weak match. Hedges (untagged
                    empty selection) still dispatch-by-default. Invalidate
                    pre-fix cached turns so the new router decision is not masked.
  v10 — 2026-06-12 — Disambiguator teach-before-provision: self-serviceable
                     ambiguous actions (reset password, configure VPN, set up
                     MFA) now route knowledge-FIRST + fulfilment (KB self-service
                     offered before the service request), instead of straight to
                     catalog. Provisioning-only asks (hardware/license/access)
                     unchanged. Invalidate pre-fix cached turns.
  v11 — 2026-06-12 — Scope authority moved to the router. The Stage-1 control
                     gate reverts to social/meta-only (stops guessing domain),
                     so legitimate catalog requests in HR/finance/facilities
                     categories (expense, leave, parking, room booking) are no
                     longer false-refused; the router's §470 decline is the sole
                     scope authority (route if any capability fits, else refuse).
                     Invalidate pre-change cached turns.
  v12 — 2026-06-12 — Reverted the Stage-1 control gate to the original
                     operational-ownership scope prompt (it refuses off-domain —
                     "change a bulb", meta/prompt-extraction probes, weather —
                     which the social/meta-only variant leaked into KB+SR).
                     Invalidate the social/meta-only cached turns.
  v13 — 2026-06-12 — Disambiguator fit-check: after the card-based pick, the LLM
                     verifies the query genuinely fits; only genuine DOUBT
                     (knowledge vs fulfilment) triggers the KB-first-then-offer-SR
                     fallback. Replaces the always-firing teach-before-provision
                     (which padded every KB how-to with uc08). Invalidate turns.
  v18 — 2026-06-13 — Control-gate scope definition tightened at source
                     (updated_policy_v2.md §Product scope + gate _PROMPT
                     Principle 1): ITOM operations/SRE knowledge (pod recovery,
                     replication lag, query-latency tuning, network diagnostics)
                     and terse resource-obtain/claim/submit asks are now in
                     scope, no longer false-refused. Off-domain still refuses.
                     Invalidate pre-fix cached turns.
  v19 — 2026-06-13 — Disambiguator rule 4 (deflect-first): a SELF-SERVICE action
                     the user could perform themselves (reset/unlock/re-enroll
                     their own credential or setting) routes knowledge-FIRST +
                     fulfilment fallback EVEN when phrased as a command — not
                     straight to catalog. Provisioning (hardware/license/access)
                     still routes directly to fulfilment. Invalidate turns.
  v20 — 2026-06-13 — uc02 stale-focus guard (multi-turn): a NEW symptom topic in
                     the current message outranks a focus_entity_id carried from
                     a prior turn (text path wins); a bare referential follow-up
                     ("any similar ones?") still anchors on the focused ticket.
"""
from __future__ import annotations

PIPELINE_CACHE_VERSION = "v20"

__all__ = ["PIPELINE_CACHE_VERSION"]
