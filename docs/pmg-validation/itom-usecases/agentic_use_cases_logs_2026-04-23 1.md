# ObserveOps Agentic AI Use-Case Catalog
_Scope: logs · Generated: 2026-04-23 · 18 use-cases across 18 features_

## Top 10 cross-domain plays
_Use-cases that chain ≥2 capabilities or implicitly span ≥2 CSVs (log + metric, log + NCCM deploy, log + APM trace, etc.). Ranked by Impact then Effort._

| Rank | Sr No | Use-case | Agent(s) | Why it's cross-domain | Impact | Effort |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 33 | Pattern-cluster RCA walker | rca-analyst → forecaster | Links anomalous log clusters to recent deploys (NCCM), config changes, upstream service errors, co-occurring clusters | H | H |
| 2 | 31 | Log-pattern ↔ metric-spike linker | correlator + anomaly-detector | Joins log patterns with metric anomalies on service/host/time | H | H |
| 3 | 26 | Malicious-IP blast-radius RCA | rca-analyst + correlator | Cites log lines, auth events, lateral-movement traces, affected assets | H | M |
| 4 | 15 | Timeline-window log bundling around a pinned event | correlator → rca-analyst | Pulls logs across services tied to an alert/APM event by shared trace ID, host, correlated spike | H | M |
| 5 | 28 | Plain-English description + human-friendly name per log pattern | narrator | Output feeds RCA, alert policies, dashboards across every domain | H | L |
| 6 | 16 | NL → log search DSL with time window | query-translator | Replaces DSL fluency prerequisite for every log user | H | L |
| 7 | 30 | Audience-tailored narrative for compliance reports | narrator | HIPAA/PCI/GDPR reporting is a platform-wide concern; output consumed by auditors + eng | H | M |
| 8 | 3 | Sample log → parser config (regex / delimiter / JSON) | query-translator | Onboarding new log sources is a recurring platform chore | H | M |
| 9 | 14 | NL → indexed-field analytics query | query-translator | Log analytics surfacing is used inside dashboards, alerts, and ad-hoc investigation | H | M |
| 10 | 32 | NL → facet selection + residual text query | query-translator | Removes DSL barrier from facet-based search for casual users | H | M |

## By domain

### Log Monitoring

| Sr No | Feature (excerpt, ≤80 chars) | Use-case | Agent | Trigger | Output | Impact | Effort |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 3 | UI-based dynamic log parser (regex, delimiter, JSON, custom plugins) | Sample log → parser config (regex/delimiter/JSON) | query-translator | User pastes 1-3 raw log lines in parser builder | Proposed parser type + regex/grok/JSON path with named fields, validated against the sample | H | M |
| 9 | Multi-line log parsing via regex through UI | NL+sample → multi-line regex boundary pattern | query-translator | User pastes a multi-line stack trace and describes the record boundary | Start/continuation regex pair + preview of correctly merged records | H | M |
| 12 | Live log tailing with advanced filtering, search, split-view | NL → live-tail filter expression for split panes | query-translator | User types "show only 5xx from checkout-svc, and errors from auth-svc" in tail bar | Two tail filter expressions bound to split-view panes with match counts | M | L |
| 13 | Pre-built dashboards per tech stack with drill-down to raw log details | Narrated dashboard tour per tech stack | narrator | User opens a pre-built stack dashboard | 4-6 sentence plain-English walkthrough of what each panel shows and what "normal" looks like for this stack | M | L |
| 14 | Advanced log analytics with user-defined filters on indexed fields | NL → indexed-field filter/aggregation query | query-translator | User asks "top 10 hosts by ERROR count last 24h" in analytics bar | Executable analytics query over indexed fields + result preview | H | M |
| 15 | Contextual review of logs surrounding specified events via timeline | Agentic timeline-window log bundling around a pinned event | correlator | User pins an event / alert fires | Grouped log bundle across services within ± window, with "why included" rationale (shared trace ID, host, correlated spike) | H | M |
| 16 | Robust log search for quick retrieval of specific entries | NL → log search DSL query with time window | query-translator | User types "checkout timeouts over 2s in prod last 1h" in search box | Ready-to-run search DSL query, executed with hit count and sample matches | H | L |
| 17 | Real-time analytics on log attribute contribution with counts and % | Attribute distribution narrative on hover | narrator | User hovers/clicks a log attribute breakdown widget | 2-3 sentence summary naming top contributors, their share, and any notable skew vs prior window | M | L |
| 20 | Reporting with export of raw log data by custom criteria | Auto-generated narrative cover page for exported log reports | narrator | User triggers a raw-log export/report | 1-page plain-English summary (scope, volume, top services, notable errors) prepended to the export | M | L |
| 22 | Data collection statistics dashboards for ingestion tuning | Weekly ingestion health narrative for log admins | narrator | Scheduled weekly job on ingestion stats | Narrative report: noisiest sources, drop/throttle events, agents lagging, tuning suggestions to review | M | M |
| 23 | Dynamic policy creation for grouping and filtering | NL → grouping/filtering policy YAML | query-translator | User describes "group by service+region, drop debug logs from staging" | Policy definition with group-by keys + filter rules, dry-run result on recent logs | M | M |
| 26 | Native malicious IP feed integration for threat detect/report/mitigate | Malicious-IP blast-radius RCA | rca-analyst | Threat-feed hit on inbound/outbound log IP | Ranked attack-path hypotheses with cited log lines, auth events, lateral-movement traces, and affected assets | H | M |
| 27 | Custom views (private/shared/public) from user queries | NL → saved-view definition with scope + facets | query-translator | User clicks "build view" and describes "payments errors by region, shared with SRE" | View spec: base query, default facets, visibility scope, ready to save | M | L |
| 28 | Auto log pattern detection with variable masking and distribution | Plain-English description + human-friendly name for each new pattern | narrator | Pattern detection job emits a new/changed pattern | 2-3 sentence pattern description, suggested name, and one-line "when you typically see this" note | H | L |
| 30 | OOTB compliance reporting for HIPAA, PCI-DSS, GDPR and similar | Audience-tailored narrative cover for compliance reports | narrator | User generates a HIPAA/PCI/GDPR report | Audience-specific (auditor vs eng) narrative summary of coverage, gaps, and period-over-period deltas | H | M |
| 31 | Deep correlation of log events with metrics to flag anomalies | Agentic log-pattern to metric-spike linker | correlator | Scheduled sweep or new metric anomaly | Linked card: metric anomaly + co-occurring log patterns + join keys (service, host, time) + confidence score | H | H |
| 32 | Facet-based log search without complex query languages | NL → facet selection + residual free-text query | query-translator | User types conversational phrase into facet search bar | Pre-selected facet chips (service, env, severity) + remaining text filter, executed | H | M |
| 33 | Log pattern analytics engine clustering + pattern-based RCA | Pattern-cluster RCA walker | rca-analyst | New/spiking log cluster detected or user clicks "investigate cluster" | Ranked cause hypotheses linking the anomalous cluster to recent deploys, config changes, upstream service errors, and co-occurring clusters with evidence trail | H | H |

## By capability

### narrator
- **Sr 13 — Log Monitoring** — Narrated dashboard tour per tech stack
- **Sr 17 — Log Monitoring** — Attribute distribution narrative on hover
- **Sr 20 — Log Monitoring** — Auto-generated narrative cover page for exported log reports
- **Sr 22 — Log Monitoring** — Weekly ingestion health narrative for log admins
- **Sr 28 — Log Monitoring** — Plain-English description + human-friendly name for each new pattern
- **Sr 30 — Log Monitoring** — Audience-tailored narrative cover for compliance reports

### query-translator
- **Sr 3 — Log Monitoring** — Sample log → parser config (regex/delimiter/JSON)
- **Sr 9 — Log Monitoring** — NL+sample → multi-line regex boundary pattern
- **Sr 12 — Log Monitoring** — NL → live-tail filter expression for split panes
- **Sr 14 — Log Monitoring** — NL → indexed-field filter/aggregation query
- **Sr 16 — Log Monitoring** — NL → log search DSL query with time window
- **Sr 23 — Log Monitoring** — NL → grouping/filtering policy YAML
- **Sr 27 — Log Monitoring** — NL → saved-view definition with scope + facets
- **Sr 32 — Log Monitoring** — NL → facet selection + residual free-text query

### correlator
- **Sr 15 — Log Monitoring** — Agentic timeline-window log bundling around a pinned event
- **Sr 31 — Log Monitoring** — Agentic log-pattern to metric-spike linker

### rca-analyst
- **Sr 26 — Log Monitoring** — Malicious-IP blast-radius RCA
- **Sr 33 — Log Monitoring** — Pattern-cluster RCA walker

## Skipped

| CSV | augmentable | automatable | not-applicable |
| --- | --- | --- | --- |
| FSO_RFP_Log_Monitoring.csv | 18 | 8 | 7 |

_automatable rows: 2, 5, 6, 10, 19, 24, 25, 29 (OOTB parsers, forwarding, timezone auto-detect, parser plugin framework, retention policies, scheduled policy evaluation, severity detection, attribute normalization)._

_not-applicable rows: 1, 4, 7, 8, 11, 18, 21 (syslog TCP/UDP ingestion, remote collection protocols, remote collectors plumbing, limitless indexing capacity, real-time tailing display, raw keyword search, viz widget catalog)._
