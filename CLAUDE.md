# CLAUDE.md — AI collaborator handshake

This is the first file any AI (or new engineer) should read.

## The rule set

The non-negotiable rules are at **[`docs/PROJECT-BRIEFING.md §2`](docs/PROJECT-BRIEFING.md)**. Treat them as the canonical list — they are versioned with the architecture and are what every PR is judged against.

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
2. **[`docs/PROJECT-BRIEFING.md`](docs/PROJECT-BRIEFING.md)** — rules, repo map, runtime stack, routing pipeline, DevEx contract.
3. **[`docs/production-maturity-plan.md`](docs/production-maturity-plan.md)** — what production-mature means here, gap matrix, P0/P1/P2 roadmap.
4. **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — target architecture, component contracts, sequence flows, ADRs index.
5. **[`docs/CONVENTIONS.md`](docs/CONVENTIONS.md)** — coding conventions (naming, types, docstrings, errors, spans, tests).
6. **[`docs/COMPONENT_SPEC.md`](docs/COMPONENT_SPEC.md)** — the C1–C24 contract every component is built to.
7. **[`RUNBOOK.md`](RUNBOOK.md)** — operator procedures.
8. **[`docs/observability/architecture_map.md`](docs/observability/architecture_map.md)** — canonical span tree + metric inventory.
9. **[`registries/`](registries/)** — data-driven configuration. The **live registry the service loads is [`registries/v2/`](registries/v2/)** (`registry.loader.load_registry`, default `REGISTRY_ROOT=registries/v2`): per-agent `agents/*.json`, per-tool `tools/*.json`, plus `service-schema.json`, `glossary.json`, `field_policy.json`, `policy_rules.json`. Remaining flat files at `registries/` root: `service-schema.json` + `role-permission-registry.json` (loaded by path — service-schema by retrieval/priority, role-permission by `authz/rbac.py`) and `agent-registry.json` + `capability-registry.json` (read by the manual `tools/seed_uc_capabilities.py` seeder). The legacy flat catalog files (`agent-catalog-registry`, `agent-tool-mapping`, `router-alias-registry`, `service-registry`, flat `tool-registry`) were **removed 2026-06-04** as dead (never loaded by any runtime path).

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
