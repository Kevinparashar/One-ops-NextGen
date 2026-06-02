# CONVENTIONS.md — coding conventions for `src/oneops/`

These are the conventions the existing platform code is built to. They distil what is already enforced in `src/oneops/` and what `docs/COMPONENT_SPEC.md` (C1–C24) and `docs/PROJECT-BRIEFING.md §2` mandate. A new module that violates them will be rejected at review.

> **This document is descriptive of what the codebase already does**, not aspirational. Use the load-bearing files cited under each rule as canonical examples.

---

## 1. Module structure

- One module per concern under `src/oneops/`. Modules are top-level by responsibility, not by use case (use cases live under `src/oneops/use_cases/uc<NN>_<name>/`).
- Cross-UC shared code goes in `uc_common/` or `_shared/`. UC-specific code never leaks into a platform module.
- A module's `__init__.py` re-exports the public surface; internals stay private. See `src/oneops/observability/__init__.py`.

## 2. Naming

- **Classes:** `PascalCase`, domain-precise nouns. `LlmGateway`, `TenantContext`, `RouteResult`, `EntityDetailsResult`.
- **Files / packages:** `snake_case`. `gateway.py`, `span_helpers.py`, `tenancy/__init__.py`.
- **Functions:** `snake_case`, verb-first. `compose_profile`, `record_cache_get`.
- **Constants:** `UPPER_SNAKE`. `DEFAULT_TIMEOUT_MS`.
- **Pydantic models:** `<Concept>Context`, `<Concept>Result`, `<Concept>Request` — never bare `Concept`.
- **Async:** prefix is implicit (don't suffix `_async`); use `async def` where the function performs I/O.

## 3. Type hints

- Public functions / classes carry full signatures. No `Any` without a docstring justification.
- Public APIs accept and return Pydantic models or `dataclass(frozen=True)`, not loose dicts (`COMPONENT_SPEC C7`).
- Use `from __future__ import annotations` consistently at module top.
- Generic containers use the built-in syntax (`list[X]`, `dict[str, Y]`), not `typing.List` / `typing.Dict`.

## 4. Docstrings

- Module docstring: explain **why this module exists**, cite the architecture doc / ADR / `COMPONENT_SPEC` clause that mandates it. See `src/oneops/llm/gateway.py`, `src/oneops/tenancy/...`, `src/oneops/policy/composer.py` for canonical examples.
- Class docstring: design rationale, invariants, lifecycle.
- Function docstring: only if behaviour is non-obvious from the signature. Don't restate parameters that are already typed.
- Inline comments: only the non-obvious **why**. No comments explaining what the next line does (rule `PROJECT-BRIEFING §2.10`).

## 5. Pydantic at boundaries (`COMPONENT_SPEC C7`)

- Every cross-component input / output is a Pydantic model.
- Validators run at construction; downstream code never re-validates.
- Frozen by default (`model_config = ConfigDict(frozen=True)`) for context / result types.
- Round-trip safe (`model_dump` / `model_validate`) so caching + audit work without custom codecs.

Canonical example: `src/oneops/tenancy/...` — `TenantContext` is frozen Pydantic with locale / tier validators at construction.

## 6. Errors — fail loud (`PROJECT-BRIEFING §2.7`)

- Custom exception hierarchy under `src/oneops/errors/`. Every typed error inherits a base that carries the structured context.
- No bare `except:`. No `except Exception: pass`. No silent fallback that returns a sentinel success.
- Failures that downstream callers must distinguish (timeout vs upstream 5xx vs validation) are separate exception classes.
- Telemetry emits NEVER raise into business code — `try/except` wrap inside helpers (`observability/span_helpers.py`), not at call sites.

## 7. Observability (`docs/observability/architecture_map.md`)

- Every meaningful operation emits an OTel span. Span names follow `<module>.<operation>` (`router.route`, `llm.call`, `cache.get`).
- Required attributes on every span: `tenant_id`, `request_id`.
- Add `agent_id`, `agent_version`, `confidence_score`, `autonomy_level` where applicable.
- Never log or span-attribute raw user text. Use `observability/safe_attrs.py` (hash + length) unless `OTEL_CAPTURE_TEXT=true` (dev-tenant only).
- Use the helpers in `observability/` (`span`, `llm_span`, `record_cache_get` / `_set`, `increment`, `histogram`) — never call `tracer.start_as_current_span` directly.

## 8. LLM calls

- The only path to a provider is `src/oneops/llm/gateway.py::LlmGateway.call` (and `.embed` for embeddings). CI gate `test_no_direct_provider` enforces this — do not add a second egress (rule `§2.5`).
- The prompt is composed by `src/oneops/policy/composer.py::compose(Profile.X, ...)`. Never hand-craft a system prompt (rule `§2.3`).
- LLM outputs that drive downstream behaviour are schema-validated Pydantic models, never free-form parsed strings (`COMPONENT_SPEC C8`).

## 9. Tenant isolation (`PROJECT-BRIEFING §2.4`, `COMPONENT_SPEC C13`)

- Every repository method takes `tenant_id` as a required parameter. There is no overload without it.
- Every Dragonfly key carries `tenant_id` as a prefix.
- Every NATS subject embeds `tenant_id` where a tenant boundary exists.
- `tenant_id` comes from the request envelope (validated upstream), never from message body or user text.
- A missing `tenant_id` is a typed error at the boundary, not a full-table scan.

## 10. Routing & agent definition (`ARCHITECTURE.md §3`, `§4`)

- An agent is a registry record under `registries/agent-catalog-registry.json`. Adding / changing / retiring an agent is a registry edit, not a code change (rule "agents are data").
- Activation conditions are structured predicates over typed signals (intent, role, ABAC, entities present, focus). Never raw user-text substring checks (rule `§2.1`, `COMPONENT_SPEC C5`).
- Tools live in `registries/tool-registry.json` and are referenced by id from agents. One tool can serve many agents.
- Per-agent tool allowlist is in `registries/agent-tool-mapping.json` keyed by `(agent_id, service_id)`.

## 11. Tests (`PROJECT-BRIEFING §3.5`)

- Layout mirrors `src/oneops/`. A change in `src/oneops/router/router.py` lands tests in `tests/unit/router/test_router.py`.
- Every change ships unit tests; cross-process changes ship an integration test under `tests/integration/`.
- In-memory backends are the default; live-infra tests are env-gated (`PYTEST_LIVE=1` or per-suite skip markers).
- Smoke (`scripts/smoke_routing.py`) and devil's-play (`scripts/devils_play.py`) must remain green after every change — these are the gates, not "nice to haves".

## 12. File hygiene (`PROJECT-BRIEFING §2.10`)

- Edit existing files. Do not create a new module for a one-shot helper.
- Three similar lines is better than a premature abstraction.
- A new file requires a justification visible in the PR (which module's surface does it extend; which `COMPONENT_SPEC` clause justifies it).
- No dead code, no commented-out blocks — delete or move to `docs/findings/` if the trail matters.

## 13. Git hygiene

- No `--no-verify` on commit.
- No `git push --force` (and never to `main`).
- No `--amend` on commits that have left your machine.
- Pre-commit hooks (ruff, mypy, secrets scan) are gates, not suggestions.
- A PR description references the ADR / rule / `COMPONENT_SPEC` clause it implements or extends.

---

## Canonical example files (read these as the live spec)

| Concern | File |
|---|---|
| Single LLM egress | `src/oneops/llm/gateway.py` |
| Policy composition | `src/oneops/policy/composer.py` |
| Routing pipeline | `src/oneops/router/router.py` |
| Span helpers | `src/oneops/observability/span_helpers.py` |
| Safe span attributes | `src/oneops/observability/safe_attrs.py` |
| Tenant context | `src/oneops/tenancy/` |
| UC handler shape | `src/oneops/use_cases/uc01_summarization/handlers.py` |
| UC retrieval | `src/oneops/use_cases/uc03_kb_lookup/kb_embed.py` |
| Registry loader | `src/oneops/registry/` |

When a convention here and a load-bearing file disagree, the file is closer to truth — open a finding under `docs/findings/` and surface the drift.

---

## Live activity stream (agent + tool, on the fly) — platform-automatic

Every turn dispatched through the executor emits a live activity stream the
UI renders as "which agent + which tool is running right now". This is
**platform behaviour, inherited by every UC — not per-UC code**:

- `executor/step_runner.py` publishes `tool_start` (`agent_id`, `tool_id`,
  and a one-line `action` taken from the tool's **registry `description`**,
  first sentence) and `tool_done` (`status`, `latency_ms`) for **every**
  step it runs, via `oneops.observability.event_sink`. It is best-effort: a
  no-op when nothing is streaming, so it never affects execution.
- `/api/chat/stream` and `/api/fast/{uc}/stream` forward these as NDJSON
  (`turn_start → tool_start / tool_done* → final`); the frontend
  `streamTurnInto()` animates the same panel for both the chat door and the
  fast-path buttons.

**Rule for new UCs (MUST follow):**
1. A UC built the standard way — a registered agent + tool dispatched via
   the executor — gets the live view **for free; do nothing.** Verified for
   UC-1, UC-2, UC-3, and inherited by every future standard UC.
2. A UC that executes tools **outside** the executor (a bespoke route/agent,
   e.g. the UC-5 triage propose/decide and UC-8 fulfillment flows) MUST
   `event_sink.publish(request_id, {...})` the same `tool_start` /
   `tool_done` shape at each tool boundary and expose a streaming endpoint.
   Do **not** invent a different activity format.
3. The one-line action is ALWAYS derived from the tool's registry
   `description` — never a hardcoded per-tool phrase (the no-static rule).

Enforced by `tests/unit/executor/test_step_runner_emits_events.py` (step
boundary publishes) + `scripts/uc_stream_devils_play.py` (end-to-end).

---

## Authority order

1. `docs/PROJECT-BRIEFING.md §2` — the 13 non-negotiable rules.
2. `docs/COMPONENT_SPEC.md` C1–C24 — the per-component contract.
3. This document — conventions distilled from (1), (2), and existing platform code.
4. Load-bearing files cited above — the live examples.

If any of these conflict with another, escalate to a `docs/findings/` entry and resolve before merging.
