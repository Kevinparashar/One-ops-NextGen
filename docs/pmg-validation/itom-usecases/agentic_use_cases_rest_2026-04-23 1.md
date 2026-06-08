# ObserveOps Agentic AI Use-Case Catalog

*Scope: rest (Infra, NetRoute, SLO, Alert, Dashboard, Pipeline, Flow, Platform, General, AI) · Generated: 2026-04-23 · 78 use-cases across 78 features*

## Top 10 cross-domain plays

*Use-cases that chain ≥2 capabilities or implicitly span ≥2 CSVs. Ranked by Impact then Effort.*


| Rank | CSV       | Sr No | Use-case                                                     | Agent(s)                              | Why it's cross-domain                                                                        | Impact | Effort |
| ---- | --------- | ----- | ------------------------------------------------------------ | ------------------------------------- | -------------------------------------------------------------------------------------------- | ------ | ------ |
| 1    | Dashboard | 29    | Agentic "KPI saturation walker" w/ driver attribution        | forecaster + rca-analyst              | Works on any KPI (app/network/log/business); chains forecast → leading-indicator attribution | H      | H      |
| 2    | NetRoute  | 14    | Narrated next-hop failover forecast                          | forecaster + impact-analyzer          | NetRoute + BGP + flows + QoS; ranks candidate paths by breach probability                    | H      | H      |
| 3    | General   | 21    | Dependency-mapper blast-radius bundle (change → alerts)      | correlator + rca-analyst              | NCCM/deploy + alerts + topology; single bundle scoped to dependency edges                    | H      | H      |
| 4    | Infra     | 51    | App-tier-event ↔ infra-signal correlation via topology edges | correlator + rca-analyst              | Infra + APM + SLO; "this infra alert affects apps X,Y,Z"                                     | H      | H      |
| 5    | Pipeline  | 10    | NL → full pipeline DAG (source→transforms→sink)              | query-translator                      | Composes across parsers, transforms, indexing policies in one go                             | H      | H      |
| 6    | AI        | 4     | Anomaly-to-action auto-response binding w/ cost + rollback   | remediator + forecaster               | Extends AI_Features anomaly detection with scoped remediation bindings                       | H      | H      |
| 7    | Infra     | 35    | Unified investigation card (logs + flows + metrics + alerts) | correlator + narrator                 | Cross-signal bundle pinned to entity with join-key rationale                                 | H      | M      |
| 8    | Alert     | 17    | Topology-aware alert dedup (parent + suppressed children)    | correlator + rca-analyst              | Alerts + topology + availability correlation                                                 | H      | M      |
| 9    | SLO       | 11    | Agentic SLO burn-rate forecast w/ dependency attribution     | forecaster + rca-analyst              | SLO + APM service map; names top-2 contributors + one action                                 | H      | M      |
| 10   | Flow      | 13    | Threat-flow RCA walker                                       | rca-analyst + correlator + remediator | Flow + threat intel + topology + NCM + auth logs; proposes block                             | H      | M      |


## By domain

### Infrastructure Monitoring


| Sr No | Feature (excerpt, ≤80 chars)                                    | Use-case                                                                   | Agent            | Trigger                                                                                   | Output                                                                                      | Impact | Effort |
| ----- | --------------------------------------------------------------- | -------------------------------------------------------------------------- | ---------------- | ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- | ------ | ------ |
| 19    | Dynamic rule-based tagging during discovery                     | NL to tagging rule DSL                                                     | query-translator | User types "tag all hosts in 10.20.0.0/16 running nginx as web-tier"                      | Tagging rule (key:value + boolean predicate) with dry-run match count                       | H      | M      |
| 20    | Discovery results with failure reasons                          | Plain-English discovery failure summary per device/subnet batch            | narrator         | Discovery job completes with failures                                                     | 3-5 bullet narrative: what failed, common reason clusters, suggested next step              | H      | L      |
| 30    | OOTB runbooks + manual/auto execution from monitoring templates | LLM-synthesized runbook draft from past incident resolutions               | remediator       | New alert signature lacking mapped runbook fires                                          | Draft runbook with parameterized steps + 3 prior similar incidents, human approval required | H      | M      |
| 34    | OOTB monitoring templates per technology                        | Narrated "what this template watches and why" intro per template           | narrator         | User opens template in library                                                            | 2-3 sentence template description + KPI rationale                                           | M      | L      |
| 35    | Cross-domain context in monitoring templates                    | Auto-link logs/flows/metrics/alerts into one investigation card per entity | correlator       | Alert fires or user opens entity view                                                     | Unified bundle pinned to entity with join-key rationale (host+time+service)                 | H      | M      |
| 40    | Prebuilt SNMP Trap parser library                               | Sample trap → parser selector + override                                   | query-translator | User pastes raw trap (OID + varbinds)                                                     | Matched library parser ID or generated MIB/regex parser stub validated on sample            | H      | M      |
| 41    | Custom trap translation engine                                  | NL/OID sample → trap-to-event mapping                                      | query-translator | User provides OID + NL "map ciscoLinkDown to Interface Down critical"                     | Translation rule (OID match + varbind extract + event naming) tested on sample              | H      | M      |
| 42    | Trap to Policy Conversion                                       | Sample trap → alert policy YAML                                            | query-translator | User selects trap(s) and says "alert on repeat >3 in 5m"                                  | Alert policy YAML with match predicate, threshold, severity, dedup key                      | H      | M      |
| 44    | Live SNMP trap viewer                                           | Rolling narrative digest of last-N-minutes trap stream                     | narrator         | Operator opens trap viewer or scheduled tick                                              | 1-paragraph summary: dominant trap types, severity mix, notable sources                     | M      | M      |
| 47    | Automated topology generation                                   | Narrated topology change summary between two snapshots                     | narrator         | Topology auto-refresh completes                                                           | "Since yesterday: 3 new ESXi hosts joined cluster-A, 2 links removed in DC-East"            | H      | M      |
| 51    | App-to-infra topology discovery                                 | Correlate app-tier events with underlying infra signals via topology edges | correlator       | Infra alert on host/VM/container                                                          | Bundle: "this infra alert affects apps X,Y,Z" with topology path shown                      | H      | H      |
| 52    | Topology link hover metrics                                     | On-hover plain-English link health blurb                                   | narrator         | User hovers a link on canvas                                                              | "Gig0/1 healthy, 62% util, 0 errors, trending flat 24h"                                     | M      | L      |
| 59    | Custom topology view creation                                   | NL → topology view definition JSON                                         | query-translator | User types "show all core routers + their L2 neighbors in us-east tagged prod"            | Topology view JSON (filter predicates, depth) with preview node/edge count                  | M      | M      |
| 62    | Customize OOTB + new monitoring templates                       | NL → monitoring template YAML                                              | query-translator | User describes "Oracle RAC KPIs: ASM space, cluster interconnect latency, node evictions" | Template YAML with metric definitions, intervals, default thresholds                        | H      | H      |
| 63    | Custom Trap Reports and Widgets                                 | NL → trap report/widget spec                                               | query-translator | User types "top 10 trap sources by severity last 24h grouped by device class"             | Widget JSON (query + viz type + grouping) with preview rendered                             | M      | L      |


### NetRoute


| Sr No | Feature (excerpt, ≤80 chars)                    | Use-case                                                                     | Agent            | Trigger                                                             | Output                                                                                                | Impact | Effort |
| ----- | ----------------------------------------------- | ---------------------------------------------------------------------------- | ---------------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ------ | ------ |
| 2     | Hop-by-hop path visualization                   | Narrate the path: hops, latency hotspots, ASN transitions                    | narrator         | User opens path view for a source-dest pair                         | Paragraph describing each hop's role and any standout latency/loss                                    | H      | M      |
| 3     | Dynamic entity resolution for metrics+alerts    | Stitch device alerts to contemporaneous perf-metric anomalies on same entity | correlator       | New device alert arrival                                            | Linked card: alert + correlated metric dips, keyed on device ID + window                              | M      | L      |
| 5     | Historical route timeline                       | Narrative timeline of route changes in a window                              | narrator         | User scrubs timeline or picks range                                 | "At 14:02 path shifted from ISP-A to ISP-B, +40ms; reverted 14:38"                                    | H      | M      |
| 10    | Customizable alerting policies for network path | NL → path/hop alert policy                                                   | query-translator | User types "alert when hop 3 latency >100ms for 5m on route NY-LON" | NetRoute alert policy YAML with hop selector, threshold, duration                                     | H      | M      |
| 14    | Transit likelihood analysis for route paths     | Narrated next-hop failover forecast                                          | forecaster       | BGP flap, latency drift on primary path, or 15-min schedule         | Ranked path list + "Path B has 78% chance of carrying overflow in next 30m — pre-stage QoS on Path C" | H      | H      |


### SLO


| Sr No | Feature (excerpt, ≤80 chars)                 | Use-case                                                                     | Agent            | Trigger                                                                      | Output                                                                               | Impact | Effort |
| ----- | -------------------------------------------- | ---------------------------------------------------------------------------- | ---------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ------ | ------ |
| 10    | Single pane SLO view with filters            | NL → SLO filter query                                                        | query-translator | User types "show at-risk SLOs for payment services in last 7d tagged tier-1" | Filter query spec (service, status, time, tag) applied to SLO list with result count | M      | L      |
| 11    | SLO trends and error-budget burn-down        | Agentic SLO burn-rate forecast w/ dependency attribution                     | forecaster       | Burn rate > 1x sustained 10m, or nightly budget review                       | Projected exhaustion + top-2 contributing dependencies + one recommended action      | H      | M      |
| 13    | Real-time & historical SLO/error-budget KPIs | Narrated error-budget runway forecast w/ "safe deploy window" recommendation | forecaster       | Pre-deploy check or daily planning digest                                    | Per-service runway + recommended deploy windows with rationale                       | M      | M      |
| 17    | Exportable SLO compliance reports            | Audience-tailored narrative cover page for exported SLO report               | narrator         | User clicks Export (PDF/Excel)                                               | Exec-vs-eng framed 1-page summary of SLO posture, burn, breaches                     | H      | L      |
| 20    | Correction window exclusions                 | NL → correction window definition                                            | query-translator | User types "exclude Saturdays 2-4am maintenance from checkout SLO"           | Correction window YAML (recurrence RRULE + SLO binding) with preview                 | M      | L      |


### Alert Policy


| Sr No | Feature (excerpt, ≤80 chars)                                       | Use-case                                                                         | Agent            | Trigger                                                                             | Output                                                                             | Impact | Effort |
| ----- | ------------------------------------------------------------------ | -------------------------------------------------------------------------------- | ---------------- | ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | ------ | ------ |
| 1     | Dynamic policy creation across signal types                        | NL → unified alert policy YAML                                                   | query-translator | User types "alert when checkout-svc 5xx rate >1% for 5m on any pod"                 | Alert policy YAML with correct signal-type block + validation against inventory    | H      | M      |
| 2     | Multi-level thresholds with occurrence suppression                 | Auto-group repeated threshold breaches across windows into one escalating bundle | correlator       | Nth occurrence within evaluation window                                             | Single evolving alert bundle with occurrence count and "why grouped" trail         | H      | M      |
| 5     | Macro-driven dynamic alert messages                                | LLM-rewrite alert body into human-readable notification                          | narrator         | Alert fires and template renders                                                    | Personalized 2-3 sentence message with entity, metric, threshold context           | H      | L      |
| 9     | Auto-remediation via runbooks with severity + context conditionals | Conditional remediation branch (sev/context-gated) with dry-run preview          | remediator       | Alert fires matching remediation-eligible policy with recurring signature           | Proposed conditional branch + dry-run output + blast-radius cap, approval required | H      | M      |
| 11    | Deep ITSM integration payload customization                        | NL → ITSM payload template                                                       | query-translator | User types "P1 DB incidents → DBA team, CI=database, impact=high"                   | ITSM payload JSON/Jinja template validated against connected ITSM schema           | H      | M      |
| 14    | Auto-classify and suppress alerts                                  | Cluster alerts + propose suppression rules with recall-risk estimate             | correlator       | Alert volume spike or scheduled sweep                                               | Clusters with class label, rationale, and suppression-rule preview user can audit  | H      | M      |
| 16    | Comprehensive alert history for post-mortem                        | Post-mortem narrative from alert history of an incident window                   | narrator         | User opens post-mortem / incident closes                                            | Timeline story: flap episodes, eval cycles, what escalated when                    | H      | M      |
| 17    | Availability correlation, upstream/downstream dedup                | Collapse dependent-service alerts into single root-entity bundle using topology  | correlator       | Alert storm on related services                                                     | One parent alert + suppressed children with dependency-path rationale              | H      | M      |
| 18    | Custom alert widgets and streams                                   | NL → alert widget spec                                                           | query-translator | User types "live stream of P1/P2 alerts for prod tagged payments, group by service" | Alert widget JSON (filter + grouping + refresh cadence) with live preview          | M      | L      |


### Dashboard / Report


| Sr No | Feature (excerpt, ≤80 chars)                     | Use-case                                                                          | Agent            | Trigger                                                                                               | Output                                                                                                                            | Impact | Effort |
| ----- | ------------------------------------------------ | --------------------------------------------------------------------------------- | ---------------- | ----------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ------ | ------ |
| 2     | Custom dashboards via no-code interface          | NL/sketch → dashboard JSON                                                        | query-translator | User types "dashboard for checkout-svc: RED metrics, error logs, top traces, dependency health"       | Dashboard JSON with 4-6 widgets (queries + viz types + layout) with preview                                                       | H      | M      |
| 5     | Multi-telemetry single-chart correlation         | Auto-highlight temporally co-occurring anomalies across metrics/logs/flows/alerts | correlator       | User opens chart or anomaly window detected                                                           | Chart overlay with linked anomaly markers and "these co-occurred because…" note                                                   | M      | M      |
| 10    | OOTB reports across infra/app/network/logs       | Narrative executive cover for any OOTB report                                     | narrator         | Report generation                                                                                     | 5-bullet "what this report says" summary prepended to PDF                                                                         | M      | L      |
| 11    | OOTB forecasting & capacity reports              | Narrated capacity-report generator w/ top growth drivers                          | forecaster       | Weekly/monthly report generation                                                                      | Auto-written exec summary: "Storage will saturate cluster-A in ~38 days; 71% of growth is service-orders log volume"              | M      | L      |
| 12    | OOTB inventory reports                           | Narrated inventory delta vs prior period                                          | narrator         | Scheduled inventory report runs                                                                       | "Fleet grew by 42 hosts; 7 retired; Linux share up 3pp" paragraph                                                                 | M      | L      |
| 13    | Custom reports via UI report builder             | NL → report template                                                              | query-translator | User types "weekly availability report for tier-1 services grouped by region with SLO breach summary" | Report template JSON (KPIs, viz, filters, schedule) with dry-run on last period                                                   | H      | M      |
| 29    | Dynamic forecast for any KPI (app/net/log/biz)   | Agentic "KPI saturation walker" w/ driver attribution                             | forecaster       | User pins a KPI or anomaly detector flags drift                                                       | Forecast horizon + plain-English driver attribution ("queue depth exceeds 10k in ~6h, led by upstream ingest since 14:00 deploy") | H      | H      |
| 30    | OOTB trend analysis reports                      | Plain-English trend narration over min/max/avg aggregates                         | narrator         | Trend report opens or exports                                                                         | 1-paragraph per KPI: direction, magnitude, notable spikes                                                                         | H      | L      |
| 31    | Custom audit reporting                           | Narrative summary of audit activity for a period                                  | narrator         | Compliance officer opens audit view                                                                   | "This week: 14 config changes, 3 privileged ops, top actor X" digest                                                              | M      | L      |
| 32    | Integrated compliance reporting (GDPR/HIPAA/PCI) | Regulator-ready narrative preface per compliance framework                        | narrator         | User exports compliance report                                                                        | Framework-specific plain-English posture statement + evidence pointers                                                            | H      | M      |


### Observability Pipeline


| Sr No | Feature (excerpt, ≤80 chars)             | Use-case                                     | Agent            | Trigger                                                                                            | Output                                                                                          | Impact | Effort |
| ----- | ---------------------------------------- | -------------------------------------------- | ---------------- | -------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- | ------ | ------ |
| 5     | OOTB parsers + custom parsing logic      | Sample log → parser config                   | query-translator | User pastes 1-3 raw log lines                                                                      | Declarative parser config (grok/regex/JSON path) with named fields + per-sample extraction test | H      | M      |
| 6     | Field remapping with schema versioning   | NL → field remap transform                   | query-translator | User types "rename src_ip to source.ip, cast bytes to int, merge first+last into full_name"        | Remap transform config with type casts + new schema version diff vs prior                       | M      | M      |
| 7     | Policy-driven transformations            | NL → transformation policy                   | query-translator | User types "drop debug logs, geo-enrich client_ip, normalize severity to ECS, convert bytes to KB" | Pipeline policy YAML (ordered transforms) with before/after sample preview                      | H      | M      |
| 9     | Pipeline self-metrics per stage          | Pipeline health narrative for operator shift | narrator         | On pipeline dashboard open or shift boundary                                                       | "Stage-3 lag rising 20min, queue depth 2x norm; drop count flat" summary                        | H      | L      |
| 10    | Visual pipeline builder + policy catalog | NL → full pipeline definition                | query-translator | User types "ingest firewall syslog, parse, drop health checks, geo-enrich, index to security tier" | Pipeline DAG JSON (source→transforms→sink) importable into visual builder                       | H      | H      |
| 11    | Policy-based indexing with retention     | NL → indexing/retention policy               | query-translator | User types "audit logs to cold tier 400d, app logs hot 30d then warm 90d"                          | Indexing policy YAML (data class predicates + tier + retention) validated against tier catalog  | M      | L      |


### Flow Monitoring


| Sr No | Feature (excerpt, ≤80 chars)                              | Use-case                                                | Agent            | Trigger                                                                                     | Output                                                                                                               | Impact | Effort |
| ----- | --------------------------------------------------------- | ------------------------------------------------------- | ---------------- | ------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | ------ | ------ |
| 7     | Custom field mapping for flow enrichment                  | NL/sample → flow field mapping                          | query-translator | User types "map AS from IP, tag internal subnets 10.0.0.0/8 as corp, extract app from port" | Flow field mapping config with defaults + custom rules, validated on flow sample                                     | M      | M      |
| 10    | OOTB flow analytics dashboards                            | Narrated "what's happening on the wire right now" blurb | narrator         | User opens flow dashboard                                                                   | 3-sentence summary of traffic mix, notable talkers, anomalies                                                        | M      | L      |
| 11    | Dynamic analytics views for flow                          | NL → flow analytics query                               | query-translator | User types "top talkers to s3 by bytes last 1h excluding internal"                          | Flow query DSL with filters, group-by, aggregation + suggested viz                                                   | H      | M      |
| 12    | Flow statistics dashboard                                 | Flow ingestion health narrative                         | narrator         | Weekly schedule or ingestion anomaly detected                                               | Short narrative: volume trends, noisy sources, config suggestions                                                    | M      | L      |
| 13    | Native malicious IP feed integration for threat detection | Threat-flow RCA walker                                  | rca-analyst      | Flow record matches threat-intel feed                                                       | Ranked hypotheses: initiator, lateral-movement chain, recent firewall/NCM rule changes, correlated auth/process logs | H      | M      |
| 15    | Embedded top-N flow analytics                             | Top-talkers narrative annotation on template widgets    | narrator         | Template renders with flow widget                                                           | "Host X is 38% of egress, up from 12% yesterday" auto-caption                                                        | M      | L      |


### Platform Features


| Sr No | Feature (excerpt, ≤80 chars)              | Use-case                                                             | Agent    | Trigger                           | Output                                                             | Impact | Effort |
| ----- | ----------------------------------------- | -------------------------------------------------------------------- | -------- | --------------------------------- | ------------------------------------------------------------------ | ------ | ------ |
| 12    | Platform health + troubleshooting tools   | Platform-health narrative for admin landing page                     | narrator | Admin opens platform health view  | Paragraph: component statuses, any degradation, what to check next | H      | L      |
| 18    | Auto advanced analytics summary in widget | LLM-narrated widget caption from distribution/percentile/trend stats | narrator | Widget renders with stats payload | 2-3 sentence natural-language reading of the stats                 | H      | L      |


### General Features


| Sr No | Feature (excerpt, ≤80 chars)                            | Use-case                                                                      | Agent            | Trigger                                                                               | Output                                                                                                                                                                   | Impact | Effort |
| ----- | ------------------------------------------------------- | ----------------------------------------------------------------------------- | ---------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------ | ------ |
| 5     | Self-monitoring dashboard                               | Daily/weekly self-health narrative for platform owner                         | narrator         | Scheduled digest or dashboard open                                                    | Health digest: top risks, capacity pressure, recent incidents                                                                                                            | M      | L      |
| 6     | One-click diagnostic data collection                    | Self-pre-packaged RCA diagnostic that reasons before it ships                 | rca-analyst      | User clicks "collect diagnostics" on a failing component                              | Ranked probable-cause hypotheses computed locally over the bundle, with cited log lines + metric anomalies + change events; bundle forwarded with hypotheses as pre-read | H      | M      |
| 15    | Agent Fleet Management                                  | Narrative fleet status report after bulk ops                                  | narrator         | Bulk start/stop/config push completes                                                 | "142/150 agents restarted OK; 8 failed — 6 timeout, 2 auth" summary                                                                                                      | M      | L      |
| 17    | Dynamic rule-based tagging with AND/OR                  | NL → tagging rule with boolean predicates                                     | query-translator | User types "tag env:prod where (region=us-east OR us-west) AND tier=1 AND NOT canary" | Tagging rule expression tree with dry-run match count on current inventory                                                                                               | H      | M      |
| 21    | Dynamic dependency mapper with manual overrides         | Link change events and alerts along dependency edges into blast-radius bundle | correlator       | New alert or recent NCM/deploy change                                                 | Topology-scoped bundle showing change → affected nodes → triggered alerts, with confidence                                                                               | H      | H      |
| 23    | Dynamic runbook engine — UI-based create/modify/execute | Suggest runbook modification diff when a step repeatedly fails                | remediator       | Telemetry shows runbook step failing >N times or operators manually overriding        | PR-style diff against existing runbook YAML + 3 past operator override transcripts as evidence                                                                           | M      | M      |
| 25    | Live user session tracking                              | Narrative shift-end summary of user activity                                  | narrator         | End of admin shift or on demand                                                       | "This hour: 23 active users, heavy use of Alert Policy module" paragraph                                                                                                 | L      | L      |
| 26    | Detailed system audit logs                              | Plain-English explanation of a single audit entry                             | narrator         | User clicks an audit row                                                              | 1-2 sentence humanized description of the action + actor + target                                                                                                        | M      | L      |
| 29    | Deployment artifact visibility                          | Narrative release/version posture summary                                     | narrator         | User opens deployment view or release completes                                       | "App servers on v4.2 (100%); collectors mixed (70% v3.1, 30% v3.0)" blurb                                                                                                | M      | L      |
| 31    | Quick start-up guide + wizard                           | NL → starter config bundle                                                    | query-translator | User types "I'm monitoring a Kubernetes payments stack, get me started"               | Bundle: starter dashboards + alert policies + SLO stubs + pipeline template, all importable                                                                              | M      | M      |


### AI Features

*All AI Features use-cases EXTEND the existing detection/forecasting engines with LLM-narrated reasoning or agentic follow-ups — not re-propose them.*


| Sr No | Feature (excerpt, ≤80 chars)                  | Use-case                                                                          | Agent      | Trigger                                                         | Output                                                                                                                                            | Impact | Effort |
| ----- | --------------------------------------------- | --------------------------------------------------------------------------------- | ---------- | --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------ | ------ |
| 1     | Dynamic thresholding                          | Narrate why a dynamic threshold moved and what it means                           | narrator   | Threshold recalibration event                                   | "Threshold for CPU-host-X lifted from 70 to 82 after 2wk learning" caption                                                                        | M      | L      |
| 2     | Continuously adapting baselines               | Weekly baseline-drift narrative per critical entity                               | narrator   | Scheduled baseline review                                       | "Baseline for API latency drifted up 15% this week, matches new release"                                                                          | M      | L      |
| 3     | Anomaly detection in widgets                  | Plain-English anomaly annotation on dashboard widget                              | narrator   | Anomaly marker rendered on a chart                              | "Spike at 14:22 is 4σ above baseline, lasted 6min, isolated to pod-7"                                                                             | H      | L      |
| 4     | Anomaly policies enabling automated responses | Scoped auto-response binding per anomaly class w/ cost + rollback                 | remediator | Recurring anomaly class detected with stable resolution pattern | Proposed anomaly-to-action binding with cost estimate, auto-rollback trigger, expiry window; human approval required                              | H      | H      |
| 5     | Seasonal-aware anomaly detection              | Narrate anomaly with seasonal context                                             | narrator   | Anomaly fires on seasonal metric                                | "Deviation flagged despite Monday-morning peak pattern; true outlier"                                                                             | H      | L      |
| 6     | Advanced forecast-based alerting policies     | Forecast-alert storm-risk pre-compute w/ suppression/maintenance overlays         | forecaster | Rolling hourly evaluation of forecast + change calendar         | Risk timeline: "18:00-20:00 has 70% storm probability — deploy X overlaps seasonal spike; suggest suppressing noisy group N and paging on-call Y" | H      | M      |
| 7     | Forecasted metrics with confidence intervals  | Narrate forecast widget: trajectory, CI, breach ETA                               | narrator   | Forecast widget renders                                         | "Disk will hit 90% in ~6 days (CI 4-9d); order capacity now" caption                                                                              | H      | L      |
| 8     | Automated forecast reports                    | Executive narrative cover for forecast report                                     | narrator   | Scheduled forecast report generation                            | 1-page prose: top 5 metrics at risk, timelines, recommended scope                                                                                 | H      | L      |
| 9     | Capacity planning w/ predictive capabilities  | Narrated saturation timeline ranked by time-to-exhaustion w/ workload attribution | forecaster | Daily capacity review or threshold-crossing on any resource     | Ranked list: "disk /var on host-12 hits 90% in ~4d driven by svc-auth log volume 3x since Tue" + suggested action                                 | H      | M      |
| 10    | Export reports enriched with AI insights      | LLM-rewrite AI/ML insight bullets into audience-appropriate prose                 | narrator   | User exports dashboard/report                                   | Narrative insights block embedded in PDF/Excel cover                                                                                              | H      | L      |


## By capability

### narrator

- **Infra 20** — Plain-English discovery failure summary per device/subnet batch
- **Infra 34** — Narrated "what this template watches and why" intro per template
- **Infra 44** — Rolling narrative digest of last-N-minutes trap stream
- **Infra 47** — Narrated topology change summary between two snapshots
- **Infra 52** — On-hover plain-English link health blurb
- **NetRoute 2** — Narrate the path: hops, latency hotspots, ASN transitions
- **NetRoute 5** — Narrative timeline of route changes in a window
- **SLO 17** — Audience-tailored narrative cover page for exported SLO report
- **Alert 5** — LLM-rewrite alert body into human-readable notification
- **Alert 16** — Post-mortem narrative from alert history of an incident window
- **Dashboard 10** — Narrative executive cover for any OOTB report
- **Dashboard 12** — Narrated inventory delta vs prior period
- **Dashboard 30** — Plain-English trend narration over min/max/avg aggregates
- **Dashboard 31** — Narrative summary of audit activity for a period
- **Dashboard 32** — Regulator-ready narrative preface per compliance framework
- **Pipeline 9** — Pipeline health narrative for operator shift
- **Flow 10** — Narrated "what's happening on the wire right now" blurb
- **Flow 12** — Flow ingestion health narrative
- **Flow 15** — Top-talkers narrative annotation on template widgets
- **Platform 12** — Platform-health narrative for admin landing page
- **Platform 18** — LLM-narrated widget caption from distribution/percentile/trend stats
- **General 5** — Daily/weekly self-health narrative for platform owner
- **General 15** — Narrative fleet status report after bulk ops
- **General 25** — Narrative shift-end summary of user activity
- **General 26** — Plain-English explanation of a single audit entry
- **General 29** — Narrative release/version posture summary
- **AI 1** — Narrate why a dynamic threshold moved and what it means
- **AI 2** — Weekly baseline-drift narrative per critical entity
- **AI 3** — Plain-English anomaly annotation on dashboard widget
- **AI 5** — Narrate anomaly with seasonal context
- **AI 7** — Narrate forecast widget: trajectory, CI, breach ETA
- **AI 8** — Executive narrative cover for forecast report
- **AI 10** — LLM-rewrite AI/ML insight bullets into audience-appropriate prose

### query-translator

- **Infra 19** — NL to tagging rule DSL
- **Infra 40** — Sample trap → parser selector + override
- **Infra 41** — NL/OID sample → trap-to-event mapping
- **Infra 42** — Sample trap → alert policy YAML
- **Infra 59** — NL → topology view definition JSON
- **Infra 62** — NL → monitoring template YAML
- **Infra 63** — NL → trap report/widget spec
- **NetRoute 10** — NL → path/hop alert policy
- **SLO 10** — NL → SLO filter query
- **SLO 20** — NL → correction window definition
- **Alert 1** — NL → unified alert policy YAML
- **Alert 11** — NL → ITSM payload template
- **Alert 18** — NL → alert widget spec
- **Dashboard 2** — NL/sketch → dashboard JSON
- **Dashboard 13** — NL → report template
- **Pipeline 5** — Sample log → parser config
- **Pipeline 6** — NL → field remap transform
- **Pipeline 7** — NL → transformation policy
- **Pipeline 10** — NL → full pipeline definition
- **Pipeline 11** — NL → indexing/retention policy
- **Flow 7** — NL/sample → flow field mapping
- **Flow 11** — NL → flow analytics query
- **General 17** — NL → tagging rule with boolean predicates
- **General 31** — NL → starter config bundle

### correlator

- **Infra 35** — Auto-link logs/flows/metrics/alerts into one investigation card per entity
- **Infra 51** — Correlate app-tier events with underlying infra signals via topology edges
- **NetRoute 3** — Stitch device alerts to contemporaneous perf-metric anomalies
- **Alert 2** — Auto-group repeated threshold breaches across windows into one escalating bundle
- **Alert 14** — Cluster alerts + propose suppression rules with recall-risk estimate
- **Alert 17** — Collapse dependent-service alerts into single root-entity bundle using topology
- **Dashboard 5** — Auto-highlight temporally co-occurring anomalies across metrics/logs/flows/alerts
- **General 21** — Link change events and alerts along dependency edges into blast-radius bundle

### forecaster

- **NetRoute 14** — Narrated next-hop failover forecast
- **SLO 11** — Agentic SLO burn-rate forecast w/ dependency attribution
- **SLO 13** — Narrated error-budget runway forecast w/ "safe deploy window"
- **Dashboard 11** — Narrated capacity-report generator w/ top growth drivers
- **Dashboard 29** — Agentic "KPI saturation walker" w/ driver attribution
- **AI 6** — Forecast-alert storm-risk pre-compute w/ suppression/maintenance overlays
- **AI 9** — Narrated saturation timeline ranked by time-to-exhaustion w/ workload attribution

### remediator

- **Infra 30** — LLM-synthesized runbook draft from past incident resolutions
- **Alert 9** — Conditional remediation branch (sev/context-gated) with dry-run preview
- **General 23** — Suggest runbook modification diff when a step repeatedly fails
- **AI 4** — Scoped auto-response binding per anomaly class w/ cost + rollback

### rca-analyst

- **Flow 13** — Threat-flow RCA walker
- **General 6** — Self-pre-packaged RCA diagnostic that reasons before it ships

## Skipped


| CSV                                   | augmentable | automatable | not-applicable |
| ------------------------------------- | ----------- | ----------- | -------------- |
| FSO_RFP_Infrastructure_Monitoring.csv | 15          | 24          | 24             |
| FSO_RFP_NetRoute.csv                  | 5           | 4           | 5              |
| FSO_RFP_SLO.csv                       | 5           | 14          | 5              |
| FSO_RFP_Alert_Policy.csv              | 9           | 8           | 1              |
| FSO_RFP_Dashboard_Report.csv          | 10          | 11          | 12             |
| FSO_RFP_Observability_Pipeline.csv    | 6           | 0           | 5              |
| FSO_RFP_Flow_Monitoring.csv           | 6           | 8           | 5              |
| FSO_RFP_Platform_Features.csv         | 2           | 5           | 12             |
| FSO_RFP_General_Features.csv          | 10          | 12          | 10             |
| FSO_RFP_AI_Features.csv               | 10          | 0           | 1              |
| **Total**                             | **78**      | **86**      | **80**         |


