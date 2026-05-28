---
title: OneOps Product Validation Overview for PMG
prepared_by: NextGen, for PMG Validation
date: 2026-05-28
---

# OneOps Product Validation Overview

## Table of Contents

- [1. Executive Summary](#1-executive-summary)
- [2. Current Build Status](#2-current-build-status)
- [3. Product Vision Alignment](#3-product-vision-alignment)

---

## 1. Executive Summary

**OneOps** is an AI-native assistant for IT Service Management (ITSM) and IT Operations Management (ITOM) work. A user types a natural-language question — about a ticket, an asset, or a knowledge article — and OneOps responds with a precise, grounded answer drawn from the customer's own ITSM data. No clicking through screens, no remembering field names, no copy-pasting between tabs.

Today, two customer-facing capabilities work end to end:

- **Ticket Summarization** — *"Summarize INC0001234"* or *"what is the priority of that ticket?"* The system fetches the ticket, reads its history, and replies with a clean summary or the specific field the user asked for.
- **Knowledge Lookup** — *"How do I reset my VPN password?"* or *"show me KB0005001."* The system searches the customer's knowledge base using both keywords and meaning, and returns the most relevant articles.

A third capability — **taking action on a ticket** (close, assign, update) — is designed and architecturally accommodated, but the working code for it is not yet built.

The underlying platform is built for scale from day one. Every AI request flows through a single governance layer that controls cost, blocks sensitive data leakage, and tracks every dollar spent. Every part of the system reports its health and timing into a unified observability stack. Sessions, recent context, and frequently-needed answers are cached for speed. The components that connect services together are in place and proven for the two live use cases; the same connectors are ready to carry future use cases without re-architecture.

**Why PMG validation now.** The two live capabilities are technically solid, but before we present them to customers we need PMG to confirm: *Are these the right two? Is the language right? Is the demo story credible? Are the gaps we know about the right gaps to be honest about?*

> This document is the entry point. Read it end to end first; everything else expands on a section of this overview.

---

## 2. Current Build Status

The table below is the single source of truth for what is built. Every row was confirmed against running code, not against READMEs or design notes.

| Area | Status | Evidence from Codebase | PMG Relevance |
| --- | --- | --- | --- |
| Ticket Summarization (incidents, requests, problems, changes, assets, configuration items) | **Done** | Use-case module with full handler, cache layer, and passing stress tests | Core product capability |
| Knowledge Lookup (semantic + keyword search of knowledge base) | **Done** | Use-case module with semantic and keyword search, passing tests | Core product capability |
| Conversational fallback (greetings, out-of-scope handling) | **Done** | Boundary responder built in; polite redirect to ITSM scope | Polish — keeps the demo feeling natural |
| Multi-turn conversation (follow-ups like *"who is it assigned to?"*) | **Done** | Session memory + focus tracking working for live use cases | Demo differentiator |
| Action on a ticket (close, assign, update) | **Planned** | Framework in place; no working handler code; awaiting design completion | Roadmap conversation |
| Agent-to-agent workflows (one AI agent autonomously handing work to another over the messaging layer) | **Built, not yet active** | The transport is in production today — every use-case step already runs over NATS to a per-UC agent worker. What's pending is the autonomy layer: an agent deciding mid-workflow to dispatch the next step to a different agent. Foundation is fully wired and observable | Roadmap conversation — transport already live |
| User profile memory (preferences, history per user) | **Planned** | Interface defined; no working storage yet | Future capability |
| Context-aware semantic routing (deciding which capability handles each query) | **Done** | Focus-aware language-model control gate (off-domain detection with the active record as context) + language-model disambiguator (UC-1 vs UC-3 with focus + candidate descriptions). Multi-turn focus is a structured state channel; chained linked-record references resolve correctly. No keyword phrase lists in the routing path | Demo differentiator on natural-language follow-ups |
| Cost governance for every AI call | **Done** | Single gateway enforces per-customer budgets and records every dollar | Critical for enterprise sales |
| Sensitive-data scrubbing on every AI call | **Done** | Automatic redaction before any text leaves the platform | Critical for enterprise sales |
| End-to-end observability (traces, metrics, dashboards) | **Done** | Every request emits structured traces; dashboards provisioned | Operability story |
| Multi-tenant data isolation | **Done** | Per-tenant context carried through every layer; role-based access enforced at the data layer | Critical for enterprise sales |

> **Read this table carefully.** Anything not in this table is either planned, internal infrastructure, or out of scope for PMG validation. If something a customer has asked for is missing from this table, that is a finding — please flag it.

---

## 3. Product Vision Alignment

OneOps is being built to replace the slow, screen-driven, copy-paste experience of working inside an ITSM tool with a single conversational surface that *knows the customer's data, respects their permissions, and gets faster the more it is used*.

The architecture supports that vision in three concrete ways.

**One conversation, many capabilities.** A user does not pick a tool. They ask a question. The system decides whether the question is about a ticket, a knowledge article, an asset, or none of the above, and routes it appropriately. Two capabilities are live today. The same routing layer is ready to accept additional capabilities as they ship — there is no re-platforming needed to add a third or thirtieth capability.

**Trustworthy by construction.** Every AI request goes through one governance checkpoint that enforces budget, scrubs sensitive data, and records cost. This is not a future feature — it is how the system already works. It means we can answer enterprise procurement questions about cost control and data handling with evidence, not promises.

**Operability as a first-class feature.** Every step of every request is traced and timed. When a customer says *"this query was slow yesterday at 3pm,"* the platform can show exactly which step took how long. This is the difference between a demo and a product.

**What the vision still needs.** Taking action on tickets (not just reading them) is the next major capability. Once that ships, OneOps moves from *assistant* to *operator*. The foundation for agent-to-agent workflows — required for multi-step actions like *"open a change request for this incident and notify the on-call"* — is built but not yet activated.

---
