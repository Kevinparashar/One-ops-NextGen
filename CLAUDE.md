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
9. **[`registries/`](registries/)** — data-driven configuration (`agent-catalog`, `tool-registry`, `capability-registry`, `agent-tool-mapping`, `role-permission-registry`, `service-registry`, `service-schema`, `router-alias-registry`).

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
