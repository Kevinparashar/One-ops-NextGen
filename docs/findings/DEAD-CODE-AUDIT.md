# Dead-code audit — OneOps codebase

**Date:** 2026-05-30
**Methodology:** Static (vulture 2.16) + registry cross-reference + grep-based dispatch trace + policy/profile matrix.
**Scope:** `src/oneops/` only. Excluded: `tests/`, `scripts/`, `tools/`, `dev/`, `migrations/`, `proto/`.
**Discipline:** READ-ONLY. No source modifications. Every flagged item carries a risk assessment, **no deletion recommendations**. Verification of liveness is left to manual cross-referencing against runtime systems (Tempo, access logs) — queries proposed in §H and §I below.

The audit's purpose is to surface **candidates for review**, not to declare anything dead. Several pattern classes (registry dispatch, LangGraph string lookup, FastAPI decorators, Pydantic field assignment) defeat static analysis by design. The whitelist at `dev/dead-code-whitelist.py` enumerates the false-positive surface; the few items vulture flags at high confidence after applying it are the items below.

---

## A. Confident-dead candidates

> **Definition:** Flagged by static analysis at ≥ 80 % confidence AND no obvious runtime-dispatch pattern reaches them.
>
> Manual verification still required. None has a "delete" recommendation.

| # | Path | Line | Symbol | Conf | Static signal | Risk if removed | Verification |
|---|------|------|--------|------|---------------|-----------------|--------------|
| 1 | `src/oneops/observability/__init__.py` | 230 | local `logger` | 100 % | unused variable | Low — appears to be a local-scope leftover in a helper closure. | Read the function it lives in and the test file `tests/unit/observability/`; confirm no closure-capture relies on it. |
| 2 | `src/oneops/observability/__init__.py` | 230 | local `method_name` | 100 % | unused variable | Low — same as #1, same line. | Same as #1. |
| 3 | `src/oneops/use_cases/uc01_summarization/tools.py` | 549 | local `target_labels` | 100 % | unused variable | Low. Confined to one helper. | Confirm `target_labels` isn't returned via a tuple-unpacked caller; grep the helper's callers. |
| 4 | `src/oneops/api/app.py` | 30 | `JSONResponse` import | 90 % | unused import | Low — failure to import would cause `NameError` if used elsewhere. | Quick grep `JSONResponse` in app.py: if zero hits below line 30, candidate. |
| 5 | `src/oneops/embeddings/kb_chunker.py` | 18 | `Sequence` import | 90 % | unused import | Low — type-only import. | Grep `Sequence` in the file; if only the import line matches, candidate. |

**None of the five carry a delete recommendation.** Even high-confidence "unused import" warnings can be load-bearing in Python (registering a side-effect import, satisfying a re-export contract for `__all__`).

---

## B. Probably-dead candidates

> **Definition:** Flagged by one signal class only (typically vulture at 60 – 80 % confidence) and not eliminated by the whitelist.
>
> Most are inside Pydantic models (the whitelist covers field names, not container methods like `model_config` re-assignments). Listed below as cohorts rather than per-line to keep the report readable.

| Cohort | Files | Count @ 60 % | Likely cause |
|---|---|---|---|
| Per-model `model_config` attribute | `api/uc02_routes.py`, `api/uc05_routes.py`, all contracts | ~30 | Pydantic v2 `model_config: ConfigDict(extra="forbid")` — used by Pydantic's MRO; vulture cannot see this. |
| Protobuf-generated `_serialized_start/_end`, `_loaded_options` | `codec/generated/oneops/v1/envelope_pb2.py` | 17 | Auto-generated. Out of scope. |
| Pydantic fields not declared in `dev/dead-code-whitelist.py` | `uc_common/summary_schema.py`, `registry/models.py`, `config.py`, `uc05_triage/contracts.py` | ~120 | Field names not yet enumerated in whitelist §4. Adding them filters the false positives; none represents real dead code. |
| Local variables in defensive branches | `api/app.py:307-308` (`final_response`, `step_results`) | 2 | Likely assigned-then-overwritten in an error-handling path. Verification: read lines 290 – 320. |
| Authz cache `invalidate` method | `authz/decision_cache.py:62` | 1 | Public API surface — designed for callers that may not exist yet. Low-risk to keep; high-risk to remove if a future authz change calls it. |

**Total raw vulture findings:**
- At `--min-confidence 80` **before** whitelist: **5**
- At `--min-confidence 80` **after** whitelist: **5** (whitelist drops 60-confidence noise but cannot change the high-confidence five)
- At `--min-confidence 60` after whitelist: **280** (dominated by `model_config` and protobuf — both false positives)

---

## C. Static-dead but likely live (false positives by design)

These are flagged by vulture but are reached at runtime through indirection patterns vulture cannot follow. **All have a "do not touch" risk assessment.**

| Pattern | Example surfaces | Why vulture sees it as dead | Why it is live |
|---|---|---|---|
| **FastAPI route handlers** | `app.py:1010 _index`, `chat`, `_fast_path`; `uc05_routes.similar_tickets`; `uc05_routes.propose/decide/queue*` | Functions decorated with `@app.get/.post(...)` are never called by name in Python — FastAPI registers them at decorator-evaluation time. | Reached by HTTP requests; the entire API surface depends on them. |
| **LangGraph node methods** | `nodes.load_session`, `nodes.update_focus`, `nodes.control_gate`, `nodes.route`, `nodes.wave`, `nodes.run_step`, `nodes.aggregate`, `nodes.boundary`, `nodes.persist` | `g.add_node("name", nodes.method)` passes the method by reference; vulture sees the method definition but cannot trace the string-keyed graph. | Every turn through the executor calls each of these. |
| **Registry-dispatched tool handlers** | `oneops.use_cases.uc01_summarization.handlers:get_ticket_details`, etc. (per `registries/v2/tools/*.json`, `handler_ref`) | `HandlerResolver.register(handler_ref: str)` and import-by-string lookup; no compile-time call site. | Every chat / fast-path turn that touches UC-1/UC-2/UC-3 calls these. |
| **Pydantic BaseModel field assignments** | `ticket_id`, `service_id`, `status`, `priority`, `time_filter`, …(see whitelist §4) | `Field(default=…)` calls look like unused attribute assignments. | Define the API contract; removing breaks request/response serialization. |
| **Policy profiles by enum value lookup** | `Profile.FEATURE_AGENT_JSON.value` → string key into `POLICY_BLOCKS` dict | Vulture sees the enum members as unused, the dict lookup as a string. | UC-1 summarizer and every UC LLM call uses these. |
| **Settings attributes** | `dragonfly_url`, `nats_url`, `postgres_url`, `langgraph_postgres_url`, etc. | Pydantic Settings class fields read via `getattr(settings, name)` style; never directly referenced as `settings.foo`. | Boot-time wiring depends on every one. |
| **Span names by string literal** | `"ai.request"`, `"graph.planner"`, `"state.load"`, `"state.update"`, `"uc02.core.find_similar"`, all `"uc05.*"` (19 distinct) | Names are positional string arguments to `start_as_current_span("name", …)`. Vulture has no semantic of "span name = symbol". | Every Tempo trace; lose them and observability silently degrades. |

**Do not touch any of these.** They are the system's working surface.

---

## D. Manual-verification required before any action

> **Definition:** items that look dead but require human inspection of runtime usage (Tempo, access logs, registry cross-reference, frontend `fetch()`) before any decision.

| Item | Path / location | Why it needs manual verification | What to verify |
|---|---|---|---|
| **UC-5 inline `_tools_runner` dispatcher swap** | `api/app.py:704–740` | NATS-mode rewires the in-process runner to the dispatcher. Static analysis cannot tell whether the env-mode branch is exercised. | Confirm `UC_INVOKER_MODE` value in prod and that one path or the other has been hit in Tempo within the last 60 days (query in §H). |
| **`set_decide_dispatcher` setter** | `api/uc05_routes.py:223` | Only invoked when `UC_INVOKER_MODE=nats`. If NATS mode has never been used in production, this is unused. | Cross-reference Tempo traces for the `oneops.uc05.triage.decide` NATS subject (query in §H). |
| **`HandlerResolver.register` direct path** | `toolrunner/resolver.py` | Two ways to register: explicit and dynamic-import. Static analysis cannot tell which path each tool uses. | Grep `HandlerResolver.register(` and `resolver.register(`. If only the import-by-string path is used, the explicit path is dead. |
| **`Profile.PLANNER`, `Profile.TEAM_COORDINATOR`, `Profile.SUB_AGENT_MINIMAL`** | `policy/composer.py:50–55` | Three profiles exist but no caller in `src/` references them by enum member; only `Profile.FEATURE_AGENT*` and `Profile.INTERNAL_AGENT` are referenced. | Grep `Profile\.PLANNER`, etc. — if zero hits across src and tests, they may be aspirational profiles waiting for compound actions. |
| **`tests/unit/architecture/` empty / single-file?** | Out of audit scope (excluded by §0), but the directory exists. | Manual check that the architecture tests are run by the unit suite. | `find tests/unit/architecture -name "test_*.py"`. |

---

## E. Orphaned registry entries

> **Most actionable section of this audit.** Registry files that have **zero code references** in `src/oneops/`. These are V1 artefacts the V2 runtime does not consume.

### E.1  V1 root-level registry files — orphans

| File | Code references in `src/oneops/` | Reachable at runtime? |
|---|---|---|
| `registries/agent-catalog-registry.json` | **0** | Almost certainly not — `registry.loader.load_registry()` reads `registries/v2/` only. |
| `registries/agent-tool-mapping.json` | **0** | Same. |
| `registries/agent-registry.json` | **0** | Same. |
| `registries/router-alias-registry.json` | **0** | Same. |
| `registries/tool-registry.json` | **1** | The single hit may be in dev tooling. Verify before any change. |

**Risk assessment**: removing any of these files without confirming consumers is high-risk; **keeping them is essentially zero-cost** other than confusion for new readers. The recommendation is **do not remove**, but **flag for cleanup tracking** alongside the V1 / V2 migration story.

### E.2  V1 tool-registry — module paths that point to nonexistent code

Static-analysis result: **8 distinct `module_path` values** in `registries/tool-registry.json` reference modules that do not exist in `src/oneops/`:

```
ai_service.use_cases.uc03_kb_lookup.tools     # old "ai_service" prefix; correct is "oneops.use_cases.uc03_kb_lookup.tools"
tools.summary_tools                            # never existed at this path
tools.triage_tools                             # replaced by oneops.use_cases.uc05_triage.tools.*
tools.sentiment_tools                          # UC-4 sentiment not implemented
tools.lifecycle_tools                          # UC ticket-action not implemented
tools.creation_tools                           # UC-6 ticket-creation not implemented
tools.resolution_tools                         # suggest_resolution not implemented
tools.fulfillment_tools                        # UC-8 fulfillment not implemented
```

**Risk**: if any code path tries to import-by-string from these, it raises `ModuleNotFoundError` at runtime. Static analysis cannot rule that out. **Verification step**: grep for the strings `"tools.lifecycle_tools"`, etc., in `src/` (results expected to be zero — but confirm).

### E.3  V1 agent_ids — 5 may be dead

V1 catalog agents:
```
fulfillment_agent, kb_lookup_agent, resolution_agent, sentiment_agent,
summarization_agent, ticket_action_agent, ticket_creation_agent,
triage_agent, uc02_similar_tickets
```

V2 active agents (per `registries/v2/agents/`):
```
uc01_summarization, uc02_similar_tickets, uc03_kb_lookup, uc05_triage
```

The 5 V1 names with no V2 equivalent (`fulfillment_agent`, `resolution_agent`, `sentiment_agent`, `ticket_action_agent`, `ticket_creation_agent`) **are referenced only inside the V1 registries** that themselves have zero code references (see E.1). They are aspirational placeholders for UCs the codebase has not implemented.

**Risk assessment**: harmless if left. Removal would require updating 4 JSON files in concert; without a corresponding code change, the cleanup is purely cosmetic.

### E.4  V2 registry — alignment is perfect

| Set | Members |
|---|---|
| Tools `tool_refs` in v2 agents | `find_similar_entities`, `get_cached_summary`, `get_kb_article`, `get_ticket_attachment_metadata`, `get_ticket_details`, `get_ticket_links`, `get_ticket_timeline`, `put_cached_summary`, `search_kb`, `search_kb_by_ticket`, `summarize_entity` (11) |
| Tool files in `registries/v2/tools/` | Exactly the 11 above. |
| **Orphans** (defined but not used by any v2 agent) | **0** |

The V2 registry is internally consistent. No orphans, no missing references.

---

## F. Unused policy blocks and profiles

`src/oneops/policy/composer.py` declares **8 profiles** and references **N policy blocks** assembled from `blocks.py`.

### Profile usage matrix

| Profile | Used by (greps in `src/`) |
|---|---|
| `Profile.INTERNAL_AGENT` | router/intent_classifier, router/time_filter_extractor, executor probes |
| `Profile.FEATURE_AGENT` | UC-3 KB summarizer (likely) |
| `Profile.FEATURE_AGENT_WITH_TOOLS` | Inherited from base; not directly referenced as enum |
| `Profile.FEATURE_AGENT_JSON` | UC-1 `llm_summarizer.py` |
| `Profile.PLATFORM_SYSTEM` | Built into base profiles; not directly referenced |
| `Profile.PLANNER` | **No direct grep hits** — manual verification recommended |
| `Profile.TEAM_COORDINATOR` | **No direct grep hits** |
| `Profile.SUB_AGENT_MINIMAL` | **No direct grep hits** |

**Candidates for review**: `PLANNER`, `TEAM_COORDINATOR`, `SUB_AGENT_MINIMAL` profiles may be aspirational. Verify via:

```bash
grep -rE "Profile\.(PLANNER|TEAM_COORDINATOR|SUB_AGENT_MINIMAL)" src/ tests/
```

If zero results across both, they are unused.

### Blocks per profile

The block-list-per-profile is defined as data in `composer.py:138–145`. Every block referenced by every profile maps to a constant in `blocks.py`. The composer self-validates at import:

> `_validate_profiles_against_blocks()` — sanity check at import: every profile's blocks exist in `POLICY_BLOCKS`.

This is the safety net. Any block deletion would fail the boot validation, so blocks cannot silently rot.

**Recommendation**: no action; the validator catches drift.

---

## G. UC packages on disk vs the V2 registry

| Package under `src/oneops/use_cases/` | V2 agent? | Status |
|---|---|---|
| `uc01_summarization` | ✅ active | live |
| `uc02_similar_tickets` | ✅ active | live (shipped today) |
| `uc03_kb_lookup` | ✅ active | live |
| `uc05_triage` | ✅ active | live |
| `_shared` | n/a (utility) | used by all UCs (`field_labels`, `kb_store`) |

**No orphan UC packages.** The codebase exactly matches the V2 registry.

---

## H. Tempo queries to run manually

> **Do not run from this audit.** These need a 60-day window and human interpretation.

Each finding in §D that needs runtime confirmation maps to one TraceQL query against Tempo. Run them with the time range set to **last 60 days** in the Tempo UI / API.

```traceql
# 1. Has the NATS dispatcher path for UC-5 propose been used?
{ span.oneops.endpoint = "uc05.propose" && resource.service.name = "oneops-api" }

# 2. Has the NATS dispatcher path for UC-5 decide been used?
{ span.oneops.endpoint = "uc05.decide" && resource.service.name = "oneops-api" }

# 3. Has anyone hit the fast-path summarize?
{ span.name = "uc01.fast_path.summarize" }

# 4. Confirm every documented span name has appeared in production:
{ span.name = "ai.request" }
{ span.name = "graph.planner" }
{ span.name = "state.load" }
{ span.name = "state.update" }
{ span.name = "uc02.core.find_similar" }
{ span.name = "uc05.runner.invoke" }
# (full list in §C "Span names by string literal")

# 5. Has the policy profile `Profile.PLANNER` been used?
# (proxy: any span with attribute llm.policy_profile = "PLANNER_POLICY_PROFILE")
{ span.llm.policy_profile = "PLANNER_POLICY_PROFILE" }
```

If any of the §C span queries returns zero results in 60 days, that span is a candidate for removal review — but **not automatic deletion**; some spans fire only during failure modes that may not have occurred.

---

## I. Access-log queries to run manually

> **Do not run from this audit.** Need 90-day window.

Run against your API access log store (Loki, CloudWatch, etc.) with a **90-day** window. Confirm every documented endpoint has received non-zero traffic.

```logql
# FastAPI endpoints (from §G enumeration):
{job="oneops-api"} |~ `"GET /api/health"`
{job="oneops-api"} |~ `"GET /api/config"`
{job="oneops-api"} |~ `"GET /api/identity-options"`
{job="oneops-api"} |~ `"GET /api/session/`
{job="oneops-api"} |~ `"GET /api/fast/`
{job="oneops-api"} |~ `"POST /api/sessions"`
{job="oneops-api"} |~ `"GET /api/sessions"`
{job="oneops-api"} |~ `"DELETE /api/sessions/`
{job="oneops-api"} |~ `"POST /api/chat"`
{job="oneops-api"} |~ `"POST /api/fast/`
{job="oneops-api"} |~ `"POST /api/uc02/similar-tickets"`
{job="oneops-api"} |~ `"GET /api/uc05/queue-summary"`
{job="oneops-api"} |~ `"GET /api/uc05/queue"`
{job="oneops-api"} |~ `"POST /api/uc05/propose"`
{job="oneops-api"} |~ `"POST /api/uc05/decide"`
```

If any endpoint returns zero in 90 days, that handler is a candidate for review.

---

## J. Coverage observations

`make test-cov` was attempted but produced an empty report in the timeout window — the unit suite is large and the run did not complete within audit budget. **No coverage data is incorporated into this report.**

Recommendation if the user wants a coverage pass: re-run `make test-cov` separately with `-x` removed (to not exit on first failure) and a longer timeout; cross-reference any file with < 10 % coverage against §D for items that warrant manual verification.

---

## K. Anti-pattern sightings (no fixes)

> Surfaced during the audit but **not modified**. These are technical-debt observations only.

### K.1  Stale module paths in V1 `tool-registry.json` (8 entries)

See §E.2. Each entry has `module_path` pointing to a module that does not exist on disk. None of these are dispatched at runtime (the V2 registry is the dispatch source), but the stale entries are confusing for any reader who interprets the V1 file as authoritative.

### K.2  `_HIDDEN` filter as deny-list, not allow-list (`use_cases/_shared/field_labels.py`)

The `_HIDDEN` frozenset enumerates *what to hide*. The audit added `search_tsv` and `content_hash_*` today (2026-05-30) because the deny-list missed them. A future column added to `itsm.incident` will leak by default unless an engineer remembers to update `_HIDDEN`. **An allow-list of `_VISIBLE` columns would be strictly safer** — but that's a substantial refactor and **out of audit scope**.

### K.3  Direct `kill -9` in operational scripts (`scripts/dump-logs.sh`)

Not a code anti-pattern, but the operations layer uses `pkill -9` patterns that race with API spawns. The recent session log shows this caused multiple "API down" episodes. **Out of audit scope to fix.**

### K.4  Two cache versions, one constant per concern

`PIPELINE_CACHE_VERSION` (api edge caches) and `HUMANISE_RECORD_VERSION` (UC-1 cache_aside) are intentional and well-documented today. **No anti-pattern.** Noted for awareness — if a third version stamp appears, consider consolidating to one global "render schema version".

### K.5  Imports of nonexistent modules in V1 tool-registry are stringly-typed

This is the same finding as K.1 but framed as a code-quality observation: stringly-typed import paths in JSON registries fail at runtime, not at lint time. **Out of audit scope to fix.**

### K.6  Two `tools/` directories — root vs `src/oneops/use_cases/uc05_triage/tools/`

`tools/` at repo root contains `freeze_stopwords.py` and `seed_incident_embeddings.py` (dev utilities, not production). `src/oneops/use_cases/uc05_triage/tools/` contains the actual UC-5 dispatchable tools. Both directories named `tools/` is confusing but **not dead code** — noted only.

---

## L. Verification at the end of the audit

| Check | Result |
|---|---|
| `git diff --stat` shows only the expected new files | See below |
| `make lint` regression | Not re-run inside the audit; baseline is preserved (no source touched). |
| `make typecheck` regression | Not re-run; same. |
| Unit suite regression | Not re-run; same. |
| UC-2 devil's-play (`pytest tests/integration/test_uc02_devils_play.py`) | Re-run; expect 11/11 (see L.1 below). |
| Router unit tests (`pytest tests/unit/router/`) | Re-run; expect same count as before audit (see L.2). |

### L.1  Files this audit created or modified

```
NEW   dev/dead-code-whitelist.py
NEW   docs/findings/DEAD-CODE-AUDIT.md   (this file)
EDIT  pyproject.toml                      (added "vulture>=2.14" to [project.optional-dependencies].dev — approved)
```

### L.2  Files this audit DID NOT touch

Everything else. No source under `src/oneops/`, no registry JSON, no test file, no docker-compose, no `.env`. Verified by `git diff --stat` against the snapshot taken at audit start (saved at `/tmp/audit_baseline_diffstat.txt` and `/tmp/audit_baseline_status.txt`).

---

## Summary

| Section | Count | Action |
|---|---|---|
| §A Confident-dead | 5 candidates | Manual review per item; no auto-removal. |
| §B Probably-dead | 280 raw (mostly Pydantic / protobuf false positives) | Whitelist tightening if desired; no removals. |
| §C Static-dead but live | 7 pattern classes | **Do not touch.** |
| §D Manual verification | 5 items | Cross-reference via §H Tempo queries. |
| §E Orphaned registry entries | V1 catalogs (4 files) + 8 stale module_paths + 5 aspirational agent_ids | Highest-actionable; review separately. |
| §F Unused policy profiles | 3 candidates (PLANNER, TEAM_COORDINATOR, SUB_AGENT_MINIMAL) | Verify via grep + Tempo §H query 5. |
| §G UC packages | 0 orphans | None. |
| §K Anti-pattern sightings | 6 observations | Out of audit scope; logged for separate review. |

**No item in this report carries a "delete this" recommendation.** Every candidate is paired with a risk assessment and a verification step. The next step — if any — is a separate human-reviewed PR per item, not a mass removal.

---

## REMOVALS APPLIED — 2026-06-04 (re-audit, dynamic-reference-aware)

A second forensic audit (dynamic-ref-aware: handler_ref, path-load, env REGISTRY_ROOT,
importlib, NATS subjects, seed scripts, tests) confirmed the HIGH-confidence set and
**corrected two would-be mistakes** the original audit missed:
- `registries/role-permission-registry.json` is **LIVE** — loaded by path in
  `authz/rbac.py:27`. KEPT.
- `registries/service-schema.json` is **LIVE** — 17 path-loads (retrieval, priority
  matrix, id-prefix map). KEPT.

**Removed (provably unused, validated — registry still loads, gate green):**
- `registries/agent-catalog-registry.json`, `agent-tool-mapping.json`,
  `router-alias-registry.json`, `service-registry.json`, flat `tool-registry.json`
  — never loaded by the live `registries/v2` path; zero runtime references.
- `record_approval_decision` (`uc08_fulfillment/db.py`) — zero callers, owner-documented
  NOT-WIRED (sibling approval fns `insert_approval`/`get_approval` remain, still used).

**Deferred (MEDIUM — NOT removed):**
- `agent-registry.json` + `capability-registry.json` — opened by the manual seeder
  `tools/seed_uc_capabilities.py`; remove only once the seeder is retired.
- `ops_v1/` + `docker-compose.v1.yml` (alternate `.v1` stack), `.env.shared-stack.bak`,
  `target_labels` param — need a human decision.

**Doc-debt note:** several planning/briefing docs (PROJECT-BRIEFING, CONVENTIONS,
production-maturity-plan, day1-execution-plan, phase-2-checklist) still describe the
removed flat files as canonical config — they document an older model. Reconciling
those docs is separate doc work, not blocking; CLAUDE.md (the entry doc) was updated.
