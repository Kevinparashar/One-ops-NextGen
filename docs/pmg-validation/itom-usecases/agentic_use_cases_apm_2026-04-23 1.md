# ObserveOps Agentic AI Use-Case Catalog
_Scope: apm · Generated: 2026-04-23 · 25 use-cases across 19 features_

## Top 10 cross-domain plays
_Use-cases that chain ≥2 capabilities or implicitly span ≥2 CSVs (APM + Infra, APM + Logs, APM + Dashboards, APM + Alert Policy, etc.). Ranked by Impact then Effort._

| Rank | Sr No | Use-case | Agent(s) | Why it's cross-domain | Impact | Effort |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 21 | Unified trace↔JVM↔log investigation card | correlator → rca-analyst | APM + Infra + Logs; bundles trace, JVM GC/heap anomalies, log patterns with join keys | H | H |
| 2 | 29 | Cross-layer KPI co-movement grouper | correlator | APM + Infra + Metric Explorer; co-moving KPIs across host/service/container/process/DB + spans | H | H |
| 3 | 5 | Log-trace timeline RCA walker | rca-analyst + correlator | APM + Logs; merged timeline of cited log lines and span IDs | H | H |
| 4 | 12 | Trace-span to infra-metric linker with rationale | correlator → rca-analyst | APM + Infra; host/container metrics co-spiking in span window | H | M |
| 5 | 26 | JVM-anomaly → trace RCA walker | rca-analyst | APM + Infra (JVM runtime) | H | M |
| 6 | 28 | Slow-query regression RCA | rca-analyst | APM + DB metrics + NCCM (schema/config diff timeline) | H | M |
| 7 | 25 | NL → alert policy YAML for APM | query-translator | APM + Alert Policy; generates platform-compliant policy with dry-run | H | M |
| 8 | 14 | NL → trace-dashboard JSON generator | query-translator + narrator | APM + Dashboard; importable dashboard bound to root-span attributes | H | M |
| 9 | 9 | Stack trace plain-English explainer | narrator | Broad utility; surfaces across every exception-producing domain | H | L |
| 10 | 15 | NL → Trace Explorer filter query | query-translator | Broad utility; removes DSL barrier for trace investigation | H | L |

## By domain

### APM

| Sr No | Feature (excerpt, ≤80 chars) | Use-case | Agent | Trigger | Output | Impact | Effort |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 5 | Correlating app logs with distributed traces for unified RCA timeline | Log-trace timeline RCA walker | rca-analyst | User clicks "investigate" on failing trace or error spike alert | Ranked cause hypotheses with cited log lines, span IDs, and timestamps on a merged timeline | H | H |
| 5 | Correlating app logs with distributed traces for unified RCA timeline | Differential trace-vs-baseline analyzer | rca-analyst | Latency/error SLO burn alert fires | Hypotheses naming which span + log pattern diverged from last-week baseline, with evidence links | H | M |
| 8 | Interactive service map with dependencies, latencies, health impacts | Plain-English service map narrative | narrator | User opens service map or hovers a node/edge | 3-4 sentence description of node's role, key dependencies, current health posture, and blast radius | H | M |
| 9 | Automated error tracking with stack traces and categorization | Stack trace plain-English explainer | narrator | New/recurring exception captured in trace view | 2-3 sentence explanation naming likely subsystem, failing operation, and error category | H | L |
| 9 | Automated error tracking with stack traces and categorization | Error cluster digest across services | narrator | Scheduled (daily) or on-demand per service | Narrative digest grouping similar exceptions with representative example and scope (services, frequency) | M | M |
| 12 | Capture infra metrics and correlate with trace & service performance | Trace-span to infra-metric linker with rationale | correlator | Slow/errored span opened in trace view | Ranked list of host/container metrics co-spiking in span window + 1-line "why linked" (time, host, container ID) | H | M |
| 14 | Custom dashboard creation using trace root span attributes | NL-to-trace-dashboard JSON generator | query-translator | User types "p95 latency & error rate by root span service for checkout" in dashboard builder chat | Importable dashboard JSON with 4-6 widgets bound to root-span attributes, plus live preview of each widget | H | M |
| 14 | Custom dashboard creation using trace root span attributes | Example-trace-to-KPI-widget generator | query-translator | User pins an exemplar trace and clicks "build KPI from this" | Widget config (metric expression over root span attrs + group-by) added to active dashboard with preview | M | M |
| 15 | Trace Explorer filter by latency, error, service, tags | NL-to-Trace-Explorer filter query | query-translator | User types "slow checkout traces with 5xx in last 2h for prod tenant" into Trace Explorer search bar | Structured filter JSON (latency>threshold, status, service, tag predicates) with preview of first 20 matching traces | H | L |
| 17 | Per-span attribute visibility with execution context and anomalies | Span anomaly annotation in plain English | narrator | User selects a span flagged as anomalous | 1-2 sentence note calling out which attributes deviate (e.g., slow DB call, unusual payload size) and how | M | M |
| 18 | Dedicated service view with KPIs, response time, throughput, errors | Service health narrative summary | narrator | User opens service view or scheduled shift start | 5-bullet summary of current health vs baseline, top movers across KPIs, notable errors | H | L |
| 18 | Dedicated service view with KPIs, response time, throughput, errors | On-call handoff report per service | narrator | End of on-call shift | Handoff note: open incidents, near-misses, recent deploys, things to watch next shift | H | M |
| 19 | Transaction-level visibility with path, errors, latency breakdown | Slow/failed transaction storyline | narrator | User clicks a slow or failed transaction | Narrative walkthrough of request path, where time was spent, where it failed, in plain English | H | M |
| 20 | APM ingestion statistics dashboard for ingestion rates and agents | Ingestion health weekly narrative | narrator | Weekly schedule or ingestion anomaly detected | Short narrative cover: volume trends, noisy agents, config suggestions worth reviewing | M | L |
| 21 | Correlate traces with host, JVM, log patterns, service KPIs for RCA | Unified trace↔JVM↔log investigation card | correlator | User clicks "find related" on a trace or alert fires on service SLO | Single card bundling the trace, JVM GC/heap anomalies, matching log patterns, and KPI deviations with join keys (trace ID, host, time) and audit trail | H | H |
| 23 | Auto-generation of service registration commands | Intent-to-service-registration command builder | query-translator | User selects runtime/language and pastes service name or picks from discovery | Ready-to-run agent install + registration CLI snippet (env vars, endpoint, tags) validated against platform schema | H | L |
| 25 | Policy-based threshold config on APM metrics/span attrs | NL-to-alert-policy YAML for APM | query-translator | User types "alert when checkout p99 > 800ms for 10m in prod" in policy editor | Platform-compliant alert policy YAML (metric, span filter, threshold, duration, action) with dry-run evaluation on last 24h | H | M |
| 26 | JVM perf monitoring (CPU, heap, GC, threads) correlated with traces | JVM-anomaly to trace RCA walker | rca-analyst | GC pause spike, heap pressure, or thread-count anomaly on a Java service | Ranked causes (e.g., "leak in endpoint X evidenced by spans A/B + heap growth pattern") with trace + JVM counter citations | H | M |
| 26 | JVM perf monitoring (CPU, heap, GC, threads) correlated with traces | Thread-contention RCA from latency alert | rca-analyst | P95 latency alert on JVM-backed service | Hypothesis tying slow spans to specific thread pool saturation / lock contention, with thread-dump + span evidence | H | M |
| 27 | Auto-detection of API endpoints with endpoint-level analytics | Endpoint performance narrative card | narrator | User opens endpoint detail or weekly report | 3-4 sentence summary of endpoint latency, error rate, throughput vs threshold, and notable shifts | M | L |
| 28 | DB analytics — slow query, call latency, pool utilization, counters | Slow-query regression RCA | rca-analyst | Slow query detector or DB latency alert fires | Ranked causes (plan change, schema/config diff, pool exhaustion, upstream traffic shift) with query text, plan, and change-timeline citations | H | M |
| 28 | DB analytics — slow query, call latency, pool utilization, counters | Connection-pool-exhaustion RCA | rca-analyst | Pool saturation alert or DB-call timeout spike | Hypothesis identifying which caller service + code path drove exhaustion, evidenced by trace spans + pool counters | M | M |
| 29 | Cross-layer KPI correlation: host, service, container, process, DB, spans | Cross-layer KPI co-movement grouper | correlator | Scheduled sweep over active incidents or on-demand "correlate KPIs" | Grouped bundle of co-moving KPIs across layers with confidence score, shared dimensions (host/container/DB instance), and suppression of known-benign co-movements | H | H |
| 30 | Trace/span analytics with aggregation and drill-down | NL-to-span-analytics aggregation query | query-translator | Analyst types "top 10 error codes by span.kind=server grouped by service last 24h" | Executable aggregation query + rendered breakdown table/chart with drill-down links to matching spans | H | M |
| 32 | OOTB error/exception tracking with facet-based filtering | Facet-filter result summarization | narrator | User applies filters (service, error type, status, region) | 2-3 sentence summary of what the filtered slice shows: dominant errors, affected services, trend | M | L |

## By capability

### narrator
- **Sr 8 — APM** — Plain-English service map narrative
- **Sr 9 — APM** — Stack trace plain-English explainer
- **Sr 9 — APM** — Error cluster digest across services
- **Sr 17 — APM** — Span anomaly annotation in plain English
- **Sr 18 — APM** — Service health narrative summary
- **Sr 18 — APM** — On-call handoff report per service
- **Sr 19 — APM** — Slow/failed transaction storyline
- **Sr 20 — APM** — Ingestion health weekly narrative
- **Sr 27 — APM** — Endpoint performance narrative card
- **Sr 32 — APM** — Facet-filter result summarization

### query-translator
- **Sr 14 — APM** — NL-to-trace-dashboard JSON generator
- **Sr 14 — APM** — Example-trace-to-KPI-widget generator
- **Sr 15 — APM** — NL-to-Trace-Explorer filter query
- **Sr 23 — APM** — Intent-to-service-registration command builder
- **Sr 25 — APM** — NL-to-alert-policy YAML for APM
- **Sr 30 — APM** — NL-to-span-analytics aggregation query

### correlator
- **Sr 12 — APM** — Trace-span to infra-metric linker with rationale
- **Sr 21 — APM** — Unified trace↔JVM↔log investigation card
- **Sr 29 — APM** — Cross-layer KPI co-movement grouper

### rca-analyst
- **Sr 5 — APM** — Log-trace timeline RCA walker
- **Sr 5 — APM** — Differential trace-vs-baseline analyzer
- **Sr 26 — APM** — JVM-anomaly to trace RCA walker
- **Sr 26 — APM** — Thread-contention RCA from latency alert
- **Sr 28 — APM** — Slow-query regression RCA
- **Sr 28 — APM** — Connection-pool-exhaustion RCA

## Skipped

| CSV | augmentable | automatable | not-applicable |
| --- | --- | --- | --- |
| FSO_RFP_APM.csv | 19 | 7 | 6 |

_automatable rows: 4, 10, 11, 13, 22, 24, 31 (span tagging metadata, intelligent trace sampling, throughput/latency/error rate metrics, custom metrics ingestion + dynamic entity detection, service-registration parameters, retention policies, K8s/OpenShift DaemonSet auto-instrumentation)._

_not-applicable rows: 1, 2, 3, 6, 7, 16 (agent-based auto-instrumentation for multiple languages, manual instrumentation SDKs, high-fidelity trace ingestion, service path visualization as display feature, trace context propagation plumbing, multi-view span viz widgets)._
