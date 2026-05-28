---
title: Internal Cleanup Notes — Code/Doc Inconsistencies
audience: Engineering (internal only — do not link from PMG docs)
date: 2026-05-27
---

# Internal Cleanup Notes — Code/Doc Inconsistencies

This is a developer-facing list of inconsistencies found during the Phase 1 PMG-validation discovery scan. None of these are visible in the PMG documentation (which is grounded in code, not in the README claims), but each item should be fixed in the source so the public-facing story stays consistent over time.

> **Do not link this file from anything in `docs/pmg-validation/`.** It is for the engineering team only.

---

## 1. Stale entry points in `pyproject.toml`

**Problem.** `pyproject.toml` declares console-script entry points such as `oneops-graph = "oneops.entry.graph_service:main"` and `oneops-uc1 = "oneops.entry.uc_service:run_uc1"`. The `oneops.entry` module does not exist in the codebase.

**Reality.** The actual entry is `src/oneops/api/app.py` started via `uvicorn`. Workers are `src/oneops/workers/graph_worker.py` and `src/oneops/workers/agent_worker.py`.

**Recommended fix.** Either implement the `oneops.entry` shims so the declared entry points work, or remove the stale declarations from `pyproject.toml` and document the real run commands in the README.

**Risk if left.** A new engineer running `pip install -e . && oneops-graph` will hit an import error and waste time.

---

## 2. README and RUNBOOK reference unused JetStream

**Problem.** The NATS container is started with `-js` (JetStream enabled), and the RUNBOOK references JetStream durable-stream semantics in places. No code in the repository actually uses JetStream — all messaging is core NATS request/reply.

**Reality.** Durable, on-disk-persistent messaging is infrastructure-ready but not exercised. It is genuinely *built, not yet active*.

**Recommended fix.** In the RUNBOOK, mark the JetStream sections as *"future capability — not yet wired into code"*, or remove them until they are wired. In the README, do not claim durable messaging as a current capability.

**Risk if left.** Operators will look for JetStream traffic, find none, and assume something is broken.

---

## 3. README implies three working use cases

**Problem.** Top-level README and `ARCHITECTURE.md` describe three live use cases. Code has two live use cases (Summarization, Knowledge Lookup) plus a conversational fallback. The third, *Action on a Ticket*, is designed and partially scaffolded but has no working handlers.

**Reality.** Phase status documents inside the repo correctly mark the action use case as *not built*. The top-level README has not been updated to match.

**Recommended fix.** Update the README's *Use Cases* section to list two live use cases and one planned, with the same status labels used in the PMG documentation (*Done* / *Planned*).

**Risk if left.** External readers of the README will be misled about the current capability surface, contradicting what PMG presents to customers.

---

## 4. `UserProfileStore` protocol declared but methods not implemented

**Problem.** `src/oneops/session/profile_store.py` declares a `UserProfileStore` protocol with three core methods, each raising `NotImplementedError`. No call sites use it.

**Reality.** Cross-session user profile memory is a *Planned* capability. The protocol was scaffolded ahead of implementation.

**Recommended fix.** Either implement at least an in-memory backend so the interface is exercised in tests, or annotate the protocol with a `# Planned — not yet implemented` docstring so future readers do not assume it is wired.

**Risk if left.** Someone wires a call site against the protocol expecting it to work and discovers at runtime that it does not.

---

## 5. `AgentWorker` built but not wired into the API path

**Problem.** `src/oneops/workers/agent_worker.py` is a complete, tested worker that subscribes to per-agent NATS subjects and runs the same handler logic the in-process executor runs. Nothing in the API path dispatches work to it.

**Reality.** Agent-to-agent dispatch is the foundation for multi-step workflows, gated on the action use case shipping first. The PMG documentation describes this honestly as *built, not yet active*.

**Recommended fix.** Add a `README` inside `src/oneops/workers/` clarifying which workers are live (`GraphWorker`) and which are foundation for future capability (`AgentWorker`). Optionally, gate the `AgentWorker` startup behind a feature flag so it is not accidentally launched in production without the orchestration layer.

**Risk if left.** A future developer or operator may start the `AgentWorker` expecting it to do something useful and be confused that no traffic reaches it.

---

## 6. Routing-mode terminology drift

**Problem.** Internal documents use `legacy` and `three_stage` as routing-mode names. PMG documentation avoids these terms entirely, preferring *simpler deterministic router* and *smarter context-aware router*.

**Reality.** No code change needed — this is purely a documentation alignment.

**Recommended fix.** When updating internal docs, add a one-line cross-reference to the PMG-friendly names so future authors writing for either audience use consistent language.

**Risk if left.** Minor. Different audiences see the same component called different things.

---

## 7. LiteLLM cost-tracking responsibility is split

**Problem.** The application-layer `CostTracker` records per-tenant per-model spend. The LiteLLM proxy has its built-in database-backed spend tracking explicitly disabled (with a comment referencing the 2026-05-16 data-loss incident). The split is intentional and correct, but it is not documented anywhere a future engineer would find it.

**Reality.** Application-layer tracking is authoritative; LiteLLM proxy is intentionally stateless for spend purposes.

**Recommended fix.** Add a short note to the LLM Gateway module docstring (or a `decisions/` ADR) explaining that LiteLLM spend tracking is intentionally disabled and the application is the single source of truth.

**Risk if left.** A future engineer may enable LiteLLM spend tracking thinking it is missing, re-introducing the failure mode that caused the 2026-05-16 incident.

---

## Summary

None of these inconsistencies affect the live behavior of the platform. Each is a documentation or scaffolding drift that, left unaddressed, will cost a future engineer's time or create a contradiction with the PMG-facing story.

Recommended priority: items 1, 2, and 3 first (visible to anyone reading the README); item 5 next (operational risk); items 4, 6, 7 as housekeeping.
