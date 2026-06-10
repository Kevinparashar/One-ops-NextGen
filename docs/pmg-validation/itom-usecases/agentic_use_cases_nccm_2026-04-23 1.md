# ObserveOps Agentic AI Use-Case Catalog
_Scope: nccm · Generated: 2026-04-23 · 32 use-cases across 18 features_

## Top 10 cross-domain plays
_Use-cases that chain ≥2 capabilities or implicitly span ≥2 CSVs (NCCM + SLO, NCCM + Logs, NCCM + Alerts, NCCM + Flow, NCCM + threat intel). Ranked by Impact then Effort._

| Rank | Sr No | Use-case | Agent(s) | Why it's cross-domain | Impact | Effort |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 12 | Pre-flight firmware-upgrade blast-radius assessor w/ staged rollout | impact-analyzer + forecaster | NCCM + SLO + Flow; past-upgrade outcomes inform wave ordering and rollback checkpoints | H | H |
| 2 | 25 | Bulk remediation runbook synthesis from recurring compliance failures | remediator + narrator | NCCM + Platform runbook; canary→batch rollout with pause-on-drift | H | H |
| 3 | 30 | CVE↔firmware↔patch linkage across inventory | correlator | NCCM + NIST threat intel; vendor-agnostic upgrade workflow trigger | H | M |
| 4 | 1 | Drift-to-impact translator (deviation → affected services/SLOs) | impact-analyzer + rca-analyst | NCCM + SLO + Service Map | H | M |
| 5 | 29 | Pre-push config-diff impact predictor with similar-change lookup | impact-analyzer + rca-analyst | NCCM + Flow + SLO + change history; recommends approval level | H | M |
| 6 | 28 | Syslog-to-change-event correlation with actor linkage | correlator | NCCM + Log Monitoring; actor + diff + backup in one bundle | H | M |
| 7 | 10 | Onboarding-failure audit-trail RCA walker | rca-analyst + remediator | NCCM + discovery logs; ranked causes with next-step fix | H | M |
| 8 | 10 | Batch-discovery failure differential RCA | rca-analyst + correlator | NCCM + discovery telemetry; clusters with delta vs last successful sweep | H | M |
| 9 | 25 | Corrective CLI-diff generator with approval workflow | remediator + rca-analyst | NCCM + Compliance + change-management; 3 similar past remediations cited | H | M |
| 10 | 31 | Link Aruba advisories to in-fleet device cohorts | correlator | NCCM + vendor advisory feed; automated remediation workflow trigger | H | M |

## By domain

### NCCM (Network Configuration & Change Management)

| Sr No | Feature (excerpt, ≤80 chars) | Use-case | Agent | Trigger | Output | Impact | Effort |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Baseline config management — detect deviations from compliance baselines | Drift-to-impact translator: map each baseline deviation to affected services/SLOs | impact-analyzer | Scheduled baseline scan or on-demand compliance check | Ranked drift card listing each deviating device, the policy/control it breaks, and downstream services/SLOs exposed | H | M |
| 2 | Side-by-side config comparison — added/modified/removed segments | Plain-English config diff explainer | narrator | User opens diff view between two config versions | 3-5 bullet narrative: what stanzas changed, risk-relevant edits, unchanged-but-notable sections | H | L |
| 2 | Side-by-side config comparison — added/modified/removed segments | Baseline drift narrative for a device | narrator | Scheduled baseline comparison or on-demand drift check | Short paragraph describing how device config diverges from baseline, grouped by intent (ACL, routing, AAA) | H | M |
| 5 | OOTB + custom vendor/device config templates without custom dev | Generate vendor config template from pasted device snippet | query-translator | User pastes 1-3 running-config samples + picks vendor/OS | Parameterized template (Jinja-style) with variables, defaults, and dry-run render against the sample | H | M |
| 5 | OOTB + custom vendor/device config templates without custom dev | NL-to-template authoring ("VLAN trunk on Cisco IOS with native 10") | query-translator | User types intent in template builder chat | Vendor-syntactically-valid template + diff preview on a reference device | H | M |
| 7 | Operational dashboards for ongoing/completed config tasks | Shift-handoff narrative from NCCM task board | narrator | End of operator shift / on-demand "summarize queue" | 5-bullet handoff: in-flight pushes, failed tasks needing retry, long-running jobs, recently completed, watch-items | H | L |
| 7 | Operational dashboards for ongoing/completed config tasks | NL dashboard annotation "what changed today" | narrator | Dashboard load / daily schedule | 2-3 sentence annotation on task-status widget describing today's volume, failure clusters, top device groups touched | M | L |
| 9 | OOTB reports for operational, compliance, audit needs | Audience-tailored cover page for OOTB reports | narrator | User exports a compliance/audit/ops report | Narrative cover page (1 paragraph exec view + 1 paragraph technical view) summarizing scope, headline numbers, notable findings | H | L |
| 10 | Unified discovery with audit logs for onboarding-failure RCA | Onboarding-failure audit-trail RCA walker | rca-analyst | User clicks "Investigate" on a failed onboarding job | Ranked probable causes (bad credential profile, SNMP mismatch, SSH timeout, ACL blocking poller, unsupported OS) cited to audit log lines, credential-attempt records, and reachability probes, plus next-step fix | H | M |
| 10 | Unified discovery with audit logs for onboarding-failure RCA | Batch-discovery failure differential RCA | rca-analyst | Scheduled bulk discovery sweep completes with >N failures | Clustered failure cohorts (by subnet, vendor, credential profile, protocol) with dominant cited cause per cluster and delta vs last successful sweep | H | M |
| 11 | Action-history records — executed commands + outputs | Plain-English summary of a change-window action history | narrator | Auditor opens action-history for a change ticket / device / window | Chronological narrative: who ran what, on which devices, intent of each command block, anomalies in output | H | M |
| 11 | Action-history records — executed commands + outputs | Per-command output interpreter | narrator | User hovers/clicks a command entry in action history | 1-2 sentence explanation of what the command did and whether output looks nominal vs unexpected | M | M |
| 12 | Firmware upgrade with mandatory pre-flight checks and incremental validation | Pre-flight firmware-upgrade blast-radius assessor w/ staged-rollout recommendation | impact-analyzer | User schedules firmware upgrade batch | Risk card per device: dependent flows, SLOs at risk, similar past upgrades' outcomes, suggested wave ordering and rollback checkpoints | H | H |
| 13 | Explorer filter across configs and compliance data | NL-to-Explorer filter query | query-translator | User types "non-compliant Cisco devices missing AAA in DC-East" | Explorer filter JSON (field predicates + scope) executed with row-count validation | H | L |
| 15 | Runbook automation with CSV-driven bulk operations | NL-to-runbook CSV context generator | query-translator | User describes bulk task ("rotate SNMP community on all edge routers") | Runbook YAML + pre-filled CSV context (device list, params) with schema-validated preview | H | M |
| 15 | Runbook automation with CSV-driven bulk operations | Example-to-runbook from one worked device session | query-translator | User supplies one successful CLI transcript | Parameterized runbook steps + CSV column schema, tested in dry-run on 1 target | M | M |
| 19 | Compliance rules with metadata: rationale, control mapping, remediation | Human-friendly rule rationale card | narrator | User opens a compliance rule / fails a rule on a device | Rewritten rationale in operator language, why it matters, mapped control in plain terms, remediation steps as numbered list | H | L |
| 19 | Compliance rules with metadata: rationale, control mapping, remediation | Per-device violation narrative with remediation | narrator | Device flagged non-compliant against a rule | Short narrative: which rule, what on this device triggered it, exact remediation commands tailored to device tech | H | M |
| 21 | Policy compliance dashboards — severity, scoring, status transitions | Weekly compliance posture narrative | narrator | Weekly schedule / dashboard export | Narrative cover: posture delta vs last week, severity mix shifts, devices that newly drifted, devices that recovered | H | L |
| 21 | Policy compliance dashboards — severity, scoring, status transitions | Status-transition storyline for a device | narrator | User drills into a device's compliance timeline | Timeline-as-prose: "Device X was compliant until <date>, drifted on rule Y after change Z, recovered on <date>" | M | M |
| 22 | Custom compliance benchmarks from OOTB + custom rules | NL-to-benchmark composer ("PCI-lite: CIS L1 + our password rules") | query-translator | User describes benchmark intent and scope | Benchmark definition JSON (rule refs + custom rule stubs) with scorecard preview on sample inventory | H | M |
| 22 | Custom compliance benchmarks from OOTB + custom rules | NL-to-custom-rule authoring for benchmark gaps | query-translator | User types rule intent ("no telnet enabled on any interface") | Rule DSL/regex with match/expected blocks, tested against a sampled device configs set | H | M |
| 25 | Remediation actions for compliance failures executable from results | Suggest corrective CLI diff for compliance failure with approval workflow | remediator | User clicks "Fix" on failed compliance rule in results view | Proposed per-device CLI diff + 3 similar past approved remediations + rollback snippet + blast-radius note (device count, change window) | H | M |
| 25 | Remediation actions for compliance failures executable from results | Synthesise bulk remediation runbook from recurring compliance failures | remediator | Scheduled compliance audit flags same failure across >N devices | Draft runbook with staged rollout plan (canary → batch), dry-run output per device class, pause-on-drift checkpoints | H | H |
| 28 | Syslog change detection — identify who made config changes, trigger backup | Syslog-to-change-event correlation with actor linkage | correlator | New syslog config-change line ingested | Linked bundle: syslog line + resulting config diff + actor + backup artifact, with 1-line "why linked" (time window, device, session ID match) | H | M |
| 28 | Syslog change detection — identify who made config changes, trigger backup | Group change-storm across devices into one change campaign | correlator | Burst of config-change syslogs within rolling window | One change-campaign card listing N devices, common actor/template, rationale for grouping (same user, same CLI pattern, same window) | M | M |
| 29 | NCM drift + failed-action policies with baseline/running/startup compare | Pre-push config-diff impact predictor with similar-change lookup | impact-analyzer | User opens config diff for review (pre-push) or on failed-action alert | Affected devices/services/SLOs, risk score, comparable past diffs with outcomes, recommended approval level | H | M |
| 29 | NCM drift + failed-action policies with baseline/running/startup compare | Startup-vs-running divergence reboot-risk forecaster | impact-analyzer | Scheduled drift scan detects running != startup | Per-device reboot-risk card: what config gets lost on reload, which services/flows depend on those lines, blast radius if device power-cycles | M | M |
| 30 | NIST vuln DB correlation — patches/firmware, vendor-agnostic upgrade workflows | CVE↔firmware↔patch linkage across inventory | correlator | NIST feed update or scheduled nightly sweep | Per-device bundle: matched CVEs + applicable patch/firmware + affected device list + confidence rationale (CPE match, version range, vendor advisory) | H | M |
| 30 | NIST vuln DB correlation — patches/firmware, vendor-agnostic upgrade workflows | Dedup duplicate CVE advisories across vendor feeds | correlator | New advisory arrives from NIST/vendor | Single CVE card merging NIST + vendor entries, rationale showing which fields matched (CVE ID, CPE, KB ref) | M | L |
| 31 | Aruba Central — correlate firmware vuln intel with automated remediation | Link Aruba advisories to in-fleet device cohorts | correlator | Aruba Central advisory webhook or poll | Cohort bundle: advisory + matching Aruba devices grouped by model/firmware + linked remediation workflow, with join-key rationale | H | M |
| 31 | Aruba Central — correlate firmware vuln intel with automated remediation | Link recent Aruba firmware pushes to subsequent alerts | correlator | New alert on Aruba device within change window | Change↔incident link card: firmware push event + alerts within N hours + confidence score + rationale | M | M |

## By capability

### narrator
- **Sr 2 — NCCM** — Plain-English config diff explainer
- **Sr 2 — NCCM** — Baseline drift narrative for a device
- **Sr 7 — NCCM** — Shift-handoff narrative from NCCM task board
- **Sr 7 — NCCM** — NL dashboard annotation "what changed today"
- **Sr 9 — NCCM** — Audience-tailored cover page for OOTB reports
- **Sr 11 — NCCM** — Plain-English summary of a change-window action history
- **Sr 11 — NCCM** — Per-command output interpreter
- **Sr 19 — NCCM** — Human-friendly rule rationale card
- **Sr 19 — NCCM** — Per-device violation narrative with remediation
- **Sr 21 — NCCM** — Weekly compliance posture narrative
- **Sr 21 — NCCM** — Status-transition storyline for a device

### query-translator
- **Sr 5 — NCCM** — Generate vendor config template from pasted device snippet
- **Sr 5 — NCCM** — NL-to-template authoring
- **Sr 13 — NCCM** — NL-to-Explorer filter query
- **Sr 15 — NCCM** — NL-to-runbook CSV context generator
- **Sr 15 — NCCM** — Example-to-runbook from one worked device session
- **Sr 22 — NCCM** — NL-to-benchmark composer
- **Sr 22 — NCCM** — NL-to-custom-rule authoring for benchmark gaps

### impact-analyzer
- **Sr 1 — NCCM** — Drift-to-impact translator
- **Sr 12 — NCCM** — Pre-flight firmware-upgrade blast-radius assessor w/ staged-rollout
- **Sr 29 — NCCM** — Pre-push config-diff impact predictor with similar-change lookup
- **Sr 29 — NCCM** — Startup-vs-running divergence reboot-risk forecaster

### correlator
- **Sr 28 — NCCM** — Syslog-to-change-event correlation with actor linkage
- **Sr 28 — NCCM** — Group change-storm across devices into one change campaign
- **Sr 30 — NCCM** — CVE↔firmware↔patch linkage across inventory
- **Sr 30 — NCCM** — Dedup duplicate CVE advisories across vendor feeds
- **Sr 31 — NCCM** — Link Aruba advisories to in-fleet device cohorts
- **Sr 31 — NCCM** — Link recent Aruba firmware pushes to subsequent alerts

### rca-analyst
- **Sr 10 — NCCM** — Onboarding-failure audit-trail RCA walker
- **Sr 10 — NCCM** — Batch-discovery failure differential RCA

### remediator
- **Sr 25 — NCCM** — Suggest corrective CLI diff for compliance failure with approval workflow
- **Sr 25 — NCCM** — Synthesise bulk remediation runbook from recurring compliance failures

## Skipped

| CSV | augmentable | automatable | not-applicable |
| --- | --- | --- | --- |
| FSO_RFP_NCCM.csv | 18 | 10 | 3 |

_automatable rows: 3, 4, 8, 14, 16, 17, 18, 23, 24, 26 (version-control retention, dynamic template assignment, bulk scheduling, config download, runbook exec history, compliance check execution, OOTB CIS benchmarks, rule severity metadata, manual/scheduled policy exec, CSV/PDF export)._

_not-applicable rows: 6, 20, 27 (universal multi-vendor syntax accommodation, diverse compliance evaluation methodologies, integrated secure terminal access)._
