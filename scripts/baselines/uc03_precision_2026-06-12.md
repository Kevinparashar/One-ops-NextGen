# Baseline — UC-3 KB precision/recall (no-match override fix)

- **Date:** 2026-06-12
- **Branch/fix:** `uc03-precision-gate` — confidence-banded no-match override
- **Flag/knob:** `UC03_FORCE_RENDER_MIN_SCORE=0.45` (.env), gate `UC03_MIN_ANSWER_RELEVANCE_SCORE=0.35`
- **Harness:** `scripts/uc03_precision_eval.py` (live API :8000, tenant T001, concurrency 3)

## Problem fixed
The 0.35 relevance gate is tuned for recall, so it admits a borderline band where an
off-topic but generically-similar article passes (text-embedding-3-large gives any two
enterprise-IT texts ~0.30-0.40 cosine). The LLM composer correctly flagged these as
no-match, but a rigid "always render gate-passers" override vetoed it → junk article shown
(e.g. "guide for requesting time off in the HR system" → returned a *calendar-outage* article
at cosine 0.3558).

## Fix
When the composer emits a no-match, decide by the TOP article's confidence:
- top cosine >= 0.45 (or degraded mode) → force-render (recall protection, unchanged);
- top cosine < 0.45 (borderline) → honor the no-match → honest CASE B.

## Result: 47/50 raw (effectively clean)

| Bucket | Score | Meaning |
|---|---|---|
| In-corpus (want ARTICLE) | 24/25 | recall held — fix did not suppress real answers |
| Out-of-corpus (want NO_MATCH/OOS) | 25/25 effective | **zero junk articles shown** (23 no-match/OOS + 2 routed to uc08) |

The 3 raw "fails" all check out:
- [IN 13] "m365 mailbox quota policy" → no-match: the correct article (KB0005017) was **not retrieved** (gate returned an Outlook-sync article at 0.3517); the fix correctly suppressed the wrong one. Pre-existing **dense-retrieval ranking gap**, not caused by the fix.
- [OUT 6/8] "update emergency contact" / "book a desk" → routed to uc08 (read as requests), not junk KB.

50 uc03 unit tests pass; case 51 now returns an honest no-match.

## Follow-up (separate issue)
KB0005017 is embedded but doesn't rank into the top-5 for a near-title-identical query — a
dense-retrieval recall gap. Levers: document expansion / aliasing, or a cross-encoder reranker.
