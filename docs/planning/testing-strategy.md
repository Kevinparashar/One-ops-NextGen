# Testing Strategy — Oneops-NextGen V1

> Status: 2026-06-04. Builds on the existing pytest setup; fixes the coverage-gate
> gap and defines the test contract for the hardening work.

## Current setup (as-built)

- pytest, `asyncio_mode=auto`, `testpaths=["tests"]`, markers `unit/integration/
  stress/slow` (`pyproject.toml:69-81`); coverage `fail_under=70`, branch on.
- Layout: `tests/unit/` (~112 files, 19 dirs), `tests/integration/`, conftest at
  `tests/conftest.py` (observability init + test env defaults) and
  `tests/integration/conftest.py` (routes LLM gateway to real OpenAI if key set).
- Local CI gate `scripts/ci.sh`: ruff → mypy → `pytest -m unit` → `pytest -m
  integration` (skipped in `--fast`) → smoke (placeholder) → devils (placeholder).

## The gate gap (P0-1) — RESOLVED 2026-06-04 (Batch A1)

`pytest -m unit` originally selected only ~19 marked tests (1572 deselected), so the
gate validated ~1% of the suite — and 19 of the skipped tests were actually *failing*
(cross-test pollution + integration-class tests needing live infra).

**Fix shipped:** `tests/unit/conftest.py` auto-marks every unit-dir test `unit`
unless it opts into `integration`/`slow`/`stress`; the real isolation bugs were
fixed (event-loop pollution → `asyncio.run`; `get_settings()` `lru_cache` leak →
autouse `cache_clear`); and integration-class tests (7 uc08 live-DB files + 3
session pipeline tests) were marked `integration`. Result: `-m unit` now selects the
**full 1510-test hermetic suite** (green, ~102s); `-m integration tests` (tree-wide)
runs the 81 reclassified tests in their lane. The unit lane is hermetic — no infra.

Open follow-up (R-5): `ci-fast` is now ~1:42; add `pytest-xdist` if per-commit speed
matters (deliberate dependency decision).

## Test categories & where they live

| Category | Scope | Location | External deps |
|---|---|---|---|
| Unit | pure logic, validation, prompt-building, LLM-output parsing, error mapping, config | `tests/unit/**` | none (mock at boundary) |
| Integration | handler→service, route→logic, NATS flow, cache flow, repo flow, gateway w/ mocked provider | `tests/integration/**` | dragonfly/nats/pg/llm (or OPENAI key) |
| Smoke | app imports, config loads, app starts, health, one critical path mocked | `scripts/smoke_routing.py` (to build, 6c) | mocked |
| Devils | malformed/empty/oversized input, missing config, dep timeout, bad LLM output, dup event, partial failure, contract break | `scripts/devils_play.py` (to build) + per-change | mocked |

## Validation contract for every hardening change

After each change, run and record:
1. **Smoke** — app imports + critical happy path (mocked deps).
2. **Unit** — targeted tests for the changed logic (add if missing); plus full
   `pytest tests/unit/<area>` for the touched area.
3. **Integration** — the affected flow (mock external only where needed).
4. **Regression** — confirm baseline goalposts unchanged (unit 1572✓/19 known-flake;
   integration 77✓/4 known; devils 98/100) — classify any new failure mine-vs-flake.
5. **Devils** — the relevant rows from the exposure matrix (audit) for this change.
6. **Risk note** — what was verified, what could still break, what needs manual review.

## Principles

- Mock ONLY at external boundaries (LLM provider, NATS, Postgres, Dragonfly).
- No fake/always-pass tests; assert real behavior.
- Deterministic: reset shared globals in fixtures (precedent: UC-3 KB tests reset
  `set_kb_embed_fn(None)`/`set_kb_relevance_scorer(None)`).
- Contract tests (6d): assert route paths, NATS subjects, and response schema keys
  so a contract break fails CI loudly.

## Known pre-existing flakes (do not attribute to new changes)

- Unit: `test_time_filter_extractor` ×13, `test_session_continuity` ×3, `uc08`
  (`catalog_search`/`e2e_devils_play`) ×2, `uc08_routes` concurrent ×1 — LLM/DB/
  gateway-dependent without live infra.
- Integration: 3 perf-load budget tests (`registry_loads_10k`, lexical-retrieval
  latency ×2) — machine-load dependent; 1 uc08 button journey — LLM-flaky, passes on retry.
