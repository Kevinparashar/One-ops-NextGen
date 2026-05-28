---
title: OneOps Status for PMG
prepared_by: NextGen, for PMG Validation
date: 2026-05-29
---

# OneOps Status

## Table of Contents

- [How to use this document](#how-to-use-this-document)
- [1. Capability status — the single source of truth](#1-capability-status--the-single-source-of-truth)
- [2. Platform foundation status](#2-platform-foundation-status)
- [3. Memory layer status](#3-memory-layer-status)
- [4. Security, governance, and PII status](#4-security-governance-and-pii-status)

> **How to use this document.** This is the honesty sheet. Every status label was verified against running code. If you read a positive claim anywhere else in the PMG documentation and want to double-check it, this document is the audit trail.

---

## 1. Capability status — the single source of truth

| # | Capability | Status | Customer-visible? | Notes |
| --- | --- | --- | --- | --- |
| 1 | Ticket Summarization (incidents, requests, problems, changes, assets, configuration items) | **Done** | Yes | Stress-tested at high turn counts; multi-turn follow-ups work |
| 2 | Knowledge Lookup (meaning-based and keyword search) | **Done** | Yes | Both search modes live; honest *"no matches"* path validated |
| 3 | Conversational Fallback (greetings, out-of-scope decline) | **Done** | Yes | Wording is in place; PMG should validate tone |
| 4 | Multi-turn Conversation (follow-ups, entity switches, hops, comparisons) | **Done** | Yes | Live across all the above; staleness gating in place |
| 5 | Action on a Ticket (close, assign, update, create) | **Planned** | No | Designed; no working code; foundation in place |
| 6 | Multi-step Agent Workflows | **Planned** | No | Messaging foundation built; orchestration not built |
| 7 | User Profile Memory (cross-session preferences) | **Planned** | No | Interface defined; storage not built |
| 8 | Multi-model Routing (cheap vs capable model per question) | **Planned** | No | Gateway supports it architecturally; not configured today |

---

## 2. Platform foundation status

| # | Foundation Element | Status | Notes |
| --- | --- | --- | --- |
| 1 | API Entry | **Done** | Production-shaped; live for both demo paths |
| 2 | Messaging Layer (core request/reply) | **Done** | Live for every user request; circuit breaker active |
| 3 | Messaging Layer (durable, on-disk persistence variant) | **Built, not active** | Infrastructure ready; code does not use it yet |
| 4 | LLM Gateway (budget, scrubbing, retry, cost tracking) | **Done** | Single egress enforced |
| 5 | Cache | **Done** | Five cache layers in production use; fails open if cache is down |
| 6 | Session Memory | **Done** | Recent context in cache; full log in database |
| 7 | Database | **Done** | Schema migrated; pooling in place |
| 8 | Observability (traces, metrics, dashboards) | **Done** | Every request traced; routing pipeline traced at stage granularity (focus-state update / focus-aware control gate / decompose / rewrite / retrieve / filter / language-model disambiguator); model reasoning and selection exposed as span attributes; sensitive content never captured |
| 9 | Agent Worker (per-UC NATS subscriber) | **Done** | Every UC step dispatch goes over NATS (`oneops.agent.<uc_id>`) to the per-UC agent worker. Inter-component messaging is fully active today |
| 9a | Agent-to-agent autonomy (one agent dispatching work to another) | **Built, not active** | The transport (NATS + agent worker subscribers + envelope schema) is in production use; what's pending is the orchestration logic that lets one agent autonomously hand off mid-workflow to another. Activation depends on the action use case |
| 10 | Context-aware semantic routing | **Done** | Focus-aware language-model control gate (off-domain detection with the active record as context, refuses domain-adjacent off-topic queries like "fix bluetooth" mid-session) + language-model disambiguator (UC-1 vs UC-3 with focus + candidate descriptions). Embedding-based field matcher for record-attribute extraction. Multi-turn focus is a structured LangGraph state channel; chained linked-record reads supported. Each routing stage is its own OTel span — decisions are investigable per-request in Grafana. No keyword phrase lists in the routing path |
| 11 | Multi-tenant isolation | **Done** | Per-tenant context through every layer; role-based access enforced at data layer |
| 12 | Cost tracking per customer | **Done** | Recorded on every AI call; visible in telemetry |
| 13 | Sensitive-data scrubbing | **Done** | Automatic on every AI call |

---

## 3. Memory layer status

Memory in an AI platform is not one thing — it is seven distinct layers with different lifetimes, costs, and risk profiles. The table below is the honesty sheet for each layer.

| # | Memory Layer | Status | What It Remembers | Where It Lives | Notes for PMG |
| --- | --- | --- | --- | --- | --- |
| 1 | Working memory (per-request) | **Done** | This turn's intermediate state | In-process via LangGraph state channels | Production-quality; nothing missing |
| 2 | Short-term conversation memory | **Done** | Recent turns + current focus entity | Dragonfly (hot) + PostgreSQL (durable log) | Token-budget trimming and summarization rollover are the polish items |
| 3 | Session lifecycle memory | **Done** | Which sessions are alive, sliding TTL | Dragonfly | Per-tenant hard caps and graceful resumption are the polish items |
| 4 | Long-term user memory | **Planned** | Per-user preferences, working hours, recurring asks | Postgres + cache (designed) | Interface defined; storage and assembler not built |
| 5 | Semantic memory (RAG) | **Done** | KB articles encoded as vectors | PostgreSQL + pgvector | Production-grade; hybrid retrieval + empirical relevance gate |
| 6 | Procedural memory (agent memory) | **Built, not active** | Per-agent success / failure / tool-call patterns | OTel spans today; rollup table not built | Raw data captured; consumption layer missing — needed for Studio agent self-improvement |
| 7 | Episodic memory (cross-session recall) | **Planned** | *"Last week the user asked about X"* | Postgres + vector index (designed) | Not built; most compliance-sensitive layer |

**The scorecard in one line.** Three layers production-quality. Two need polish. One has raw data but no consumption layer. Two not yet built. Every existing layer is structurally tenant-scoped — `tenant_id` is the first key segment of every store access, cache key, and metric label.

---

## 4. Security, governance, and PII status

Security in a multi-tenant SaaS AI platform is seven dimensions, not one. The table below is the honesty sheet for each dimension.

| # | Security Dimension | Status | What It Protects Against | Notes for PMG |
| --- | --- | --- | --- | --- |
| 1 | Identity and authentication | **In progress** | Unauthorized humans or clients | Today we trust the customer's identity layer; JWT signature verification at the front door is the procurement-blocker work |
| 2 | Authorization and RBAC | **In progress** | Authorized users acting outside their role | Tenant + role propagated through every layer; the materialised `(role × tool) → allow/deny` matrix and the two-gate enforcement (author-time + runtime) are the next step — shared with Studio (Doc 7) |
| 3 | Tenant isolation | **Done** | Tenant A seeing Tenant B's data | Structurally enforced at every layer: SQL first predicate, cache namespace, NATS header, OTel label. Per-tenant one-shot delete and adversarial cross-tenant CI suite are the build-out items |
| 4 | PII and sensitive-data protection | **Partial** | Sensitive data leaking to LLM provider, logs, traces, vectors, caches | Outbound scrubbing at the LLM Gateway and OTel scrubbing at the source are live. Inbound classification, pre-embedding scrub, cache scrub, and per-tenant policy tier (EU vs US vs regulated) are not yet built |
| 5 | Prompt safety (`updated_policies`) | **Done** | Prompt injection, jailbreak, hallucination, off-domain abuse, PII echo, system-prompt leak | 41 reusable safety blocks across 8 profiles; every LLM call composes through `compose(Profile.X, ...)`. This is our strongest asset. Per-tenant overlays, output-side safety scanner, and an adversarial CI probe suite are the next moves |
| 6 | Audit and compliance | **Partial** | Lack of evidence when a regulator or security team asks | Append-only conversation log + OTel traces + per-tenant cost are operational-grade. Hash-chained immutable audit log, per-tenant retention policy, per-tenant export tooling, and a right-to-be-forgotten endpoint are the compliance-grade gap |
| 7 | Operational security | **Planned** | Stolen secrets, supply-chain compromise, infra breach | Dev-grade today. Secret manager integration, image signing + SBOM, dependency and code scanning in CI, egress allow-list firewall, backups + DR, annual pentest, and incident-response runbook are the SaaS-prep workstream |

**The scorecard in one line.** Two dimensions strong (tenant isolation, prompt safety — the latter thanks to `updated_policies` being treated as code from day one). Two partial (PII outbound-only; audit operational-only). Two in progress (identity, authorization — overlapping with Studio). One dev-grade (operational security).

**On `updated_policies` specifically.** This file (consumed by `src/oneops/policy/`) is the single most important file in our safety posture. Every LLM call in the platform composes from it — there is no path from a capability to a model that bypasses it. Treated as code (PR-reviewed, version-controlled, never hot-edited), it gives us *"one auditable source of truth"* as the answer to procurement's prompt-safety questions. The work ahead is adding per-tenant overlays, an output-side scanner, and an adversarial CI suite that proves every edit preserves safety.

---

