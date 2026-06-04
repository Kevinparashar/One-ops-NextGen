---
title: PMG Coverage Gap Analysis — 22-Doc Target vs §F-LOCKED 7-Step Day-1 Cut
prepared_by: NextGen AI Platform
date: 2026-05-31
status: Active — drives §G #3 manager decision + Studio scoping
related: docs/production-maturity-plan.md, registries/, src/oneops/
---

# PMG Coverage Gap Analysis — what the 7-step cut covers vs the 22 docs

## Headline

The §F-LOCKED Day-1 cut delivers **~80% of the 22-doc target by its own admission**:

> *"Expected Day-1 coverage of 22 docs: ~80%. The remaining ~20% [...] remains named-but-deferred in §A.7 Scoping commitments with rationale and roadmap weeks."*
> — `docs/production-maturity-plan.md` §F-LOCKED

This file decomposes the gap honestly so it can drive: (a) the manager decision package (§G), (b) the Studio scoping ask (§G #3), and (c) the demo deck framing.

---

## What the 7 steps DO cover, by manager axis

| Axis | What 7 steps deliver | Coverage |
|---|---|---|
| **1. Agent lifecycle** | Lifecycle state machine + UC-5 + UC-8 registered as `active` | 🟢 ~70% |
| **2. Performance tracking** | SLO alerts + per-tenant cost board + synthetic probes | 🟢 ~80% |
| **3. Output validation** | UC-5/UC-8 tests + evidence logs; existing smoke + devil's-play green | 🟡 ~40% |
| **4. Security** | Tenant isolation already in place; no new security work | 🔴 ~25% |
| **5. DevEx / new capabilities** | UC-8 demonstrates "new UC = registry + handler" | 🟢 ~60% |
| **6. End-to-end automation** | Local CI gate (no GitHub) | 🟡 ~30% |

---

## What the 7 steps DO NOT cover (the missing ~20%)

These are explicitly deferred in §A.7 + §G with deferral rationale — but the manager **will** ask about them at sign-off.

### Security (axis 4) — biggest deferred bucket
- ❌ **Front-door JWT verification** (P0-#2) — "trust customer IdP today" remains
- ❌ **Materialized RBAC `(role × tool)` matrix** (P0-#3)
- ❌ **Hash-chained immutable audit log** + RTBF endpoint (P1)
- ❌ **Reversible PII token store** (DOC-04 §6) — current scrub is irreversible
- ❌ **Cross-tenant adversarial CI** (1000 attempts) (P0-#6)
- ❌ **TenantContext immutable** — gated on §G #8

### Output validation (axis 3)
- ❌ **Prompt-regression CI gate** (P0-#4, full version)
- ❌ **Drift detector** + per-UC quality score rollup
- ❌ **RAG faithfulness check** as a hard gate
- ❌ **Explainability trace store** (ClickHouse per target)
- ❌ **Misclassification Detector** (DOC-05 §4.2.8) — Workstream 3.1

### Lifecycle (axis 1)
- ❌ **`AgentManifest` export/import** (P0-#1 part 2) — the doc puts it in Day 2; Day 2 has not been executed
- ❌ **A/B traffic split** via Istio (P2)
- ❌ **Per-tenant catalog overlay** (P1)
- ❌ **Quality-gated promotion** tied to lifecycle (P1)

### DevEx (axis 5)
- ❌ **Scaffolding CLI** `oneops scaffold uc` (P1)
- ❌ **Intent ontology** (DOC-10) — blocked on §G #6 manager decision
- ❌ **`DATA_PRODUCT_REGISTRY`** (CLAUDE.md) — not shipped

### End-to-end automation (axis 6) — second-biggest gap
- ❌ **Real CI/CD pipeline** with IaC, canary, auto-rollback (P0-#7 full)
- ❌ **Terraform / Helm / ArgoCD** (P1)
- ❌ **EKS + Istio + Lambda + Dragonfly cluster + 3-node NATS + DR** (P1) — blocked on §G #1
- ❌ **Chaos suite** (P2)
- ❌ **Secret manager, image signing, SBOM, dep scanning, egress allow-list** (P1)

### Capability surface (axes 1 + 5)
- ❌ **DOC-08**: WebSocket-first chat, Bridge Service, webhooks, ChatOps (Slack/Teams), language SDKs — blocked on §G #2
- ❌ **DOC-09 ITOM**: UC-9 event correlation, UC-10 alert triage, UC-11 runbook automation, UC-12 change-risk graph, UC-13 capacity planning, UC-14 proactive discovery — blocked on §G #4
- ❌ **DOC-13A platform UCs**: UC-15..UC-29 + 40+ tools + **OneOps Studio** — blocked on §G #3 + #4
- ❌ **IT Operations Graph as load-bearing data product** — blocked on PMG positioning call

---

## The OneOps Studio dimension (architectural endpoint)

Recorded after the 2026-05-31 gap conversation: the user has confirmed the strategic intent is **OneOps Studio** — a no-code agent author plane where:

1. Users describe an agent in **natural-language text**
2. The system compiles to registry records (`agent-catalog` + `agent-tool-mapping`)
3. Agents use **pre-defined cross-service tools** from the tool catalog
4. Author + runtime are gated by **RBAC + ABAC** (twice-enforced per DOC-13A §7)

### Why this reframes the 7-step plan

If Studio is the architectural endpoint, then most hand-coded UCs (UC-8 included) become **proofs of substrate, not the product itself**. The factory wins at PMG over yet-another-UC.

### What Studio actually needs

| Component | What it does | Have today? |
|---|---|---|
| **Registry-driven architecture** | Agents/tools/capabilities/roles all data | ✅ 9 registries shipped |
| **Pre-defined tool catalog** | Library of cross-service tools (summarize, find_similar, lookup_kb, notify, assign, …) | 🟡 Partial — tools are UC-coupled today, not declared cross-service |
| **Policy + LLM gateway** | The compiler is itself an LLM call; goes through gateway | ✅ Done |
| **Tenant isolation + ABAC** | Authored agents respect tenant + risk class | ✅ Code present (`src/oneops/authz/`) |
| **Lifecycle state machine** | `draft → test → active → deprecated → retired`; Studio drops into `draft` | ❌ Pending (task #14) |
| **Twice-enforced RBAC `(role × tool)` matrix** | Author cannot grant tools they don't have; runtime re-checks | 🟡 Partial — runtime exists, author-time gate missing |
| **NL → Manifest compiler** | LLM reads text, emits `AgentManifest` (catalog row + tool refs + activation cond + abac tags + policy profile) | ❌ Not built |
| **Sandboxed test runner** | Run authored agent against demo fixtures before activation; capture trace | ❌ Not built |
| **Activation workflow** | Approval / quality gate before `active`; rollback = ref-point change | ❌ Not built |
| **Studio UI** | Text input + tool catalog browser + sandbox trace viewer + activate button | ❌ Not built |

### Studio-aware re-ordering recommendation

| New order | Item | Why |
|---|---|---|
| 1 | **Lifecycle state machine** (was step 3) — `draft / active / deprecated / retired` enforced at boot | Foundation Studio writes into |
| 2 | **Cross-service tool catalog refactor** | Surface every existing tool as a Studio-pickable building block; declare `consumed_capabilities` + `required_role` + `abac_tier` on each |
| 3 | **Twice-enforced RBAC `(role × tool)` matrix** | Author-time gate — Studio can't allow what the user can't perform |
| 4 | **NL → Manifest compiler** | The actual Studio brain — text in, `AgentManifest` out (signed JSON) |
| 5 | **Sandboxed test runner** | Run authored agent against demo fixtures; trace captured |
| 6 | **Studio UI** | Single page: textarea + tool catalog + sandbox button + activate button |
| 7 | **Manager decision package** (was step 7) — including the §G #3 ask **"Studio in or out of PMG sign-off?"** | Without this answer, all the above is risk-on |
| 8 | **End-to-end demo** — user types *"Create an agent that finds high-priority VPN incidents and notifies the network team"* → compile → sandbox → activate → live | The flagship moment for PMG |
| later | UC-8 Fulfillment | Built **via Studio**, not by hand — proof the substrate generalizes |

### Studio scope estimate (MVP)

- Lifecycle state machine: 1 hr (already scoped)
- Cross-service tool catalog refactor: 4–6 hr
- Twice-enforced RBAC matrix: 1–1.5 days
- NL → Manifest compiler: 2 days
- Sandbox test runner: 1 day
- Studio UI (minimal): 1 day
- Glue + tests + evidence: 1 day

**Total: ~7–8 working days for a Studio MVP** that does text → compile → sandbox → activate. Cheaper than hand-coding UC-8 + UC-15..UC-29 individually.

---

## What this analysis drives

1. **The §G #3 manager decision becomes the critical-path question.** Until it is answered, every Studio investment is at risk; every hand-coded UC delays the factory.
2. **The 7-step plan is correct for "show progress" but wrong for "ship the factory".** Re-ordering above is the Studio-aware sequence.
3. **Honest demo deck:** show what's done, name what's deferred, attach roadmap weeks to each deferral. PMG sign-off in IT-software always lands better as *"we know exactly what's left and when"* vs *"we covered everything"*.

## Honest sign-off readiness summary

| Condition | If manager answers… | Then we are… |
|---|---|---|
| §G #1 (production = EKS migration?) — answers **"no, dev/staging + roadmap is fine"** | sign-off-ready at ~80% with the 7-step plan + clear deferral table | 2 weeks out |
| §G #1 — answers **"yes, must be on EKS / Istio / IaC"** | 6–10 weeks out | not Day-2 work |
| §G #3 (Studio in scope?) — answers **"yes, demo Studio"** | the 7-step plan is the **wrong** sequence; pivot to Studio MVP (~8 days) | new critical path |
| §G #3 — answers **"keep Studio separate"** | continue the 7-step plan, defer Studio to a separate milestone | as-is |

---

## Cross-references

- `docs/production-maturity-plan.md` §A, §C, §F-LOCKED, §G — source of truth for axes + locked plan + open questions
- `registries/` — agent-catalog, tool-registry, capability-registry, agent-tool-mapping, role-permission-registry, service-registry, service-schema — the substrate Studio writes into
- `src/oneops/authz/` — RBAC + ABAC + token + decision cache primitives Studio's twice-enforcement composes on
- `src/oneops/policy/` — policy composer the Studio compiler must route through
- Memory: `project_oneops_nextgen_2026_05_31_full_session.md` — current substrate state
