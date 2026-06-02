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
"""
from __future__ import annotations

PIPELINE_CACHE_VERSION = "v7"

__all__ = ["PIPELINE_CACHE_VERSION"]
