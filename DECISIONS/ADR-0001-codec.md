# ADR-0001 — Wire/disk codec: Protocol Buffers

**Status:** Accepted · 2026-05-20
**Decision owners:** AI Platform Architect

## Context

Every inter-service message (NATS payloads), every conversation event written
to the append-only log, and every registry record exchanged across a boundary
needs one serialization contract. The system must, over a 5-year horizon:

- Evolve schemas without breaking running consumers (N and N−1 must coexist).
- Stay language-agnostic — a tool runner may one day be written in Go/Rust.
- Make accidental breaking changes hard, not merely discouraged.

Today the repo serializes with ad-hoc JSON in-process. No enforced schema, no
evolution rules — keys can drift silently. That does not survive 1000 UCs and
multiple teams.

## Options

| Option | Schema evolution | Codegen | Cross-language | Risk |
|---|---|---|---|---|
| **Protobuf** | Field numbers are permanent; adding optional fields is safe by rule | Yes (`.proto` → clients) | First-class | Build step; `.proto` discipline required |
| msgpack | None enforced — it's JSON-shaped binary; key drift still silent | No | Via libraries | Cheap now, expensive at year 3 when a consumer breaks silently |
| Keep JSON | None | No | Universal | The status quo that does not scale |

## Decision

**Protocol Buffers (proto3).** A versioned base envelope plus per-message
schemas under `proto/oneops/v1/`. Python clients generated at build time;
the build fails if generated code is stale.

The envelope carries: `schema_version`, `tenant_id`, `trace_context`
(W3C traceparent), `idempotency_key`, and a typed payload.

## Why protobuf over msgpack

The brief asks for *enforced* schema evolution. msgpack gives a compact binary
encoding but **no evolution discipline** — it cannot stop a consumer from
breaking when a producer renames a field. Protobuf's permanent field numbers
make the N/N−1 compatibility requirement a property of the format, not a
hope. At 1000 UCs and multiple owning teams, "the format enforces it" is the
only thing that holds.

## Consequences

- A `protoc` build step enters CI; generated code is checked in or built
  reproducibly.
- A schema-registry record (ADR scope: registry layer) tracks every message
  version and its deprecation window.
- Removing or renumbering a field is a **major version bump** — caught in review.
- In-process Python may still pass dicts; protobuf is enforced **on the wire
  and on disk** only.

## Exit plan

If a future need (e.g. schema-less telemetry blobs) wants msgpack locally,
that is an additive choice for one message type — it does not unseat protobuf
as the contract format. Leaving protobuf entirely would mean regenerating all
`.proto` definitions in the target format; the `.proto` files are the
language-neutral source of truth, so the migration is mechanical.
