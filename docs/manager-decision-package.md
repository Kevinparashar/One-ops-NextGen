---
title: Manager Decision Package — 10 Binding Questions for PMG Sign-Off Scope
prepared_by: NextGen AI Platform
date: 2026-05-31
status: Awaiting binding answers
related: docs/production-maturity-plan.md (§G), docs/pmg-coverage-gap-analysis.md
---

# Manager Decision Package

## What this is

Ten questions that **must be answered** before further engineering work can be scoped responsibly. Each question:

- States the conflict between the 22-doc target and the current POC-5-MW-1 state
- Recommends a binding answer + rationale
- Names the **cost of the alternative**
- Names the **downstream gates** the answer unlocks

These are not opinion questions. Each has a measurable downstream consequence in the roadmap.

---

## How to read this document

| Symbol | Meaning |
|---|---|
| ⏱ | Time impact (working days) per answer choice |
| 🚪 | Downstream gates this answer unlocks |
| ⚠️ | What you implicitly commit to if you pick this answer |
| 🧭 | Recommended answer (engineering call) |

The manager's job is to **accept or override** the recommendations. Either is fine. Ambiguity is not.

---

## Q1 — Scope of "production" for PMG sign-off

**The conflict.** Target architecture (DOC-11) is full AWS EKS + Istio + Lambda + multi-region DR. POC is on docker-compose. Sign-off can mean either.

| Option | What "production" means | ⏱ Time | ⚠️ Cost of alternative |
|---|---|---|---|
| **A** | Sign-off on the 2 live UCs at current dev/staging + a credible roadmap to EKS | ~2 weeks to ready | We owe a written roadmap with weeks per workstream |
| **B** | Sign-off requires the EKS / Istio / Lambda migration completed | 6–10 weeks | All UC work pauses for infra port; demo blocked until done |

**🧭 Recommendation: A.** EKS migration is a separate workstream sized 6–10 weeks (Workstream 4 in §E). Tying it to PMG sign-off compresses nothing — it just shifts the goal.

**🚪 If A:**
- §F-LOCKED 7-step plan stays the path
- Manager decision package (this doc) ships first
- Roadmap weeks become a hand-deliverable

**🚪 If B:**
- All UC engineering pauses
- Workstream 4 (EKS/Istio/Lambda/ArgoCD/DR) becomes critical path
- PMG demo pushed ~2 months

---

## Q2 — Bridge Service + front-door JWT — in or out?

**The conflict.** PMG Doc 4 says we trust the customer's IdP today. Target DOC-04 says Envoy RBAC + Bridge Service do full JWT verification at the front door.

| Option | What we ship | ⏱ Time | ⚠️ Cost of alternative |
|---|---|---|---|
| **A** | Front-door JWT verification + signed internal service JWTs (P0-#2) | ~2.5 days | Security posture remains "trust the customer IdP" — auditable but not strict |
| **B** | Defer to a later milestone | 0 days | Security review will flag this gap loudly |

**🧭 Recommendation: A**, scoped narrowly. The materialized RBAC matrix (Q3) needs a real principal identity. A minimal JWT layer (3rd-party IdP via OIDC, JWKS rotation, signed internal JWTs) is a 2-day deliverable that unblocks every downstream security item. Skip Envoy/Bridge — those are scale-time.

**🚪 If A:**
- P0-#3 (materialized RBAC matrix) becomes feasible
- Twice-enforced RBAC for Studio (Q3) becomes feasible
- DOC-08 work stays deferred but unblocked

**🚪 If B:**
- Manager owns the "trust the customer IdP" security stance publicly
- P0-#3 work loses its principal source

---

## Q3 — OneOps Studio: in or out of PMG sign-off?

**The conflict.** Target docs (the 22) do not have a Studio equivalent. Studio is a POC-5-MW-1 invention — a no-code agent author plane where users describe agents in text, the system compiles to registry records, and cross-service tools are gated by twice-enforced RBAC + ABAC. The user has confirmed Studio is the **architectural endpoint**.

| Option | What it means for the next 8 days | ⏱ Time | ⚠️ Cost of alternative |
|---|---|---|---|
| **A** | Studio is **in PMG scope**. Ship MVP (text → compile → sandbox → activate) | ~7–8 days | Hand-coded UC-8 (current §F-LOCKED step 4) becomes redundant — Studio would author it |
| **B** | Studio is **separate milestone**. Continue §F-LOCKED 7-step plan with hand-coded UC-8 | ~2 weeks | Demo story is "another UC" not "the factory" — weaker PMG narrative |

**🧭 Recommendation: A.** The factory is a stronger product story than yet-another-UC. Studio also automatically covers axis 5 (DevEx) and a chunk of axis 1 (lifecycle) — two birds. Cost of Studio MVP (~8 days) is comparable to hand-coding UC-8 + the lifecycle state machine the manager already wants.

**🚪 If A — Studio re-orders the work to:**
1. Lifecycle state machine (1 hr) — task #14
2. Cross-service tool catalog refactor (4–6 hr)
3. Twice-enforced RBAC matrix (1.5 days) — depends on Q2
4. NL → Manifest compiler (2 days)
5. Sandbox test runner (1 day)
6. Studio UI minimal (1 day)
7. Glue + tests + evidence (1 day)
8. End-to-end demo: "Create an agent that finds high-priority VPN incidents and notifies the network team" → compile → sandbox → activate → live

**🚪 If B:**
- §F-LOCKED 7-step plan is the path
- Studio becomes a separate ~3-week post-PMG workstream
- Hand-coded UC-8 + lifecycle + SLO + CI + demo runbook + evidence

---

## Q4 — UC catalogue scope (UC-15 to UC-29)

**The conflict.** POC has UC-1, UC-2, UC-3, UC-5 live. DOC-09 (ITOM) and DOC-13A (platform services) add UC-9..UC-29 (~22 more) — but those are designs only.

| Option | Scope | ⏱ Time | ⚠️ Cost of alternative |
|---|---|---|---|
| **A** | PMG sign-off is scoped to **live UCs + named-deferred catalogue + roadmap weeks** | 0 extra days | Manager accepts "we'll deliver the catalogue in milestones X..Y" |
| **B** | PMG expects **coverage commitments** for the 29-UC catalogue | 12–24 weeks | Multi-month scope balloon; all current Day-1 work is moot |

**🧭 Recommendation: A.** If Q3=A (Studio in), UC-15..UC-29 are not engineering-bound any more — they become Studio-authored. If Q3=B, this question forces a milestone plan.

**🚪 If A:**
- A named-deferred table with weeks per UC family lands in the demo deck
- Each UC becomes a workstream sized when activated

**🚪 If B:**
- 6-month-plus roadmap required
- Studio (Q3) likely re-enters scope because hand-coding 22 UCs is irrational

---

## Q5 — Audit store: ClickHouse now, or stay on Postgres?

**The conflict.** Target says ClickHouse for immutable audit + explainability trace store (DOC-05). POC uses Postgres append-only.

| Option | What we ship | ⏱ Time | ⚠️ Cost of alternative |
|---|---|---|---|
| **A** | Stay on Postgres append-only **with hash-chained immutability + RTBF endpoint** | ~2 days | "We'll move to ClickHouse at volume X" commitment owed |
| **B** | Adopt ClickHouse now | ~5 days | New ops surface to monitor; demo gains little |

**🧭 Recommendation: A.** Postgres + hash-chain gets us compliance-grade immutability at far lower ops cost. Move to ClickHouse only when query-throughput on audit becomes a real bottleneck. The Postgres path is reversible.

**🚪 If A:**
- P1 audit work scoped at ~2 days
- RTBF endpoint becomes a known deliverable

**🚪 If B:**
- ClickHouse adoption + connector + ops + migrations adds ~3-4 days

---

## Q6 — Intent ontology shape (DOC-10 / CLAUDE.md)

**The conflict.** The CLAUDE.md `INTENT_ONTOLOGY` is a 3-level taxonomy (`domain.category.action`) over ~50 leaf nodes with parent-child fallback. POC routing is **two-layer LLM** (control gate + disambiguator) over registry **descriptions** — explicitly **no phrase catalogues** per the descriptions-are-semantic-principles thumb rule (§2.1 of the codebase rules).

A closed taxonomy + customer-onboarding-into-it conflicts with §2.1 and regresses on new phrasings.

| Option | What ontology means | ⏱ Time | ⚠️ Cost of alternative |
|---|---|---|---|
| **A** | Ontology = **analytical / labelling layer** over LLM decisions (read-only telemetry classes) | ~1 day | Manager accepts that routing is LLM-decided, taxonomy is observability |
| **B** | Ontology = **closed taxonomy** customers must map into | breaks §2.1 + regresses on new phrasings | Re-adopt phrase catalogues; routing accuracy drops on novel verbs |

**🧭 Recommendation: A.** Routing decisions remain LLM-as-decider over descriptions (§2.1, §2.2). The ontology is bolted on as a labeller / telemetry index for dashboards + analytics, never as a routing key. This preserves the live 96.4% routing pass rate AND gives PMG the categorical view they expect.

**🚪 If A:**
- Workstream 5b ships in 1 day (labeller node + telemetry attribute)
- §2.1 remains intact

**🚪 If B:**
- Codebase rule §2.1 must be retracted
- Routing accuracy will degrade
- Demo regressions expected

---

## Q7 — Agent-to-agent autonomy: when does it activate?

**The conflict.** Transport (NATS subjects) is live. Orchestration is not. PMG Doc 4 says activation is gated on the Action UC.

| Option | When agent-to-agent goes live | ⏱ Time | ⚠️ Cost of alternative |
|---|---|---|---|
| **A** | Gate on Action UC as PMG Doc 4 says | post-Action-UC | Manager accepts deferred autonomy |
| **B** | Activate earlier (e.g., when UC-8 ships) | concurrent with UC-8 | Higher blast radius; risk class jump |

**🧭 Recommendation: A.** The substrate is ready (transport, policy, RBAC code), but autonomous agent-to-agent action requires the approval/rollback machinery of the Action UC. Activating earlier is a security regression for marginal demo gain.

**🚪 If A:**
- Status quo; Action UC drives activation
- Studio (Q3) ships *without* autonomy at first — agents authored, not auto-invoking

**🚪 If B:**
- Risk-class review required; Action UC accelerated

---

## Q8 — PII handling: reversible token store vs current scrub

**The conflict.** DOC-04 §6 wants **reversible detokenization** via a token store with TTL. POC redacts irreversibly at the LLM gateway (the outbound scrub).

| Option | What we ship | ⏱ Time | ⚠️ Cost of alternative |
|---|---|---|---|
| **A** | Adopt reversible token store | ~3 days | New ops surface (token store, TTL, rotation, detok at egress) |
| **B** | Stay with irreversible scrub | 0 days | Cannot reconstruct PII on egress — fine for chat, breaks workflows needing PII roundtrip |

**🧭 Recommendation: A** *but only if there is a clear use case for PII roundtrip*. Workflows like UC-8 Fulfillment (employee onboarding) need to *return* names + emails to the requester. If no such workflow ships in the milestone, stay with **B**.

**🚪 If A:**
- ~3 days added to security workstream
- Token-store ops bundle (rotation, TTL, audit) owed

**🚪 If B:**
- Current scrub stays
- Future UCs requiring PII roundtrip blocked until store ships

---

## Q9 — Per-request-type SLOs: contractual or aspirational?

**The conflict.** CLAUDE.md publishes hard SLOs:
- fast-path p95 < 2 s
- standard p95 < 6 s
- complex p95 < 12 s
- multi-turn p95 < 3 s
- AWS API GW hard cap 29 s, `REQUEST_TIMEOUT_SECONDS=25`

These drive the alert thresholds in P0-#5.

| Option | Status | ⏱ Time | ⚠️ Cost of alternative |
|---|---|---|---|
| **A** | Contractual — alerts page on breach, error-budget tracked | normal P0-#5 scope | Customer-facing SLA owed |
| **B** | Aspirational — alerts are dashboards, no pager | normal P0-#5 scope | Manager accepts "we report, we don't promise" |

**🧭 Recommendation: B for PMG sign-off, A for GA.** SLOs need an error-budget regime + on-call rotation to be honest contractual. Both take time to spool up. For PMG, demonstrating measurement + alerting is enough; "contractual" is a GA-time commitment.

**🚪 If A:**
- On-call rotation + error budget + customer SLA doc required
- Adds 2–3 days

**🚪 If B:**
- Standard P0-#5 work; current cost dashboard fix already lands a chunk

---

## Q10 — NATS topology: DOC-11 single 3-node vs CLAUDE.md dual cluster

**The conflict.** Target DOC-11 specifies a single 3-node NATS cluster. CLAUDE.md says **dual cluster**: `nats-ops` (orchestration) + `nats-obs` (observability).

| Option | Topology for POC sizing | ⏱ Time | ⚠️ Cost of alternative |
|---|---|---|---|
| **A** | Single 3-node NATS (DOC-11 canonical) | 0 days extra | Workstream 4 stays single-cluster |
| **B** | Dual cluster (CLAUDE.md canonical) | ~1 day infra extra | Twice the NATS ops surface |

**🧭 Recommendation: A.** Single-cluster is simpler and aligned with DOC-11. Dual-cluster's split (ops vs obs) is a scale-time pattern, not a Day-1 requirement. Revisit when obs traffic actually competes with orchestration on subject delivery — measured, not assumed.

**🚪 If A:**
- Workstream 4 EKS port uses single-cluster NATS
- CLAUDE.md updated to reflect

**🚪 If B:**
- Dual-cluster ops bundle owed
- 1 extra day on Workstream 4

---

## Summary table — recommended bindings

| # | Question | Recommended | If accepted, sized at | Cost of alternative |
|---|---|---|---|---|
| Q1 | Production = EKS migration? | **A** (no, dev/staging + roadmap) | 0 days extra | +6–10 weeks |
| Q2 | Front-door JWT in scope? | **A** (yes, narrow scope) | ~2.5 days | Security review flags it |
| Q3 | **Studio in PMG scope?** | **A** (yes) | ~8 days | Hand-coded UC-8 + weaker demo story |
| Q4 | UC-15..UC-29 commitment? | **A** (deferred + roadmap) | 0 days extra | 12–24 weeks of UC work |
| Q5 | ClickHouse for audit? | **A** (stay on Postgres + hash-chain) | ~2 days | +3 days ClickHouse |
| Q6 | Intent ontology shape? | **A** (labelling layer only) | ~1 day | §2.1 retraction + accuracy regression |
| Q7 | Agent-to-agent now? | **A** (gate on Action UC) | 0 days | Security risk regression |
| Q8 | Reversible PII store? | **A** if UC-8 ships; else **B** | ~3 days if A | UC-8 PII roundtrip blocked |
| Q9 | SLOs contractual or aspirational? | **B** (aspirational for PMG) | 0 days extra | +2–3 days on-call + SLA doc |
| Q10 | NATS topology? | **A** (single 3-node) | 0 days | +1 day dual-cluster ops |

**Total under all recommended answers: ~12–14 days to PMG sign-off-ready.**

---

## What we do **after** the manager signs the answers

The recommended bindings above commit us to a **Studio-first sequence (Path A)**. Concrete plan:

### Day 0 (today, post-sign-off)
- Update `docs/production-maturity-plan.md` §F-LOCKED with the new sequence
- Close out task #15 (hand-coded UC-8) and replace with "UC-8 via Studio" downstream
- Open new tasks for Studio MVP steps

### Day 1 — Foundation
- **Lifecycle state machine** (task #14): `draft / test / active / deprecated / retired`. Boot-time validation. Router refuses non-`active` agents with `lifecycle.refused` span. 1 hr.
- **Cross-service tool catalog refactor**: declare `consumed_capabilities` + `required_role` + `abac_tier` on every existing tool. Group tools by service-agnostic capability (summarize, find_similar, lookup_kb, notify, assign). 4–6 hr.

### Day 2 — Security gate
- **Front-door JWT verification** (Q2 acceptance): minimal OIDC + JWKS + signed internal JWTs. 1 day.

### Day 3 — RBAC matrix
- **Twice-enforced `(role × tool)` matrix**: materialized at boot from `role-permission-registry.json`. Author-time gate (Studio refuses to grant tools the user doesn't have); runtime gate (re-checks per invocation). 1.5 days. Depends on Day 2.

### Days 4–5 — Studio compiler
- **NL → Manifest compiler**: LLM call that reads text + tool catalog + user RBAC, emits signed `AgentManifest` JSON. Goes through the policy layer + LiteLLM gateway like every other LLM call. 2 days.

### Day 6 — Sandbox
- **Sandboxed test runner**: run the authored agent against demo fixtures, capture Tempo trace, block activation if test fails or quality score < threshold. 1 day.

### Day 7 — UI
- **Studio UI** minimal: single page — textarea + tool catalog browser + sandbox button + activate button. 1 day.

### Day 8 — End-to-end demo + evidence
- **End-to-end demo flow**: user types *"Create an agent that finds high-priority VPN incidents and notifies the network team"* → compile → sandbox → activate → live in next chat turn. Tempo trace captured to `ops/pmg-evidence/studio-end-to-end.log`.
- **Run `ops/pmg-evidence/verify-all.sh`** to generate the final evidence report.
- **PMG demo runbook** + dry run.

### Days 9–10 — Hardening + slack
- Cross-tenant adversarial CI (1000 attempts) — task #16 partial
- Local CI gate — task #12
- Manager decision package + roadmap weeks — this doc + addendum
- UC-8 authored via Studio as the **demo proof** that the substrate generalizes
- Misclassification Detector telemetry attribute (Q6 follow-on)

**Total: ~10 working days (~2 weeks) to demo Studio MVP + axes 1 + 2 + 4 + 5 + 6 visibly progressed.**

---

## What this leaves deferred (named, with weeks)

| Item | Workstream | Weeks |
|---|---|---|
| Hash-chained audit log + RTBF endpoint | P1 | +1 |
| Inbound PII detection + pre-embedding scrub | P1 | +1 |
| Drift detector + per-UC quality rollup + explainability store | P1 | +2 |
| Misclassification Detector full pipeline | Workstream 3.1 | +1 |
| Real CI/CD with IaC + canary + auto-rollback | P0-#7 full | +1 |
| Terraform + Helm + ArgoCD | P1 | +2 |
| EKS + Istio + Lambda + DR | Workstream 4 | +6–10 |
| Chaos suite | P2 | +1 |
| DOC-08: WebSocket / Bridge / webhooks / ChatOps / SDKs | Workstream 4b | +3 |
| DOC-09 ITOM UC-9..UC-14 | If Q3=A: authored via Studio; else hand-coded +6 weeks | varies |
| DOC-13A UC-15..UC-29 + 40+ tools | If Q3=A: authored via Studio; else +12 weeks | varies |
| Customer-facing per-tenant dashboard | P2 | +1 |
| OneOps Graph as load-bearing data product | PMG positioning decision | varies |

---

## Sign-off

Manager: ____________________  Date: ____________

For each question:
- [ ] Q1 — A / B / Override: ________________
- [ ] Q2 — A / B / Override: ________________
- [ ] Q3 — A / B / Override: ________________
- [ ] Q4 — A / B / Override: ________________
- [ ] Q5 — A / B / Override: ________________
- [ ] Q6 — A / B / Override: ________________
- [ ] Q7 — A / B / Override: ________________
- [ ] Q8 — A / B / Override: ________________
- [ ] Q9 — A / B / Override: ________________
- [ ] Q10 — A / B / Override: ________________

Engineering will re-baseline the roadmap within 24 hours of receiving binding answers.
