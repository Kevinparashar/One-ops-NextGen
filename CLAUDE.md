# CLAUDE.md — AI collaborator handshake

This is the first file any AI (or new engineer) should read.

## Rule 0 — Production-grade, always

**Whenever we code, fix, or choose an approach to build something, it must be
production-grade — never a hot-fix.** Solve the root cause, not the symptom.
Build for sustainability, scale, and maintainability — the way it should live in
production — even when a quicker patch would "work" for now. Disabling a feature,
papering over a failure, or shipping a temporary shortcut is not a fix. If a true
fix isn't possible in the moment, surface the trade-off and the production path
rather than quietly shipping the shortcut. This rule overrides any pressure to
just-make-it-pass.

## The rule set

The non-negotiable rules are at **[`docs/briefings/PROJECT-BRIEFING.md §2`](docs/briefings/PROJECT-BRIEFING.md)**. Treat them as the canonical list — they are versioned with the architecture and are what every PR is judged against.

A quick index of the 13 rules in §2 (read the full text before relying on them):

- §2.1 Descriptions are semantic principles, not phrase catalogs
- §2.2 LLM is the decision maker; prompts are dynamic
- §2.3 Policy layer mandatory for every LLM call
- §2.4 Tenant isolation is structural, not advisory
- §2.5 Single egress through `src/oneops/llm/gateway.py`
- §2.6 Observability is not optional
- §2.7 No silent failures
- §2.8 LangGraph-first for state, retries, caching, fan-out
- §2.9 Production-grade testing on every change
- §2.10 No file bloat, no premature abstraction
- §2.12 Don't ask, drive
- §2.13 Engineering principles checklist

## Read order

1. **This file** — handshake.
2. **[`docs/briefings/PROJECT-BRIEFING.md`](docs/briefings/PROJECT-BRIEFING.md)** — rules, repo map, runtime stack, routing pipeline, DevEx contract.
3. **[`docs/planning/production-maturity-plan.md`](docs/planning/production-maturity-plan.md)** — what production-mature means here, gap matrix, P0/P1/P2 roadmap.
4. **[`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md)** — target architecture, component contracts, sequence flows, ADRs index.
5. **[`docs/architecture/CONVENTIONS.md`](docs/architecture/CONVENTIONS.md)** — coding conventions (naming, types, docstrings, errors, spans, tests).
6. **[`docs/architecture/COMPONENT_SPEC.md`](docs/architecture/COMPONENT_SPEC.md)** — the C1–C24 contract every component is built to.
7. **[`docs/runbooks/RUNBOOK.md`](docs/runbooks/RUNBOOK.md)** — operator procedures.
8. **[`docs/observability/architecture_map.md`](docs/observability/architecture_map.md)** — canonical span tree + metric inventory.
9. **[`registries/`](registries/)** — data-driven configuration. The **live registry the service loads is [`registries/v2/`](registries/v2/)** (`registry.loader.load_registry`, default `REGISTRY_ROOT=registries/v2`): per-agent `agents/*.json`, per-tool `tools/*.json`, plus `service-schema.json`, `glossary.json`, `field_policy.json`, `policy_rules.json`. Remaining flat files at `registries/` root: `service-schema.json` + `role-permission-registry.json` (loaded by path — service-schema by retrieval/priority, role-permission by `authz/rbac.py`) and `agent-registry.json` + `capability-registry.json` (legacy routing-corpus seed inputs — their seeders `tools/seed_uc_capabilities.py` + `tools/seed_uc_embeddings.py` were **removed 2026-06-06** as dead: they targeted the `uc_capabilities` / `agent_capability_embeddings` tables, which the runtime never created or read. The replacement is the registry→DB path: `itsm.agent` + `ai.embeddings_agent`). The legacy flat catalog files (`agent-catalog-registry`, `agent-tool-mapping`, `router-alias-registry`, `service-registry`, flat `tool-registry`) were **removed 2026-06-04** as dead (never loaded by any runtime path).

**DB-ops layout (2026-06-06):** all database operations live under **`database/`** as **per-service vertical slices** — apply-order authority is `database/README.md`. Each service folder (`incident/`, `request/`, `kb/`, `catalog_fulfillment/`, `agent/`, `tool/`, `uc_schema/`, `conversation/`) owns its own `01_schema.sql`, `02_embeddings.sql` (own pgmq queue `embedding_refresh_<service>` + trigger), `load_data.py`/`sync.py`, `backfill.py`, and `worker.py`. Shared: `_foundation/` (extensions, schemas, reference tables), `_lib/` (the shared-code package: `_loader.py` load mechanics + `_worker_base.py` worker poll/ack loop), `_utils/` (whole-DB utilities — named `_utils/` not `_tools/` to avoid confusion with the `tool/` service slice). Embedding workers are **separate per-service processes** (`python database/<service>/worker.py`), NOT started by the API. Top-level `scripts/` is eval/CI only; `dev/` holds build/codegen + developer helpers (`gen_proto.sh`, `freeze_stopwords.py`, `migrate_registry_v2.py`, `dead-code-whitelist.py`) — the old top-level `tools/` folder was removed 2026-06-06 (merged into `dev/`) to avoid confusion with `database/tool/`.

After steps 1–4 an AI has enough context to do a production-grade change.

## Response format

When completing work, return:

- **Summary** — one paragraph, what changed and why.
- **Root Cause** — only for fixes; what the actual defect was.
- **Architecture Impact** — which boundary owns the new behaviour; which ADRs / rules apply.
- **Files Changed** — paths with one-line per-file justification.
- **Implementation Details** — load-bearing decisions; cite rules / ADRs.
- **Validation Performed** — exactly what was run (no claims without commands).
- **Risks** — honest, sized.
- **Remaining Gaps** — what was deferred and why.
- **Recommended Next Step** — one concrete action.

Be concise. Be direct. Don't hide trade-offs. Don't overstate confidence.

## What to skip

- Asking choice menus when you can drive (rule §2.12).
- Editing platform code to solve what is a registry / activation-condition / policy concern (rule "agents are data").
- Adding keyword catalogs / synonym lists / phrase tables anywhere on the routing path (rule §2.1).
