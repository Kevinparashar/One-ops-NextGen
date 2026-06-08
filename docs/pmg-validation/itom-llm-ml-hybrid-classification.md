# ITOM Use-Case Classification — LLM / ML / Hybrid

> **Source catalog:** `docs/pmg-validation/itom-usecases/agentic_use_cases_{apm,logs,metric,nccm,rest,rum}_2026-04-23 1.md`
> **Generated:** 2026-06-05 · 181 use-cases across 6 domains (REST split into 10 sub-domains)

## Legend

**Class**
- **LLM** — needs only a language model (generation / reasoning / narration over data it is given). Capabilities: `narrator`, `query-translator`.
- **Hybrid (ML+LLM)** — needs a learned model (vector similarity, clustering, forecasting, anomaly, topology/risk) **and** an LLM. Capabilities: `rca-analyst`, `correlator`, `forecaster`, `impact-analyzer`, `remediator`.
- **ML-only** — a learned model with no LLM. None in this catalog (the anomaly/forecast engines it narrates are pre-existing platform inputs, not new work).

**Footnotes**
- **¹** — LLM that *consumes* a pre-existing ML signal (anomaly detector, forecaster, frustration/pattern detector). The new agentic work is LLM-only; end-to-end the feature is ML+LLM. Runs only if that upstream signal is fed in.
- **²** — Key-join correlator: bundles on shared keys (trace-ID / host / time) — deterministic join + LLM rationale, not a learned model. Marked Hybrid because it is a `correlator` whose production-grade form adds semantic similarity.

**Agent column** — the registry agent that would serve the use-case, named `itom_<domain>_<capability>` (agents are created per capability × domain × trigger surface; the ML/LLM/retrieval compute lives in the tools the agent binds).

---

## APM (25)

| Sr | Use-case | Capability | Agent | Class |
|----|----------|------------|-------|-------|
| 5 | ITOM Log-trace timeline RCA walker | rca-analyst | itom_apm_rca_analyst | Hybrid |
| 5 | ITOM Differential trace-vs-baseline analyzer | rca-analyst | itom_apm_rca_analyst | Hybrid |
| 8 | ITOM Plain-English service map narrative | narrator | itom_apm_narrator | LLM |
| 9 | ITOM Stack-trace plain-English explainer | narrator | itom_apm_narrator | LLM |
| 9 | ITOM Error cluster digest across services | narrator | itom_apm_narrator | LLM |
| 12 | ITOM Trace-span ↔ infra-metric linker w/ rationale | correlator | itom_apm_correlator | Hybrid |
| 14 | ITOM NL → trace-dashboard JSON | query-translator | itom_apm_query_translator | LLM |
| 14 | ITOM Example-trace → KPI-widget | query-translator | itom_apm_query_translator | LLM |
| 15 | ITOM NL → Trace-Explorer filter query | query-translator | itom_apm_query_translator | LLM |
| 17 | ITOM Span anomaly annotation (plain English) | narrator | itom_apm_narrator | LLM¹ |
| 18 | ITOM Service health narrative summary | narrator | itom_apm_narrator | LLM |
| 18 | ITOM On-call handoff report per service | narrator | itom_apm_narrator | LLM |
| 19 | ITOM Slow/failed transaction storyline | narrator | itom_apm_narrator | LLM |
| 20 | ITOM Ingestion health weekly narrative | narrator | itom_apm_narrator | LLM |
| 21 | ITOM Unified trace↔JVM↔log investigation card | correlator | itom_apm_correlator | Hybrid |
| 23 | ITOM Intent → service-registration command builder | query-translator | itom_apm_query_translator | LLM |
| 25 | ITOM NL → alert-policy YAML for APM | query-translator | itom_apm_query_translator | LLM |
| 26 | ITOM JVM-anomaly → trace RCA walker | rca-analyst | itom_apm_rca_analyst | Hybrid |
| 26 | ITOM Thread-contention RCA from latency alert | rca-analyst | itom_apm_rca_analyst | Hybrid |
| 27 | ITOM Endpoint performance narrative card | narrator | itom_apm_narrator | LLM |
| 28 | ITOM Slow-query regression RCA | rca-analyst | itom_apm_rca_analyst | Hybrid |
| 28 | ITOM Connection-pool-exhaustion RCA | rca-analyst | itom_apm_rca_analyst | Hybrid |
| 29 | ITOM Cross-layer KPI co-movement grouper | correlator | itom_apm_correlator | Hybrid |
| 30 | ITOM NL → span-analytics aggregation query | query-translator | itom_apm_query_translator | LLM |
| 32 | ITOM Facet-filter result summarization | narrator | itom_apm_narrator | LLM |

**APM: 16 LLM · 9 Hybrid**

---

## Logs (18)

| Sr | Use-case | Capability | Agent | Class |
|----|----------|------------|-------|-------|
| 3 | ITOM Sample log → parser config | query-translator | itom_logs_query_translator | LLM |
| 9 | ITOM NL+sample → multi-line regex boundary | query-translator | itom_logs_query_translator | LLM |
| 12 | ITOM NL → live-tail filter for split panes | query-translator | itom_logs_query_translator | LLM |
| 13 | ITOM Narrated dashboard tour per tech stack | narrator | itom_logs_narrator | LLM |
| 14 | ITOM NL → indexed-field filter/aggregation | query-translator | itom_logs_query_translator | LLM |
| 15 | ITOM Timeline-window log bundling around event | correlator | itom_logs_correlator | Hybrid² |
| 16 | ITOM NL → log search DSL w/ time window | query-translator | itom_logs_query_translator | LLM |
| 17 | ITOM Attribute distribution narrative on hover | narrator | itom_logs_narrator | LLM |
| 20 | ITOM Narrative cover page for log-export reports | narrator | itom_logs_narrator | LLM |
| 22 | ITOM Weekly ingestion health narrative | narrator | itom_logs_narrator | LLM |
| 23 | ITOM NL → grouping/filtering policy YAML | query-translator | itom_logs_query_translator | LLM |
| 26 | ITOM Malicious-IP blast-radius RCA | rca-analyst | itom_logs_rca_analyst | Hybrid |
| 27 | ITOM NL → saved-view definition | query-translator | itom_logs_query_translator | LLM |
| 28 | ITOM Plain-English name+desc per log pattern | narrator | itom_logs_narrator | LLM¹ |
| 30 | ITOM Audience-tailored compliance report narrative | narrator | itom_logs_narrator | LLM |
| 31 | ITOM Log-pattern ↔ metric-spike linker | correlator | itom_logs_correlator | Hybrid |
| 32 | ITOM NL → facet selection + residual query | query-translator | itom_logs_query_translator | LLM |
| 33 | ITOM Pattern-cluster RCA walker | rca-analyst | itom_logs_rca_analyst | Hybrid |

**Logs: 14 LLM · 4 Hybrid**

---

## Metric Explorer (10)

| Sr | Use-case | Capability | Agent | Class |
|----|----------|------------|-------|-------|
| 2 | ITOM LLM-suggested KPI group for overlay | correlator | itom_metric_correlator | Hybrid |
| 2 | ITOM Co-movement explainer across KPIs | correlator | itom_metric_correlator | Hybrid |
| 3 | ITOM Period-over-period regression narrative | narrator | itom_metric_narrator | LLM |
| 3 | ITOM Behavioral-shift callout annotations | narrator | itom_metric_narrator | LLM |
| 4 | ITOM Anomaly narration on detector output | narrator | itom_metric_narrator | LLM¹ |
| 4 | ITOM Forecast explanation in plain English | narrator | itom_metric_narrator | LLM¹ |
| 4 | ITOM Delta/derivative interpretation helper | narrator | itom_metric_narrator | LLM |
| 4 | ITOM Log-scale / MA "what am I looking at" helper | narrator | itom_metric_narrator | LLM |
| 7 | ITOM NL → curated metric view template | query-translator | itom_metric_query_translator | LLM |
| 7 | ITOM Example monitor → portable view template | query-translator | itom_metric_query_translator | LLM |

**Metric: 8 LLM · 2 Hybrid**

---

## NCCM (32)

| Sr | Use-case | Capability | Agent | Class |
|----|----------|------------|-------|-------|
| 1 | ITOM Drift-to-impact translator | impact-analyzer | itom_nccm_impact_analyzer | Hybrid |
| 2 | ITOM Plain-English config-diff explainer | narrator | itom_nccm_narrator | LLM |
| 2 | ITOM Baseline drift narrative for a device | narrator | itom_nccm_narrator | LLM |
| 5 | ITOM Vendor config template from snippet | query-translator | itom_nccm_query_translator | LLM |
| 5 | ITOM NL → template authoring | query-translator | itom_nccm_query_translator | LLM |
| 7 | ITOM Shift-handoff narrative from task board | narrator | itom_nccm_narrator | LLM |
| 7 | ITOM NL dashboard annotation "what changed today" | narrator | itom_nccm_narrator | LLM |
| 9 | ITOM Audience-tailored cover for OOTB reports | narrator | itom_nccm_narrator | LLM |
| 10 | ITOM Onboarding-failure audit-trail RCA walker | rca-analyst | itom_nccm_rca_analyst | Hybrid |
| 10 | ITOM Batch-discovery failure differential RCA | rca-analyst | itom_nccm_rca_analyst | Hybrid |
| 11 | ITOM Plain-English change-window action history | narrator | itom_nccm_narrator | LLM |
| 11 | ITOM Per-command output interpreter | narrator | itom_nccm_narrator | LLM |
| 12 | ITOM Pre-flight firmware-upgrade blast-radius | impact-analyzer | itom_nccm_impact_analyzer | Hybrid |
| 13 | ITOM NL → Explorer filter query | query-translator | itom_nccm_query_translator | LLM |
| 15 | ITOM NL → runbook CSV context generator | query-translator | itom_nccm_query_translator | LLM |
| 15 | ITOM Example → runbook from one device session | query-translator | itom_nccm_query_translator | LLM |
| 19 | ITOM Human-friendly rule rationale card | narrator | itom_nccm_narrator | LLM |
| 19 | ITOM Per-device violation narrative w/ remediation | narrator | itom_nccm_narrator | LLM |
| 21 | ITOM Weekly compliance posture narrative | narrator | itom_nccm_narrator | LLM |
| 21 | ITOM Status-transition storyline for a device | narrator | itom_nccm_narrator | LLM |
| 22 | ITOM NL → benchmark composer | query-translator | itom_nccm_query_translator | LLM |
| 22 | ITOM NL → custom-rule authoring | query-translator | itom_nccm_query_translator | LLM |
| 25 | ITOM Corrective CLI-diff w/ approval workflow | remediator | itom_nccm_remediator | Hybrid |
| 25 | ITOM Synthesise bulk remediation runbook | remediator | itom_nccm_remediator | Hybrid |
| 28 | ITOM Syslog → change-event correlation (actor) | correlator | itom_nccm_correlator | Hybrid² |
| 28 | ITOM Group change-storm into one campaign | correlator | itom_nccm_correlator | Hybrid |
| 29 | ITOM Pre-push config-diff impact predictor | impact-analyzer | itom_nccm_impact_analyzer | Hybrid |
| 29 | ITOM Startup-vs-running reboot-risk forecaster | impact-analyzer | itom_nccm_impact_analyzer | Hybrid |
| 30 | ITOM CVE↔firmware↔patch linkage | correlator | itom_nccm_correlator | Hybrid |
| 30 | ITOM Dedup duplicate CVE advisories | correlator | itom_nccm_correlator | Hybrid |
| 31 | ITOM Link Aruba advisories to device cohorts | correlator | itom_nccm_correlator | Hybrid |
| 31 | ITOM Link Aruba firmware pushes to alerts | correlator | itom_nccm_correlator | Hybrid |

**NCCM: 18 LLM · 14 Hybrid**

---

## RUM (18)

| Sr | Use-case | Capability | Agent | Class |
|----|----------|------------|-------|-------|
| 5 | ITOM Plain-English session narrative | narrator | itom_rum_narrator | LLM |
| 6 | ITOM Replay caption track | narrator | itom_rum_narrator | LLM |
| 7 | ITOM NL → RUM entity explorer filter | query-translator | itom_rum_query_translator | LLM |
| 8 | ITOM Weekly Core Web Vitals narrative | narrator | itom_rum_narrator | LLM |
| 11 | ITOM Browser-breakdown narration for a release | narrator | itom_rum_narrator | LLM |
| 12 | ITOM Geo heatmap narrative annotation | narrator | itom_rum_narrator | LLM |
| 13 | ITOM Release-impact diff on RUM KPIs | impact-analyzer | itom_rum_impact_analyzer | Hybrid |
| 13 | ITOM Pre-promotion staging-vs-prod UX delta | impact-analyzer | itom_rum_impact_analyzer | Hybrid |
| 16 | ITOM NL → cross-entity correlation query | query-translator | itom_rum_query_translator | LLM |
| 17 | ITOM Frustration-event plain-English description | narrator | itom_rum_narrator | LLM¹ |
| 18 | ITOM Stack-trace → English error explainer | narrator | itom_rum_narrator | LLM |
| 19 | ITOM Frontend-error session-context RCA walker | rca-analyst | itom_rum_rca_analyst | Hybrid |
| 19 | ITOM Differential cohort RCA for error regression | rca-analyst | itom_rum_rca_analyst | Hybrid |
| 23 | ITOM NL sketch → RUM dashboard JSON | query-translator | itom_rum_query_translator | LLM |
| 23 | ITOM NL → RUM alert-policy YAML | query-translator | itom_rum_query_translator | LLM |
| 25 | ITOM "What changed since last week" narration | narrator | itom_rum_narrator | LLM |
| 27 | ITOM Multi-dimensional RUM cohort grouping | correlator | itom_rum_correlator | Hybrid |
| 28 | ITOM Frontend-to-backend session linkage | correlator | itom_rum_correlator | Hybrid |

**RUM: 12 LLM · 6 Hybrid**

---

## REST — Infrastructure Monitoring (15)

| Sr | Use-case | Capability | Agent | Class |
|----|----------|------------|-------|-------|
| 19 | ITOM NL → tagging rule DSL | query-translator | itom_infra_query_translator | LLM |
| 20 | ITOM Plain-English discovery-failure summary | narrator | itom_infra_narrator | LLM |
| 30 | ITOM Runbook draft from past incident resolutions | remediator | itom_infra_remediator | Hybrid |
| 34 | ITOM "What this template watches" intro | narrator | itom_infra_narrator | LLM |
| 35 | ITOM Auto-link logs/flows/metrics/alerts card | correlator | itom_infra_correlator | Hybrid |
| 40 | ITOM Sample trap → parser selector + override | query-translator | itom_infra_query_translator | LLM |
| 41 | ITOM NL/OID → trap-to-event mapping | query-translator | itom_infra_query_translator | LLM |
| 42 | ITOM Sample trap → alert-policy YAML | query-translator | itom_infra_query_translator | LLM |
| 44 | ITOM Rolling narrative digest of trap stream | narrator | itom_infra_narrator | LLM |
| 47 | ITOM Narrated topology change summary | narrator | itom_infra_narrator | LLM |
| 51 | ITOM App-tier ↔ infra-signal correlation (topology) | correlator | itom_infra_correlator | Hybrid |
| 52 | ITOM On-hover link health blurb | narrator | itom_infra_narrator | LLM |
| 59 | ITOM NL → topology view definition JSON | query-translator | itom_infra_query_translator | LLM |
| 62 | ITOM NL → monitoring template YAML | query-translator | itom_infra_query_translator | LLM |
| 63 | ITOM NL → trap report/widget spec | query-translator | itom_infra_query_translator | LLM |

**Infra: 12 LLM · 3 Hybrid**

## REST — NetRoute (5)

| Sr | Use-case | Capability | Agent | Class |
|----|----------|------------|-------|-------|
| 2 | ITOM Narrate the path (hops, latency, ASN) | narrator | itom_netroute_narrator | LLM |
| 3 | ITOM Stitch device alerts ↔ perf-metric anomalies | correlator | itom_netroute_correlator | Hybrid |
| 5 | ITOM Narrative timeline of route changes | narrator | itom_netroute_narrator | LLM |
| 10 | ITOM NL → path/hop alert policy | query-translator | itom_netroute_query_translator | LLM |
| 14 | ITOM Narrated next-hop failover forecast | forecaster | itom_netroute_forecaster | Hybrid |

**NetRoute: 3 LLM · 2 Hybrid**

## REST — SLO (5)

| Sr | Use-case | Capability | Agent | Class |
|----|----------|------------|-------|-------|
| 10 | ITOM NL → SLO filter query | query-translator | itom_slo_query_translator | LLM |
| 11 | ITOM SLO burn-rate forecast w/ dependency attribution | forecaster | itom_slo_forecaster | Hybrid |
| 13 | ITOM Error-budget runway forecast + safe-deploy window | forecaster | itom_slo_forecaster | Hybrid |
| 17 | ITOM Audience-tailored cover for SLO report | narrator | itom_slo_narrator | LLM |
| 20 | ITOM NL → correction-window definition | query-translator | itom_slo_query_translator | LLM |

**SLO: 3 LLM · 2 Hybrid**

## REST — Alert Policy (9)

| Sr | Use-case | Capability | Agent | Class |
|----|----------|------------|-------|-------|
| 1 | ITOM NL → unified alert-policy YAML | query-translator | itom_alert_query_translator | LLM |
| 2 | ITOM Auto-group repeated threshold breaches | correlator | itom_alert_correlator | Hybrid |
| 5 | ITOM LLM-rewrite alert body | narrator | itom_alert_narrator | LLM |
| 9 | ITOM Conditional remediation branch w/ dry-run | remediator | itom_alert_remediator | Hybrid |
| 11 | ITOM NL → ITSM payload template | query-translator | itom_alert_query_translator | LLM |
| 14 | ITOM Cluster alerts + propose suppression rules | correlator | itom_alert_correlator | Hybrid |
| 16 | ITOM Post-mortem narrative from alert history | narrator | itom_alert_narrator | LLM |
| 17 | ITOM Collapse dependent-service alerts (topology) | correlator | itom_alert_correlator | Hybrid |
| 18 | ITOM NL → alert widget spec | query-translator | itom_alert_query_translator | LLM |

**Alert: 5 LLM · 4 Hybrid**

## REST — Dashboard / Report (10)

| Sr | Use-case | Capability | Agent | Class |
|----|----------|------------|-------|-------|
| 2 | ITOM NL/sketch → dashboard JSON | query-translator | itom_dashboard_query_translator | LLM |
| 5 | ITOM Auto-highlight co-occurring anomalies | correlator | itom_dashboard_correlator | Hybrid |
| 10 | ITOM Narrative executive cover for OOTB report | narrator | itom_dashboard_narrator | LLM |
| 11 | ITOM Narrated capacity-report generator | forecaster | itom_dashboard_forecaster | Hybrid |
| 12 | ITOM Narrated inventory delta vs prior period | narrator | itom_dashboard_narrator | LLM |
| 13 | ITOM NL → report template | query-translator | itom_dashboard_query_translator | LLM |
| 29 | ITOM KPI saturation walker w/ driver attribution | forecaster | itom_dashboard_forecaster | Hybrid |
| 30 | ITOM Plain-English trend narration | narrator | itom_dashboard_narrator | LLM |
| 31 | ITOM Narrative summary of audit activity | narrator | itom_dashboard_narrator | LLM |
| 32 | ITOM Regulator-ready narrative preface | narrator | itom_dashboard_narrator | LLM |

**Dashboard: 7 LLM · 3 Hybrid**

## REST — Observability Pipeline (6)

| Sr | Use-case | Capability | Agent | Class |
|----|----------|------------|-------|-------|
| 5 | ITOM Sample log → parser config | query-translator | itom_pipeline_query_translator | LLM |
| 6 | ITOM NL → field remap transform | query-translator | itom_pipeline_query_translator | LLM |
| 7 | ITOM NL → transformation policy | query-translator | itom_pipeline_query_translator | LLM |
| 9 | ITOM Pipeline health narrative for operator shift | narrator | itom_pipeline_narrator | LLM |
| 10 | ITOM NL → full pipeline DAG definition | query-translator | itom_pipeline_query_translator | LLM |
| 11 | ITOM NL → indexing/retention policy | query-translator | itom_pipeline_query_translator | LLM |

**Pipeline: 6 LLM · 0 Hybrid**

## REST — Flow Monitoring (6)

| Sr | Use-case | Capability | Agent | Class |
|----|----------|------------|-------|-------|
| 7 | ITOM NL/sample → flow field mapping | query-translator | itom_flow_query_translator | LLM |
| 10 | ITOM "What's happening on the wire" blurb | narrator | itom_flow_narrator | LLM |
| 11 | ITOM NL → flow analytics query | query-translator | itom_flow_query_translator | LLM |
| 12 | ITOM Flow ingestion health narrative | narrator | itom_flow_narrator | LLM |
| 13 | ITOM Threat-flow RCA walker | rca-analyst | itom_flow_rca_analyst | Hybrid |
| 15 | ITOM Top-talkers narrative annotation | narrator | itom_flow_narrator | LLM |

**Flow: 5 LLM · 1 Hybrid**

## REST — Platform Features (2)

| Sr | Use-case | Capability | Agent | Class |
|----|----------|------------|-------|-------|
| 12 | ITOM Platform-health narrative for admin page | narrator | itom_platform_narrator | LLM |
| 18 | ITOM LLM-narrated widget caption from stats | narrator | itom_platform_narrator | LLM |

**Platform: 2 LLM · 0 Hybrid**

## REST — General Features (10)

| Sr | Use-case | Capability | Agent | Class |
|----|----------|------------|-------|-------|
| 5 | ITOM Daily/weekly self-health narrative | narrator | itom_general_narrator | LLM |
| 6 | ITOM Self-pre-packaged RCA diagnostic | rca-analyst | itom_general_rca_analyst | Hybrid |
| 15 | ITOM Narrative fleet status report after bulk ops | narrator | itom_general_narrator | LLM |
| 17 | ITOM NL → tagging rule w/ boolean predicates | query-translator | itom_general_query_translator | LLM |
| 21 | ITOM Change↔alert blast-radius bundle (dependency) | correlator | itom_general_correlator | Hybrid |
| 23 | ITOM Suggest runbook modification diff | remediator | itom_general_remediator | Hybrid |
| 25 | ITOM Narrative shift-end summary of user activity | narrator | itom_general_narrator | LLM |
| 26 | ITOM Plain-English explanation of an audit entry | narrator | itom_general_narrator | LLM |
| 29 | ITOM Narrative release/version posture summary | narrator | itom_general_narrator | LLM |
| 31 | ITOM NL → starter config bundle | query-translator | itom_general_query_translator | LLM |

**General: 7 LLM · 3 Hybrid**

## REST — AI Features (10) — all extend existing detection/forecast engines

| Sr | Use-case | Capability | Agent | Class |
|----|----------|------------|-------|-------|
| 1 | ITOM Narrate why a dynamic threshold moved | narrator | itom_ai_narrator | LLM¹ |
| 2 | ITOM Weekly baseline-drift narrative | narrator | itom_ai_narrator | LLM¹ |
| 3 | ITOM Plain-English anomaly annotation on widget | narrator | itom_ai_narrator | LLM¹ |
| 4 | ITOM Scoped auto-response binding per anomaly class | remediator | itom_ai_remediator | Hybrid |
| 5 | ITOM Narrate anomaly with seasonal context | narrator | itom_ai_narrator | LLM¹ |
| 6 | ITOM Forecast-alert storm-risk pre-compute | forecaster | itom_ai_forecaster | Hybrid |
| 7 | ITOM Narrate forecast widget (trajectory, CI, ETA) | narrator | itom_ai_narrator | LLM¹ |
| 8 | ITOM Executive narrative cover for forecast report | narrator | itom_ai_narrator | LLM¹ |
| 9 | ITOM Narrated saturation timeline by time-to-exhaustion | forecaster | itom_ai_forecaster | Hybrid |
| 10 | ITOM LLM-rewrite AI/ML insight bullets | narrator | itom_ai_narrator | LLM¹ |

**AI Features: 7 LLM · 3 Hybrid**

---

## Roll-up

| Domain | Total | LLM | Hybrid | ML-only |
|--------|-------|-----|--------|---------|
| APM | 25 | 16 | 9 | 0 |
| Logs | 18 | 14 | 4 | 0 |
| Metric | 10 | 8 | 2 | 0 |
| NCCM | 32 | 18 | 14 | 0 |
| RUM | 18 | 12 | 6 | 0 |
| REST — Infra | 15 | 12 | 3 | 0 |
| REST — NetRoute | 5 | 3 | 2 | 0 |
| REST — SLO | 5 | 3 | 2 | 0 |
| REST — Alert | 9 | 5 | 4 | 0 |
| REST — Dashboard | 10 | 7 | 3 | 0 |
| REST — Pipeline | 6 | 6 | 0 | 0 |
| REST — Flow | 6 | 5 | 1 | 0 |
| REST — Platform | 2 | 2 | 0 | 0 |
| REST — General | 10 | 7 | 3 | 0 |
| REST — AI Features | 10 | 7 | 3 | 0 |
| **Total** | **181** | **125** | **56** | **0** |

**125/181 (69%) pure-LLM · 56/181 (31%) Hybrid (ML+LLM) · 0 ML-only.**
