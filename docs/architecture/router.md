# Router — how it works, and the path to 100 UCs

The router answers one question per turn: **which agent (or set of agents) should handle this user query — or should we refuse/clarify?** This doc is the canonical description of the live funnel + the concrete work to make it hold at ~100+ use-cases (the incoming ITOM batch).

---

## 1. TL;DR

- **Pattern:** retrieve-then-rerank (Tool RAG). Embeddings do *recall* (get the right agent into a shortlist); an LLM reranker does *precision* (pick from the shortlist, reading the full card). This is the research-canonical scale pattern (RAG-MCP / SkillRouter / Tool-to-Agent), mandatory past ~100 agents.
- **A score never *refuses*.** The abstain gate is a junk-skip only; the **reranker is the sole refuse authority** (intent + `not_when` + off-domain axis-D).
- **Multi-agent = decompose.** "summarize X and find docs" is split into sub-queries, each routed independently → the union is the agent set.
- **Cards are context, not the decision.** Descriptions/`use_when`/`not_when` are the routing signal the LLM reads; they are *not* a keyword catalog and routing must not depend on enumerating topics in them (§2.1).
- **Validated:** ~95–96% on unseen real-user queries (single-agent ~100%, multi-agent sets 100% via the full funnel, off-domain refused). Residual: a control-gate model bug (below).

---

## 2. The funnel (current, in order)

Entry point: `POST /api/chat` → executor graph (`src/oneops/executor/graph.py`) → `Router.route()` (`src/oneops/router/router.py`).

| # | stage | kind | what it does | file |
|---|-------|------|--------------|------|
| 1 | **control gate** | LLM + structural | social/meta (greeting/thanks) → canned reply; clear non-IT chit-chat → refuse; everything else falls through | `conversation/control_gate.py` |
| 2 | **entity extraction** | deterministic | pull + canonicalize record ids (`INC0001001`→service=incident); malformed → clarify | `router/entity_id.py` |
| 3 | **decompose** | LLM | split a compound/multi-intent message into atomic sub-queries (this is how the *agent set* is produced) | `router/decompose.py` |
| 4 | **rewrite** | LLM | resolve references ("close it", "its priority") against history + focus | `router/rewrite.py` |
| 5 | **retrieve** | vector ANN | embed sub-query → pgvector HNSW over `ai.embeddings_agent` → **top-K candidate agents** (the shortlist) | `router/retrieval.py` |
| 6 | **filter** | deterministic | drop candidates failing `activation_condition` (entity-service, `entry_mode`) + ABAC (role/tenant/audience). *Button-only agents (uc05/uc08) are filtered from chat here.* | `router/conditions.py`, `signals.py` |
| 7 | **preroute** | deterministic | high-confidence shortcuts (bare id → summary, focus follow-ups). Match → select, skip the LLM | `router/disambiguation.py` |
| 8 | **abstain** | deterministic | top retrieval score < **junk floor (0.25)** → skip the LLM (obvious junk). Otherwise **fall through to the reranker** (NOT a refusal) | `router/disambiguation.py` |
| 9 | **rerank / disambiguate** | LLM (the decision) | reads the query + **only the shortlisted candidates' cards** (description + `use_when` + `not_when`) → selects agent(s) / refuses (axis A/B/C/D). The **refuse authority.** | `router/disambiguation.py` |
| 10 | **plan + execute** | — | `assemble_plan(selected)` → executor runs tools/handlers (downstream of routing) | `router/plan.py`, `executor/` |

**The decision in one line:**
`decompose → (per sub-query) retrieve top-K → deterministic filter → preroute → abstain-skip-junk → LLM rerank over the shortlist → agent(s)`. Multi-intent ⇒ multiple sub-queries ⇒ the union (the set).

---

## 3. Key design decisions (and the why)

1. **Retrieve-then-rerank, never inject-all.** The reranker only sees the **top-K shortlist**, never the full registry — so the prompt stays bounded at any agent count. (Injecting all agents collapses ~50+ tools.)
2. **Shortlist-only catalog.** The agent cards ride in the **per-request user block, scoped to the retrieved candidates** — *not* a cached all-agents catalog. This is what makes the reranker survive 100 UCs. (`_candidate_catalog` in `disambiguation.py`.)
3. **A score must not refuse.** Abstain is a junk-skip floor (~0.25); the **reranker decides route-vs-refuse** by intent. A raw cosine can't separate weak-but-valid (e.g. a KB topic ~0.31) from junk; the reranker (reading the full card) can. (`ONEOPS_ROUTER_ABSTAIN_MIN_SCORE` in `.env`.)
4. **`not_when` is the contrastive signal**, read by the reranker (not embedded) — how same-entity look-alikes (summarize vs similar vs how-to) are told apart.
5. **Cards are context, not the decision.** Routing rides on *intent* the LLM reads, not on whether a topic string is enumerated in a card (§2.1). Don't fix routing by stuffing topics into cards.
6. **Decompose for multi-agent.** Sets are produced by splitting the query, not by the reranker emitting many agents from one pass.
7. **Soft, never hard, domain gating** (at scale). A wrong hard pre-filter prunes the right agent unrecoverably; prefer boost / fall-through-when-unsure.

---

## 4. Current state

**Validated (unseen real-user queries):**
- Single-agent (uc01 summary / uc02 similar / uc03 KB) — ~95–100%.
- Multi-agent sets — **100%** via the full funnel (decompose); offline under-tests them.
- Off-domain — refused (control-gate + reranker axis-D).
- The how-to+id confusion class ("how do I resolve INC…" → uc03) — fixed.
- KB over-abstain (valid KB queries scoring ~0.31) — fixed by abstain-as-fallback (floor 0.32 → 0.25).

**Known residual (open):**
- **Control-gate over-refusal** — `gpt-4o-mini` mis-labels some IT how-to ("connect to corporate wifi", "factory reset company iphone") as `out_of_scope`, despite the prompt's "IT how-to → none" rule. **Fix: run the control gate on `gpt-4o`** (a nuanced classification, same as the doc2query judge). Model quality, NOT a scale issue.

**Operational notes:**
- `/api/chat` *executes* each query (tools/handlers) — heavy; it **cannot be load-tested at concurrency** against the single LiteLLM gateway (concurrent full-execution saturates it → timeouts). Eval routing **sequentially**, or build a route-only path.
- The chat-turn cache keys on `tenant+user+role+session+message` — **use unique session ids in evals** or stale results are served.

---

## 5. Scaling to 100 UCs — what's done / what's needed

The **architecture survives 100 UCs** (retrieve→rerank-on-shortlist is the proven pattern). Accuracy holds only if the scale-levers below are applied — each **triggered by eval data, not preemptively.**

### Done ✅
- **retrieve → rerank** (mandatory >100).
- **shortlist-only catalog** (the #1 structural blocker — inject-all fix).
- **decompose** (multi-agent scales — each sub-query is a small routing problem).
- **abstain-as-fallback** (reranker is the refuse authority).
- **eval harnesses + counter-example regression gate** (measure-don't-guess; see §6).

### Needed before / as ITOM lands ⬜
| lever | when | why |
|------|------|-----|
| **Control gate → gpt-4o** | now | fixes the live over-refusal; constant cost regardless of UC count |
| **Embedding fine-tuning on hard negatives** | when `retrieval_eval` recall@K drops | the #1 recall lever; "fine-tuning > scale". Hard negatives are already mined by the harness |
| **Per-domain abstain floors** | when itsm/itom subdomains coexist | one global floor can't separate score distributions across domains |
| **Soft domain scoping** (boost, not hard filter) | when a flat top-K over 100 gets noisy | narrows candidates per domain *without* the hard-prune cascade risk |
| **Route-only eval endpoint** (no execution) | for parallel eval at scale | `/api/chat` executes → can't parallelize; a route-only path can |
| **Topology grounding (CMDB/Smartscape)** | ITOM, post-DAL | ITOM intent lives in the dependency graph; route by CI/service. Needs the DAL contract |
| **Catalog stays shortlist-only** | always | never regress to all-agents injection |

### What does NOT need to change
- The reranker decision (bounded by the shortlist).
- The control gate's *structure* (UC-count-independent; only its model needs upgrading).
- `not_when` / decompose / abstain-as-fallback mechanics.

### What to avoid
- **Card-stuffing** topics to fix routing (§2.1 — unscalable, fragile).
- **Hard domain pre-filter** (prunes the right agent unrecoverably).
- **Scaling by growing prompts** (more playbooks/agents in one prompt → inject-all collapse).

---

## 6. How to measure (eval harnesses)

| harness | measures | path / notes |
|---------|----------|--------------|
| `scripts/retrieval_eval.py` | **recall@K** + mines hard negatives | offline; the recall-degradation signal that triggers fine-tuning |
| `scripts/routing_eval100.py` | single-agent + off-domain selection (offline) | fast (parallel); under-tests sets (no decompose) |
| `scripts/routing_eval_chat.py` | **full pipeline** via `/api/chat` (control-gate + decompose + Stage-3) | faithful; run **sequentially** (`--workers 1`), unique sessions |
| `scripts/counter_example_eval.py` | **regression gate** — known confusions never misroute | offline; CI gate (`make eval`) |
| `scripts/multiagent_eval.py` | multi-agent sets via full funnel | proved sets 16/16 |

Gate (CI): `tests/integration/test_routing_gates.py` (`make eval`, behind `RUN_ROUTING_EVAL=1`).

**Discipline:** routing is its own versioned eval layer — every card/prompt/embedding change re-runs these; regressions fail loudly; the counter-example set grows from production misroutes.

---

## 7. The bottom line

- **Works today** at ~95–96% on unseen queries; the architecture is the one that scales.
- **One live bug:** control-gate model (→ gpt-4o).
- **To hold ~95%+ at 100 UCs:** apply the scale-levers (§5) *when the harness data calls for them* — chiefly **embedding fine-tuning** (recall) and **per-domain abstain**. None are rewrites.
- **The instruments to know when** are already built (§6).
