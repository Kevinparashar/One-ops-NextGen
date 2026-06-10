# ADR-0003 — Policy engine: embedded data-driven evaluator

**Status:** Accepted · 2026-05-20
**Decision owners:** AI Platform Architect

## Context

`updated_policy_v2.md` and the policy-as-data files are the authoritative
source for guardrails, content policy, tenant policy hooks, and canned-response
selection. Requirements:

- A policy change must deploy **without a code change or redeploy**.
- Agents and tools query policy at runtime, on the hot path — must be fast.
- Decisions must be auditable: which policy rule fired, why.
- The brief asks for an "OPA-style" engine.

## Options

| Option | Hot-reload | New runtime component | Language | Audit |
|---|---|---|---|---|
| **Embedded data-driven evaluator** | Yes — reload policy data into the cache | None | Policy is structured data (YAML/JSON), evaluated by our code | Native — every eval traced |
| OPA sidecar (Rego) | Yes | A sidecar per service to deploy/monitor/secure | Rego — a second policy language to learn and maintain | Via OPA decision logs |
| Policy hardcoded in agents | No (needs redeploy) | None | Python | Poor |

## Decision

An **embedded, data-driven policy engine**: policy is versioned structured
data; a deterministic evaluator in-process loads it (hot, from Dragonfly;
cold, from the policy files) and answers policy queries. The repo's existing
`src/oneops/policy/` (`compose`, `blocks`) is the seed of this evaluator and
is extended, not replaced.

## Why embedded over OPA

"OPA-style" is satisfied by **policy-as-data with hot-reload and traced
decisions** — and that is the actual requirement. A full OPA deployment adds a
sidecar to every service and **Rego as a second policy language** that every
policy author must learn. For a 5-year system the cost of a second language
and an extra runtime component is real and recurring; the benefit (Rego's
expressiveness) is not yet needed. The embedded evaluator delivers the
deploy-without-code-change property — which is the point — without that cost.

## Consequences

- Policy files are versioned; a policy change is a data deploy, picked up via
  cache invalidation — no service redeploy.
- Every policy evaluation emits an OTel event naming the rule and outcome.
- Canned-response selection (high-stakes touchpoints) is one policy decision
  type among others.
- The evaluator's rule grammar is **our** structured schema — kept
  deliberately small; complexity that would need Rego is a signal to revisit.

## Exit plan / revisit trigger

If policy logic genuinely outgrows a structured-data grammar — needs
recursion, complex set logic, partial evaluation — adopt OPA then. The policy
*data* would migrate to Rego; the *query interface* agents use stays stable,
so the blast radius is the evaluator only. Revisit when a policy author first
hits an expressiveness wall the structured grammar cannot cleanly meet.
