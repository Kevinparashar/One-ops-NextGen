---
title: OneOps Glossary for PMG
prepared_by: NextGen, for PMG Validation
date: 2026-05-29
---

# OneOps Glossary

## Table of Contents

- [How to use this glossary](#how-to-use-this-glossary)
- [Product terms](#product-terms)
- [Architecture terms](#architecture-terms)
- [AI and governance terms](#ai-and-governance-terms)
- [Operational terms](#operational-terms)
- [Status labels](#status-labels)

---

## How to use this glossary

Every technical term that appears in the PMG documentation is defined here, in product language, with an example or analogy wherever helpful. Acronyms are spelled out on their first appearance in each document, then used freely; this glossary is the always-available reference.

---

## Product terms

**OneOps**
The AI-native assistant for IT Service Management work that this set of documents validates.

**ITSM (IT Service Management)**
The discipline and tooling for handling tickets, incidents, requests, changes, knowledge articles, and related work in an enterprise IT environment.

**ITOM (IT Operations Management)**
The discipline and tooling for monitoring, automating, and operating IT infrastructure. Adjacent to ITSM; OneOps is positioned across both.

**Use Case** (in OneOps context)
A discrete customer-facing capability. *Ticket Summarization* is a use case. *Knowledge Lookup* is a use case. The platform is designed to host many use cases on the same foundation.

**Capability**
Used interchangeably with *use case* in these documents. The product team's preferred term.

**Ticket**
Any record in the customer's ITSM system that OneOps can read: incidents, requests, problems, changes, assets, and configuration items. Each ticket type has its own ID format (for example, *INC0001234* for an incident, *REQ0002007* for a request).

**Knowledge Article**
A document in the customer's knowledge base. In ITSM systems these are commonly identified with a *KB* prefix (for example, *KB0005001*).

**Multi-turn Conversation**
A conversation in which each user turn can refer to previous turns without re-stating context. OneOps remembers what the user is currently focused on and resolves pronouns and follow-ups against it.

**Grounded Answer**
An answer drawn from the customer's own retrieved data, not invented by the AI. OneOps is built to give only grounded answers; it refuses politely when grounding is not available.

---

## Architecture terms

**API Entry**
The public-facing web endpoint a client (web UI, chatbot, partner system) sends questions to. Built on a standard web framework.

**Messaging Layer**
The system that lets the different parts of OneOps talk to each other instantly and keep working even if one part is briefly down. *NATS* is the specific technology used. Think of it as a high-speed conveyor belt between parts of the kitchen.

**NATS**
The name of the messaging-layer technology. Pronounced *nats*. Chosen because it is fast, lightweight, and built for the exact pattern OneOps needs — handing work between many services that can be scaled independently.

**Worker**
A part of OneOps that picks up work from the messaging layer, runs a capability end-to-end, and sends the result back. Workers can be scaled horizontally (run more copies) to handle more traffic.

**Routing**
The logic that decides which capability handles each user question. OneOps uses a chain of focus-aware language-model calls: a **control gate** decides whether the message belongs to the IT/ITSM domain at all (and receives the active record as structured context, so it can distinguish a legitimate follow-up like *"any data on this"* from an unrelated query like *"how to fix bluetooth"* when the same incident is in focus); a **disambiguator** picks between the record-summary capability and the knowledge-lookup capability with the active record as context; and an **embedding-based field matcher** picks the specific record field to read when the query is a field-level question. New phrasings work without code changes because the language model handles them by meaning, and the field matcher compares query embeddings to semantic descriptions of canonical fields. Each routing stage emits its own observability span so the decision path for any request is fully visible.

**Cache**
A fast in-memory store for things that are needed frequently — recent conversation context, common answers, quick-access lookups. *Dragonfly* is the specific technology used. Think of it as the kitchen pantry of frequently-used ingredients.

**Dragonfly**
The name of the cache technology. Compatible with the widely-used Redis interface, with substantially higher throughput.

**Session**
A conversation between one user and OneOps, identified by a session ID. The session memory holds the recent turns and tracks what the user is currently focused on.

**Session Store**
The component that persists session data. Recent turns live in the cache (for speed); the full log lives in the database (for durability and audit).

**Database**
The durable home for everything that must survive a restart. *PostgreSQL* is the specific technology used.

**PostgreSQL**
The name of the database technology. Industry-standard open-source relational database.

**Agent**
In OneOps context, an autonomous AI-driven capability that can be addressed independently and (in the future) collaborate with other agents to complete multi-step work.

**Agent Worker**
A per-capability background worker that subscribes on `oneops.agent.<capability_id>` and runs the capability's tool handlers end-to-end. Every use-case step in OneOps is dispatched to its agent worker over the messaging layer today — this is how Ticket Summarization and Knowledge Lookup execute on every request. The transport is in production; what is not yet active is the **agent-to-agent autonomy** layer, where one agent autonomously decides mid-workflow to hand the next step to a different agent. That activation is gated on the action use case.

---

## AI and governance terms

**AI (Artificial Intelligence)**
The branch of computing that produces systems capable of tasks usually requiring human judgment. In OneOps, AI specifically means the *Large Language Model* that generates text answers from retrieved data.

**LLM (Large Language Model)**
The category of AI model that produces and understands natural-language text. Examples: *GPT-4o*, *Claude*, *Gemini*. OneOps uses a configurable LLM through the LLM Gateway.

**LLM Gateway**
The single checkpoint every AI call in OneOps passes through. Enforces per-customer budgets, scrubs sensitive data, retries transient failures, records cost. The reason an enterprise customer can trust the platform with cost and data-handling questions.

**Embedding**
A numerical fingerprint of a piece of text that captures its meaning. Used for meaning-based search — two texts about the same topic have similar embeddings even if they share no exact words.

**Semantic Search**
Search by meaning, not by exact keyword match. Powered by embeddings. The reason a user can ask *"how do I reset my VPN password?"* and find an article titled *"Network credential recovery procedure."*

**Grounding**
The practice of basing every AI answer in retrieved data rather than letting the model generate freely. OneOps is built to ground every answer and refuse when grounding is not possible.

**Hallucination**
When an AI model generates a plausible-sounding answer that is not based in any source data. OneOps prevents hallucination by architecture, not by hoping — every answer must be tied to retrieved facts.

**PII (Personally Identifiable Information)**
Data that can identify a specific individual — names, emails, phone numbers, identifiers. OneOps automatically scrubs structural PII from any prompt before it leaves the platform.

**Budget Control**
The per-customer daily AI spend limit. Enforced before each call, not after. Prevents runaway costs.

**Cost Tracking**
The per-call record of how much each AI request cost. Recorded in dollars per customer per model. The basis for billing and for proving cost transparency to enterprise buyers.

**Multi-tenant Isolation**
The property that one customer's data, sessions, and costs are strictly separated from another's. Enforced at every layer in OneOps.

---

## Operational terms

**OTEL (OpenTelemetry)**
The vendor-neutral standard for emitting traces, metrics, and logs from a software system. OneOps emits OTEL data for every step of every request.

**Trace**
The record of one request flowing through the system, with timing and outcome for each step. The basis for answering *"why was this slow?"* with evidence.

**Metric**
A numerical value sampled over time — error count, latency at the 99th percentile, AI calls per minute. The basis for dashboards and alerts.

**Tempo**
The technology OneOps uses to store traces. Read by Grafana to display them.

**Prometheus**
The technology OneOps uses to store metrics. Read by Grafana to display them.

**Grafana**
The dashboard tool OneOps uses to visualize traces and metrics.

**Circuit Breaker**
A safety mechanism in the messaging layer. If a downstream component is unhealthy, traffic to it is paused until it recovers, instead of allowing failures to cascade across the platform.

**Single Egress**
The principle that every AI call exits the platform through exactly one place (the LLM Gateway), so governance rules can be enforced in one place. No exceptions.

**Idempotency**
The property that the same request issued twice (for example, from a retrying client) produces the same outcome, not duplicated work. The cache layer carries deduplication markers to enforce this for recent identical requests.

**Connection Pool**
A managed reusable pool of open database connections, sized to handle expected concurrency without exhausting the database. OneOps has a properly bounded pool tuned for production load.

---

## Status labels

**Done**
Built, tested, and works end to end today. Demo-ready.

**In progress**
Built and partially working. Some paths work; others are being completed.

**Partial**
A subset of the capability works. The rest is designed but not built.

**Mocked**
Interface exists but returns simulated data, not real results.

**Planned**
Designed and architecturally accommodated, but no working code yet.

**Built, not yet active**
Code exists and is tested in isolation, but is not wired into the live request path. The foundation is in place; the capability is not active.

---
