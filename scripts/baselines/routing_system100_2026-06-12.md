# Routing baseline — system 100-query (unseen, real-life)

- **Date:** 2026-06-12
- **Commit:** 6781ad5 (uc02 similar-by-text; on `main` via PR #13)
- **Flag:** `ONEOPS_ROUTER_MERGE_DECOMPOSE_REWRITE=1` (ON, persisted in .env)
- **Harness:** `scripts/routing_eval_system100.py` (live API :8000, tenant T001, role service_desk_agent, concurrency 3)
- **Scope:** uc01 / uc02 / uc03 / uc08 — excludes uc05 (button-only)

## Result: 95/100 raw (~97–98 effective)

| Agent | Score |
|---|---|
| uc01_summarization | 24/25 |
| uc02_similar_tickets | 24/25 |
| uc03_kb_lookup | 23/25 |
| uc08_fulfillment | 24/25 |
| **TOTAL** | **95/100** |

## The 5 non-matches (characterized)

| # | Query | Routed | Verdict |
|---|---|---|---|
| 22 | "how **was** INC9010014 **resolved**" | uc03 KB | **Correct by design** — card routes "how was this solved" → authored KB, not the record. Mislabeled in the set. |
| 63 | "how do I **request a software exception**" | uc08 (interrupt) | **Acceptable** — was the flagged alt; harness can't read uc08 off an interrupt envelope. |
| 48 | "**anything matching** mailbox auto-archive not running" | uc03 KB | Defensible ambiguity — no "tickets" cue → reads as knowledge. |
| 93 | "**order an ergonomic chair** for my office" | control-gate OOS | Defensible — office furniture is at the edge of the IT domain (Stage-1 scope decision). |
| 51 | "guide for **requesting time off in the HR system**" | uc03 KB (wrong article) | **Real gap (RAG precision)** — routing correct, but surfaced an irrelevant article for an out-of-corpus HR topic. The 0.35 faithfulness gate let a weak match through; should fall back to "no matching article". |

## Notes
- Routing intent accuracy ≈ 97–98% across two independent unseen 100-query sets (this + the 2026-06-12 prior run, both 95–96/100).
- merge-decompose-rewrite flag stable for routing accuracy.
- Only actionable quality lever: uc03 relevance/faithfulness gate precision (case 51) — tighten so out-of-corpus queries get an honest "no match".
