# Component Specification — POC-5-MW

The contract every component in this system is built to. Drawn from the
mandated ideology — **Moveworks**, **Parlant**, **Salesforce/AgentScript** — on
the **LangGraph** substrate, plus the project thumb rules. It is **not** derived
from the old version; the old version's approaches are explicitly out of scope.

A *component* here is any unit the registry declares and the executor runs: a
**UC agent** (`AgentRecord`) or a **tool handler** (`ToolRecord` + its
`(arguments, context)` callable). Every requirement below is checkable. A
component is not "done" until all that apply to it hold.

---

## A. Declarative identity — *agents-as-data (AgentScript)*

**C1 — Registry-declared.** A component exists as a versioned registry record
(`AgentRecord` / `ToolRecord`), not as hardcoded behaviour. Adding, changing,
or retiring a component is a registry edit — no code change for what is data.

**C2 — Hot-reloadable & versioned.** Every record carries a version and an
`active_version`. A new version can be activated without redeploying code; the
old version stays resolvable for in-flight work. No behaviour is pinned to a
process lifetime.

**C3 — Owns its metadata.** Description, owner, intent family, ABAC tags,
policy refs, tool refs — all live on the record. Code reads them; code does not
re-state them.

## B. When a component runs — *activation conditions (Parlant)*

**C4 — Declares its activation condition.** Every component declares, as a
structured `ActivationCondition` over typed signals (intent, role, ABAC,
present-entities, focus…), exactly *when* it is eligible. It enters the routing
shortlist or the prompt **only** when that condition holds — protecting the
attention budget and preventing false invocation.

**C5 — No keyword/phrase matching.** Eligibility and routing never branch on raw
user-text substrings. Conditions are semantic principles over signals; the LLM
disambiguates an already-narrowed, already-eligible set. (Thumb rule: no static
keyword catalogs.)

**C6 — Declares dependencies & exclusions (Parlant).** A component declares what
it depends on and what it is mutually exclusive with, as data — so the planner
composes and the policy layer enforces, rather than logic discovering this at
runtime.

## C. Inputs & outputs — *structured outputs (Moveworks)*

**C7 — Typed, validated I/O.** Inputs and outputs are declared schemas
(`ToolParameter`s, an output contract), validated at the boundary. No loose,
free-form dicts crossing component boundaries.

**C8 — Structured output, not prose.** A component returns a structured result
object — status, data, and (where relevant) a user-facing message field —
so downstream steps, the aggregator, and the attention budget operate on a
contract, not on string parsing.

**C9 — Attention budget.** A large output is moved to the variable store and
replaced by a reference/preview. Only what a downstream step needs enters its
prompt. Components never dump unbounded payloads into the LLM context.

## D. Logic placement — *determinism dial (AgentScript) + move logic out of the LLM (Moveworks)*

**C10 — Deterministic by default.** Any decision that can be made by rule, data,
or schema **is** — validation, redaction, routing filters, formatting, ID
handling. The LLM is used only for genuine natural-language judgment
(classification, disambiguation, response composition).

**C11 — Declares its determinism level.** Each component states where it sits on
the determinism dial. Raising LLM involvement is a deliberate, declared choice,
not a default.

**C12 — No static catalogs.** No hardcoded keyword lists, field lists, redaction
lists, prefix tables, or enum catalogs in code. Such data lives in the
registry/schema and is read at runtime. Descriptions and rules are *semantic
principles*, never phrase lists. (Thumb rules: no static approaches; principle,
not phrasebook.)

## E. Safety, tenancy, governance

**C13 — Tenant-scoped always.** Every component is multi-tenant. `tenant_id`
comes from the request envelope, never from user text. Data access is scoped to
the tenant at the data layer; a component can never see another tenant's data.

**C14 — RBAC + ABAC enforced.** Access is checked against the caller's role and
the component's ABAC tags before work runs. Denials are explicit outcomes, not
silent drops.

**C15 — Every LLM call through the policy layer.** No hand-crafted system
prompts. Prompts are composed through the policy engine (safety, RBAC, tenant
guards, anti-fabrication). Compliance-critical answers use **canned responses
(Parlant)** — declared, not free-generated.

**C16 — Lifecycle hooks (AgentScript).** Components run through before/after
hooks for validation and policy gating. Action-tier components pass an approval
interrupt. Hooks are idempotent.

## F. No silent failure — *thumb rule #11*

**C17 — Always responds, per context.** Every path — success, not-found,
denied, malformed input, upstream error — returns an explicit, structured
result that can be turned into a context-appropriate user message
(answer, clarification request, or canned decline). A component never returns a
bare `None`, never silently drops, never guesses to paper over a gap.

**C18 — Failures are typed and contained.** A handler fault becomes a typed
failed result; it never escapes as an unhandled exception. Timeouts and
upstream errors are surfaced as typed outcomes.

## G. Reliability & runtime

**C19 — Idempotent.** Action-tier components are idempotent — re-delivery
(NATS at-least-once) must not double-apply. Read components are naturally safe.

**C20 — Observable.** Every component emits OTel spans and the standard metrics
(invocation, latency, status). Tracing is not optional.

**C21 — Pluggable backends.** Any infrastructure dependency (DB, LLM, cache,
queue) is reached through an interface with a deterministic in-memory backend
**and** a live backend. The in-memory backend is the default; the live backend
is env-gated.

## H. Engineering discipline — *thumb rules*

**C22 — Tested hard.** Unit tests run with zero infrastructure (in-memory
backends), cover the golden path **and** edge/adversarial cases, and assert real
behaviour — content, state, trace, payload. Live-infra tests are env-gated.

**C23 — Placement.** UC-specific code lives in that UC's folder; shared,
UC-agnostic code lives in shared modules. No UC specifics leak into common code.

**C24 — RCA before fix, devil's-advocate after.** A change is preceded by root-
cause analysis and followed by an adversarial review of what could still break.
No hot fixes.

---

## How to use this spec

- **Building a component:** every requirement that applies must hold before it
  is considered done.
- **Reviewing/auditing:** walk C1–C24; each is a pass/fail check.
- **A drift is a defect:** if a component violates a Cn, fix the component —
  do not weaken the spec.

This document is the anchor. UC-1's handler correction and UC-3's fresh design
are both built and reviewed against it.
