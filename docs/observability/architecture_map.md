# Observability Architecture Map — POC copy 5

**Stack:** OpenTelemetry SDK (HTTP/protobuf OTLP exporter) + structlog for JSON logs with trace correlation.

**Init point:** `src/oneops/observability/__init__.py::setup_observability()` — called once at process startup. Idempotent.

**Disabled mode:** When `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, the MeterProvider falls back to a no-export configuration and BatchSpanProcessor is not attached. Every `span()` / `increment()` / `histogram()` becomes a sub-microsecond no-op. **Observability NEVER raises into business code** — every emit is wrapped in `try/except`.

**PII safety:** Raw user text, ticket bodies, KB content are NEVER captured into span attributes by default. The `OTEL_CAPTURE_TEXT=true` env flag opens a 4-KiB-bounded capture window for debugging only. Until then, every text attribute is `<prefix>_hash` (16-char sha256) + `<prefix>_len`.

---

## 1. Helper library — `src/oneops/observability/`

| Module | Purpose |
|---|---|
| `__init__.py` | Setup + re-exports of every public helper |
| `safe_attrs.py` | `safe_hash_text`, `safe_text_len`, `set_safe_text_attrs`, `capture_text_enabled`, `safe_json_attr`, `safe_list_attr` |
| `metrics.py` | `increment(name, value, **labels)`, `histogram(name, value, **labels)` — instruments cached by name |
| `span_helpers.py` | `span(name, **attrs)`, `llm_span(operation, model, **attrs)`, `set_attrs`, `record_exception_safe`, `current_trace_ids` |
| `cache_event.py` | `record_cache_get`, `record_cache_set`, `record_cache_delete` — emit events on current span + metrics |

---

## 2. Trace span inventory

### 2.1 Request & graph layer

| Span | Where | Captures |
|---|---|---|
| `graph.load_session` | `graph/nodes.py` `load_session_node` | session.id, focus presence |
| `graph.decomposer` | `routing/decomposer.py` | sub_query_count |
| `graph.shortlist` | `routing/uc_shortlister.py` | candidate_count |
| `graph.rerank` | `routing/uc_reranker.py` | top_uc_id |
| `graph.rewriter` | `routing/rewriter.py` | rewrite_kind |
| `graph.verifier` | `routing/verifier.py` | verdict, override_applied |
| `graph.uc_executor` | `graph/nodes.py` | uc_id, status |
| `graph.aggregator` | `graph/nodes.py` | fragment_count |
| `graph.content_safety` | `graph/nodes.py` | safety_verdict |
| `graph.planner` | `graph/nodes.py` | plan_steps |

### 2.2 State (session store)

All under `state.load` / `state.update`, discriminated by `state.kind` attribute.

| `state.kind` | Where | Attributes |
|---|---|---|
| `history` | `SessionStore.get_history` / `append_history` | state.history_len, state.role, state.content_len, state.found |
| `focus` | `SessionStore.get_focus` / `update_focus` / `set_focus` | state.focus_keys, state.focus_entity_id, state.update_keys, state.operation |
| `canonical` | `SessionStore.get_canonical_state` / `update_canonical_state` | state.key_count, state.last_successful_use_case, state.active_kb_id, state.update_keys |

### 2.3 LLM calls

Centralized in `gateway/client.py` — every outbound LLM request is one `llm.call` span.

**Attributes:** `llm.model`, `llm.prompt_hash`, `llm.temperature`, `llm.max_tokens`, `llm.queue_wait_ms`, `llm.prompt_tokens`, `llm.completion_tokens`, `llm.total_tokens`, `llm.cost_usd`, `llm.latency_ms`, `llm.actual_model`, `llm.finish_reason`, `llm.replay`, `oneops.request_id`, `llm.concurrency_cap`

**Specialized LLM operations (named spans, all also produce the gateway's `llm.call`):**

| Span | Where |
|---|---|
| `planner.llm.plan_request` | planner LLM call |
| `routing.decomposer` | sub-query decomposition |
| `routing.rerank` | UC reranker |
| `routing.rewriter` | rewriter LLM call |
| `safety.classify` | content safety classifier |
| `scope.classifier` | scope classifier |
| `verifier.classify` | verifier first-pass LLM |
| `quality.hallucination.validate` | hallucination validator |
| `subject.resolve` | subject resolver |
| `conversation.ordinal_resolver` | ordinal LLM resolver |
| `uc01.intent_classifier` | UC-1 intent LLM |
| `uc01.field_resolver` | UC-1 field resolver |
| `uc01.field_read` | UC-1 field read LLM |
| `uc03.field_resolver` | UC-3 field resolver |
| `uc03.relevance_gate` | UC-3 KB relevance gate |
| `uc03.handler` | UC-3 handler boundary |
| `uc03.retrieval.hybrid` | UC-3 hybrid retrieval |
| `uc03.rerank` | UC-3 reranker |
| `uc99.conversational` | UC-99 conversational handler |
| `llm.embed` | embedding calls (also via gateway) |

### 2.4 Tool calls

Every `@tool`-decorated coroutine emits `tool.<tool_id>` span.

**Attributes:** `tool.id`, `tool.service_id`, `tool.role`, `tool.latency_ms`, `tool.payload_size`, span status (OK / ERROR + exception class)

### 2.5 Cache events (on current span, not new spans)

| Cache | Event names | Trigger |
|---|---|---|
| `uc01_summary` | `cache.get` / `cache.set` | UC-1 summary cache hit/miss/store |
| `llm_replay` | `cache.get` / `cache.set` | Gateway replay cache hit/miss/store |

**Attributes:** `cache.name`, `cache.hit`, `cache.stale`, `cache.key_hash`, `cache.latency_ms`, `cache.payload_size`, `cache.ttl_seconds`

### 2.6 NATS

| Span | Where |
|---|---|
| `nats.publish` | `adapters/nats_client.py` outbound |
| `nats.request` | request-response |
| `nats.process` | inbound message handler in `invoker/nats_invoker.py` |

---

## 3. Metric inventory

### 3.1 LLM

| Metric | Type | Labels |
|---|---|---|
| `ai.llm.tokens.input.total` | counter | model |
| `ai.llm.tokens.output.total` | counter | model |
| `ai.llm.tokens.total` | counter | model |
| `ai.llm.latency_ms` | histogram | model, operation |
| `ai.llm.errors.total` | counter | model, error_type, operation |

### 3.2 Cache

| Metric | Type | Labels |
|---|---|---|
| `ai.cache.hits.total` | counter | cache_name |
| `ai.cache.misses.total` | counter | cache_name |
| `ai.cache.writes.total` | counter | cache_name |
| `ai.cache.deletes.total` | counter | cache_name |
| `ai.cache.stale_reads.total` | counter | cache_name |
| `ai.cache.latency_ms` | histogram | cache_name, operation |

### 3.3 Tools

| Metric | Type | Labels |
|---|---|---|
| `ai.tool.calls.total` | counter | tool_id, status, error_type? |
| `ai.tool.latency_ms` | histogram | tool_id, service_id |

---

## 4. Guarantees (tested in `tests/unit/observability/`)

1. **Setup is idempotent** — repeated `setup_observability()` calls are no-ops
2. **No-op when disabled** — `OTEL_EXPORTER_OTLP_ENDPOINT` unset → no exporter, no batch processor, no error on emit
3. **Never raises into business code** — every helper wraps emit in try/except
4. **PII scrub default ON** — `*_hash` + `*_len` attributes only; raw text only with explicit `OTEL_CAPTURE_TEXT=true`
5. **Bounded raw capture** — even with flag on, raw text capped at 4 KiB
6. **Instruments cached by name** — counters/histograms reuse the same instance
7. **None labels filtered** — OTel rejects None values; our wrappers drop them
8. **Business outcomes stay OK** — `clarification`, `no_match`, `not_found`, `ambiguous` → span status OK; only exceptions → ERROR

---

## 5. Operator usage

### View spans

When `OTEL_EXPORTER_OTLP_ENDPOINT` points to a Tempo / Jaeger / OTel-collector instance, spans appear there.

For local dev without a collector, set `OTEL_EXPORTER_OTLP_ENDPOINT=""` to disable export entirely (the SDK still tracks spans in-memory for the duration of the request but does not ship them).

### Enable raw-text capture (debug only)

```bash
export OTEL_CAPTURE_TEXT=true
```

**Do NOT enable in production** — the capture flag includes user queries, prompts, and response text in span attributes. Use only on a dev tenant with synthetic data.

### Per-request trace id

Every log line emitted via `oneops.observability.get_logger(...)` carries `trace_id` + `span_id` (when inside a span context). Searchable in any log aggregator alongside the trace.

---

## 6. Gaps (deferred)

| Gap | Reason deferred | Workaround |
|---|---|---|
| HTTP server entry span | No HTTP layer yet; entry is the LangGraph DAG | `graph.load_session` is the de-facto root |
| DB query spans | Rare and short; deferred until a slow DB call shows up | Use Postgres slow-query log |
| Prometheus scrape endpoint | OTel metrics go to collector; Prometheus is the collector's downstream | Configure exporter on the collector |
| Sampler config UI | Sampler is `ParentBased(TraceIdRatioBased(N))`; N comes from settings | Set `OTEL_TRACES_SAMPLER_ARG` in env |

---

## 7. Change log

| Date | Phase | Change |
|---|---|---|
| 2026-05-19 | P0a | `safe_attrs.py` + `OTEL_CAPTURE_TEXT` flag + 12 tests |
| 2026-05-19 | P0b | `metrics.py` + MeterProvider wiring + LLM/cache counters + 8 tests |
| 2026-05-19 | P1a | `span_helpers.py` + `cache_event.py` + 16 tests |
| 2026-05-19 | P1b | `state.*` discriminator spans + replay-cache events |
| 2026-05-19 | P2 | Tool decorator metrics + architecture map + smoke bundle |
