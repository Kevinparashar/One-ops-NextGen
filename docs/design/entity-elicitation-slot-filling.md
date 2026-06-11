# Entity elicitation — slot-filling for missing required entity references

**Status:** in progress · **Flag:** `ONEOPS_ENTITY_ELICITATION_ENABLED` (default **OFF**)
**Owner:** executor · **Rules:** §2.1 (no phrase catalogs), §2.2 (LLM decides),
§2.4 (tenant isolation), §2.6 (observability), §2.7 (no silent failures),
§2.9 (testing), §2.12 (drive).

## Problem

A query that requires a record but names none — `summarize my ticket`,
`similar tickets`, `tickets like VPN problems` — routes to a UC whose tool
declares a **required** entity-shaped parameter (`ticket_id`). The router binds
nothing (no id in the message, no focus), the handler runs anyway and **fails**:
UC-1 returns `invalid_request`, UC-2 raises `ValueError` → the executor renders
the generic *"I wasn't able to complete that request."*

That is a silent-ish dead-end. The production pattern (slot-filling /
parameter elicitation, confirmed across dialogue-systems literature and
LangGraph's own guidance — *"put `interrupt()` on validation failures instead of
allowing tool calls to fail"*) is: **detect the missing required slot before
dispatch and ask the user**, then resolve their answer and proceed.

## Key requirement — replies are contextual, not just ids

The user may answer **"last ticket" / "previous one" / "my recent tickets" /
"the VPN one"**, not a literal id. We must understand these **against the user's
real records**, never via a keyword/regex catalog (§2.1). Resolution is
LLM-led against fetched candidates (§2.2).

## Design

### Gate (single choke point)
`step_runner.run()` — after tool selection + argument assembly, before
`_invoke_handler`. Flag-gated; OFF ⇒ byte-for-byte today's path.

Trigger (data-driven): the selected tool has a `required`, **entity-shaped**
param (`_ENTITY_SHAPED_PARAMS`) that is missing/empty in `arguments` and not
satisfiable from `context` focus. Only the **first** such param per step is
elicited (one interrupt per turn, matching UC-8). `service_id` is never
elicited on its own — it is derived when the entity resolves.

### Ask
`interrupt_for_clarification(question, hints)` — **open text** so contextual
replies are possible. `hints` = a couple of the user's recent ids as example
chips. Returns `{"answer": <text>}` on resume via `Command(resume=...)`.

### Resolve the reply (3 layers, LLM-led, no phrase catalog)
1. **Literal id** → `EntityIdNormalizer.extract(answer)` (data-driven
   `id_prefix→service` from `service-schema.json`). Deterministic, no LLM.
2. **Relative / descriptive** → fetch the user's recent records
   (`list_recent_for_user`, RBAC + tenant scoped) + session focus; the **LLM
   picks** the concrete ticket the phrase refers to (structured output:
   `{ticket_id, service_id}` or `none`).
3. **Unresolved** → one re-ask, else fall through to the existing graceful
   handler outcome (never silent — §2.7).

On success, bind `arguments[<entity_param>] = id` **and**
`arguments[<service_param>] = service` (only if the tool declares a service
param), then proceed to the handler.

### Observability (§2.6)
Span `executor.step.entity_elicitation` + counters
`ai.elicitation.raised`, `ai.elicitation.resolved{method=literal|llm|focus}`,
`ai.elicitation.unresolved`.

## Non-breaking guarantee
Flag OFF ⇒ the gate is a no-op; existing 1731-test suite unchanged. UC-8's
interrupt machinery, the gateway, the normalizer, and the ticket store are all
reused — no new transport, no new interrupt kind.

## Build steps (DONE only when build + smoke + unit + integration + devils-play + edge)

- [x] **S0** Design doc (this file) + flag plumbing (`_parse_flag`, default OFF).
- [x] **S1** `list_recent_for_user()` on the ticket store (in-memory + Postgres, data-driven per-service owner/recency columns, user+tenant scoped). 5 unit tests. Verified live against Postgres.
- [x] **S2** `entity_elicitation.py`: `resolve_reply()` (literal → LLM-against-candidates) + principle-based `CandidatePicker` (no phrase catalog). 9 unit tests.
- [x] **S3** `elicit_entity()` orchestrator: fetch candidates → ask (`interrupt`) → resolve → bind. 6 hermetic tests.
- [x] **S4** Gate wired into `step_runner.run()` behind the flag + gateway injected at startup. 7 tests (flag OFF no-op, flag ON bind, interrupt propagates, focus-skip, already-bound-skip).
- [x] **S5** Resume path: `Command(resume)` re-enters and binds — verified LIVE (interrupt → reply → executed).
- [x] **S6** Edge cases: no recent records (degrade to id-ask), unresolved reply, out-of-set id rejected, store-failure degrade — unit + live covered.
- [x] **S7** Live E2E (flag ON): the previously-failing no-ID queries now ASK and resolve — literal ids AND contextual ("the VPN one" → INC0001019, "my most recent one" → newest, "the SSO problem" → INC0001030). Regression flag-OFF: 1819 passed, only the 8 documented pre-existing UC-8 failures.
- [ ] **S8** Flip flag default ON after sign-off (separate, explicit). **PENDING.**

## Validated behaviour (live, flag ON)
Resolution layers all confirmed against real records: **recency** ("my most
recent one" → newest), **topic** ("the VPN one" → the VPN incident; "the SSO
problem" → the SSO incident), **literal** (id → id). User scope comes from the
`x-user-id` principal (header), not request text — correct production behaviour.

## Known follow-up (separate from this fix)
A no-entity "summarize my ticket" routes to `uc02_similar_tickets` (the
disambiguator's choice for entity-less phrasing), so after resolving it shows
the ticket + similar rather than a pure UC-1 summary. The elicitation resolves
the record correctly either way; the UC-1-vs-UC-2 routing is a disambiguator
concern, not part of slot-filling. Track separately.
