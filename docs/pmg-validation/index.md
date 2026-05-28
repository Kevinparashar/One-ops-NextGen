---
title: OneOps PMG Validation — Reading Index
prepared_by: NextGen, for PMG Validation
date: 2026-05-27
---

# OneOps PMG Validation — Reading Index

## Table of Contents

- [Purpose](#purpose)
- [Who should read this](#who-should-read-this)
- [Reading order](#reading-order)
- [How to read a status label](#how-to-read-a-status-label)
- [How to flag feedback](#how-to-flag-feedback)

---

## Purpose

This set of documents exists so the Product Management (PMG) team can validate **what NextGen has built so far for OneOps**, understand **how it works in plain product terms**, and decide **what is ready to take to customers**.

The goal is not approval of an engineering plan — it is a check that what we have built lines up with the customer problem PMG wants OneOps to solve.

---

## Who should read this

- **Product Managers** validating the OneOps build against customer needs.
- **PMG leadership** preparing demos, positioning, and roadmap conversations.
- **GTM partners** who need to understand what is genuinely shippable today.

These documents are written for a non-engineering audience. Every technical term is explained in plain language with an analogy. Acronyms are defined on first use and again in the glossary.

---

## Reading order

Read the documents in this order. Each one builds on the previous.

| # | Document | What it gives you | Time to read |
| --- | --- | --- | --- |
| 1 | [Product Validation Overview](./01-product-validation-overview.md) | The big picture — what OneOps is, what is built, what PMG must validate | 10 min |
| 2 | [Architecture Explanation](./02-architecture-explanation.md) | How OneOps works under the hood, in product language | 20 min |
| 3 | [Use Case Deep-Dives](./03-use-case-deep-dives.md) | Step-by-step walkthroughs of each customer scenario | 15 min |
| 4 | [OneOps Status](./04-oneops-status.md) | Honest scorecard — capability, platform foundation, memory layers, security dimensions: what is Done, In Progress, Partial, Planned | 5 min |
| 5 | [Glossary](./05-glossary.md) | Definitions of every term used | reference |
| 6 | [Detailed Architecture — Narrated Walkthrough](./06-architecture-detailed.md) | Deep-dive architecture with block diagrams, design ideology, and speaker notes — built for live PMG screen-share | 30 min |
| 7 | [OneOps Studio — User-Authored Agents](./07-oneops-studio.md) | Architecture for the no-code agent authoring surface on top of the platform: text-to-agent compilation, tool catalog, RBAC, multi-tenant isolation | 25 min |

If you only have ten minutes, read **Document 1** end to end and then skim **Document 4**.

If you have an hour, read Documents **1, 2, 4** in full and skim **6**.

If the conversation is about how customers will build their own agents on the platform, add Document **7**.

---

## How to read a status label

Every capability in these documents carries one of five status labels. They mean exactly what they say.

| Label | Meaning |
| --- | --- |
| **Done** | Code is built, tested, and works end to end today. Demo-ready. |
| **In progress** | Code is built and partially working. Some paths work; others are being completed. Not yet demo-ready for the unfinished paths. |
| **Partial** | A subset of the capability works. The rest is designed but not yet built. |
| **Mocked** | The interface exists but returns simulated data, not real results. Do not show to customers as a working feature. |
| **Planned** | Designed and architecturally accommodated, but no working code yet. |

> If a capability does not carry a status label, treat it as **not validated yet** and ask.

---

## How to flag feedback

When you have questions, concerns, or want a capability validated more deeply, mark them against the **Key Questions for PMG** section in Document 1, or add them to the PMG validation tracker.

Every claim in these documents has been checked against actual code. Where the code and our earlier README files disagreed, the code wins — and the inconsistencies are being cleaned up separately so the public-facing story stays consistent.
