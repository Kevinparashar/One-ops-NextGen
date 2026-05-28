# ADR-0005 — NATS topology: 3-node cluster + JetStream for durable work

**Status:** Accepted · 2026-05-20
**Decision owners:** AI Platform Architect

## Context

NATS is the messaging fabric between services (Ingress, AuthZ, Router,
Session, Executor, Tool Runners). The system needs:

- Low-latency request/reply for the synchronous routing path.
- Durable, replayable streams for long-running and side-effecting work
  (action use cases, multi-wave executions that must survive a restart).
- Multi-tenant subject isolation.
- No single point of failure over a 5-year horizon.

## Options

| Concern | Core NATS | JetStream |
|---|---|---|
| Request/reply (routing path) | Ideal — minimal latency | Overkill |
| Durable / replayable work | Not durable — at-most-once | Designed for it — at-least-once, persisted |
| Ordering & dedup | No | Yes |

Topology: single node (simple, no HA) vs. 3-node cluster (HA, no SPOF).

## Decision

**3-node NATS cluster in production; single node in local dev.**
**Both transports, by purpose:**

- **Core NATS request/reply** for the synchronous routing path
  (Ingress → Router → Executor). Latency-critical, transient — durability
  would only add cost.
- **JetStream durable streams** for long-running / side-effecting work:
  action use cases, multi-wave executions, anything that must survive a
  process restart or be replayed.

**Subject hierarchy** embeds the tenant at every tenant boundary:
`oneops.<tenant_id>.uc.<agent_id>.<op>`. Queue groups load-balance replicas
of the same consumer.

**Trace context** (W3C `traceparent`) travels in NATS headers on every
message, core and JetStream alike — so a trace is unbroken across hops
(ARCHITECTURE.md §7).

## Why this split

Using JetStream for everything would put durability overhead on the
latency-critical routing path that does not need it; using core NATS for
everything would lose the at-least-once durability that action workflows
require to be safe under re-delivery. Matching transport to message intent is
the correct discipline. A 3-node cluster removes the single point of failure
that a 5-year production system cannot carry.

## Consequences

- JetStream consumers must be **idempotent** — at-least-once means re-delivery
  happens. Idempotency keys in Dragonfly (minted at ingress) guard every
  side effect (ARCHITECTURE.md §8).
- Stream retention and replica count are configured per stream; action-work
  streams replicate across all 3 nodes.
- Backpressure on a full stream surfaces upstream as a 429 at the API
  Gateway — never a silent downstream collapse.
- Subjects are tenant-scoped by construction, so a consumer for tenant T1
  cannot receive tenant T2's messages.

## Exit plan

NATS is reached only through a thin client adapter (`adapters/nats_client.py`).
Replacing the bus (e.g. with Kafka for the durable side) means re-implementing
that adapter against the new broker; service code, which speaks only the
adapter interface, is unaffected. The core/durable split documented here would
map onto the replacement's equivalent primitives.
