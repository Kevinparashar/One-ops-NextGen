# ObserveOps Agentic AI Use-Case Catalog
_Scope: rum · Generated: 2026-04-23 · 18 use-cases across 15 features_

## Top 10 cross-domain plays
_Use-cases that chain ≥2 capabilities or implicitly span ≥2 CSVs (RUM + APM, RUM + Logs, RUM + NCCM/release, RUM + Alert Policy, RUM + Dashboard). Ranked by Impact then Effort._

| Rank | Sr No | Use-case | Agent(s) | Why it's cross-domain | Impact | Effort |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 28 | Frontend-to-backend session linkage across signals | correlator → narrator | RUM + APM + Logs + Infra; joins session→trace→logs→metrics on trace-ID/user keys | H | H |
| 2 | 19 | Frontend-error session-context RCA walker | rca-analyst + correlator | RUM + Alert Policy; chains correlator into RCA with session replay + resource-timing evidence | H | H |
| 3 | 16 | NL → cross-entity correlation query across RUM | query-translator | Multi-entity (sessions↔errors↔actions↔resources) | H | H |
| 4 | 13 | Release-impact diff on RUM KPIs | impact-analyzer + rca-analyst | RUM + NCCM/deploy; per-KPI regression card with rollback recommendation | H | M |
| 5 | 13 | Pre-promotion staging-vs-prod UX delta check | impact-analyzer | RUM + CI/deploy pipeline; gating verdict for release promotion | H | M |
| 6 | 19 | Differential cohort RCA for error regression | rca-analyst | RUM + baseline comparison; cohort diff with evidence links | H | M |
| 7 | 27 | Multi-dimensional RUM cohort grouping with rationale | correlator + rca-analyst | RUM + user context (device/ISP/geo); chains capabilities | H | M |
| 8 | 23 | NL → RUM alert policy YAML | query-translator | RUM + Alert Policy; generates platform-compliant policy with dry-run | H | M |
| 9 | 23 | NL sketch → RUM dashboard JSON | query-translator | RUM + Dashboard; importable dashboard with widgets bound to RUM attributes | H | M |
| 10 | 18 | Stack-trace-to-English error explainer | narrator | Broad utility; surfaces across every JS-error-producing surface | H | M |

## By domain

### RUM

| Sr No | Feature (excerpt, ≤80 chars) | Use-case | Agent | Trigger | Output | Impact | Effort |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 5 | Granular visibility into real user sessions — lifecycle, page views, actions | Plain-English session narrative from structured timeline | narrator | User opens a session in RUM explorer | 4-6 sentence story of what the user did, where they struggled, how the session ended | H | M |
| 6 | Session replay-style analytics through structured timelines | Replay caption track — sentence per timeline segment | narrator | User scrubs/plays a session replay | Inline captions describing each action cluster ("rapid scrolling on pricing page", "abandoned checkout after 3 retries") | M | M |
| 7 | Dedicated explorer for sessions, views, actions, resources, errors, long tasks | NL to RUM entity explorer filter | query-translator | User types intent in explorer chat (e.g., "slow checkout views on Safari yesterday") | Explorer filter JSON bound to correct entity (view/session/action) with live preview count and auto-selected time range | H | M |
| 8 | Core Web Vitals and user-centric performance indicators | Weekly Core Web Vitals narrative cover page | narrator | Scheduled weekly RUM report | 1-page narrative summarizing LCP/CLS/TTFB movement vs last week, biggest regressions, audience-tailored (eng vs product) | H | L |
| 11 | Browser-wise performance and error comparison | Browser-breakdown narration for a release | narrator | User opens browser comparison widget post-deploy | 3-5 bullet narrative: which browsers/versions degraded, which improved, notable error spikes by rendering engine | M | L |
| 12 | Geo-spatial visibility with interactive maps — KPI by region/city | Geo heatmap narrative annotation | narrator | User hovers/clicks a region on the map | 2-3 sentence callout: KPI deltas for that region vs global baseline, top-affected cities, dominant error type | H | L |
| 13 | App versioning & env mapping — correlate UX with releases/deploys | Release-impact diff on RUM KPIs | impact-analyzer | New frontend release version detected in RUM stream | Per-KPI regression card (LCP, JS errors, conversion) vs prior release, broken down by browser/geo/device segments with affected-user counts and rollback recommendation | H | M |
| 13 | App versioning & env mapping — correlate UX with releases/deploys | Pre-promotion staging-vs-prod UX delta check | impact-analyzer | User requests promotion of a staging build to prod | Predicted prod UX impact: KPIs where staging already diverges from prod baseline, segments likely to regress, confidence score, and gating verdict (go/hold) | H | M |
| 16 | Advanced attribute-based search, filter, grouping, correlation across RUM | NL to cross-entity correlation query | query-translator | User pastes RCA question ("sessions with JS errors leading to failed checkout action") | Multi-entity correlation query (sessions to errors to actions) with group-by attributes and validated result sample | H | H |
| 17 | Frustration detection — rage clicks, dead clicks, long tasks | Frustration-event plain-English description with user context | narrator | Frustration signal fires on a session | 2-3 sentence description: what element, how many repeats, surrounding actions, severity phrasing | H | L |
| 18 | JavaScript error tracking — runtime, unhandled, promise rejections | Stack-trace-to-English error explainer | narrator | User opens a JS error detail pane | Plain-English explanation of the error, likely framework/module, user-visible symptom, grouped fingerprint name suggestion | H | M |
| 19 | Correlate frontend errors with session, browser, actions, resource timelines | Frontend-error session-context RCA walker | rca-analyst | User clicks "investigate" on a spiking JS error or error-rate alert fires | Ranked root-cause hypotheses citing session replay clip, browser/OS/version, preceding user actions, and resource-timing anomalies | H | H |
| 19 | Correlate frontend errors with session, browser, actions, resource timelines | Differential cohort RCA for error regression | rca-analyst | Error-rate anomaly detected vs 7-day baseline | Cohort diff report: "affected sessions share X browser + Y action sequence + Z slow resource; unaffected sessions lack these", with evidence links to sample sessions | H | M |
| 23 | Custom widgets, dashboards, policies using attribute filter/group on RUM | NL sketch to RUM dashboard JSON | query-translator | User describes "frontend health dashboard for mobile web users in EU" | Importable dashboard JSON with 4-6 widgets (Core Web Vitals, error rate, slow resources, session funnel) pre-bound to RUM attributes | H | M |
| 23 | Custom widgets, dashboards, policies using attribute filter/group on RUM | NL to RUM alert policy YAML | query-translator | User says "alert when LCP p75 > 2.5s for 10m on checkout page" | Policy YAML with SLI expression, thresholds, grouping keys, validated against platform schema and dry-run evaluated | H | M |
| 25 | OOTB widgets — top errors, resources, views, geo, browser | "What changed since last week" narration on each OOTB widget | narrator | Dashboard load / scheduled refresh | Inline 1-2 sentence annotation per widget flagging new entrants, biggest movers, vanished items | H | L |
| 27 | Correlate user context (IP, browser, OS, device, network) with perf/errors | Multi-dimensional RUM cohort grouping with rationale | correlator | User reports slowness or error spike detected | Grouped cohort bundles (e.g. "iOS Safari 17 + ISP-X + APAC") each with 1-line "why grouped" rationale and shared symptom fingerprint | H | M |
| 28 | Natively integrated RUM with shared data model across metrics/logs/traces/APM/network | Frontend-to-backend session linkage across signals | correlator | RUM session shows error or slow page load | Linked investigation card joining RUM session → APM trace → backend logs → infra metrics on shared time/trace-ID/user keys, with confidence score per link | H | H |

## By capability

### narrator
- **Sr 5 — RUM** — Plain-English session narrative from structured timeline
- **Sr 6 — RUM** — Replay caption track (sentence per timeline segment)
- **Sr 8 — RUM** — Weekly Core Web Vitals narrative cover page
- **Sr 11 — RUM** — Browser-breakdown narration for a release
- **Sr 12 — RUM** — Geo heatmap narrative annotation
- **Sr 17 — RUM** — Frustration-event plain-English description with user context
- **Sr 18 — RUM** — Stack-trace-to-English error explainer
- **Sr 25 — RUM** — "What changed since last week" narration on OOTB widgets

### query-translator
- **Sr 7 — RUM** — NL to RUM entity explorer filter
- **Sr 16 — RUM** — NL to cross-entity correlation query
- **Sr 23 — RUM** — NL sketch to RUM dashboard JSON
- **Sr 23 — RUM** — NL to RUM alert policy YAML

### correlator
- **Sr 27 — RUM** — Multi-dimensional RUM cohort grouping with rationale
- **Sr 28 — RUM** — Frontend-to-backend session linkage across signals

### impact-analyzer
- **Sr 13 — RUM** — Release-impact diff on RUM KPIs
- **Sr 13 — RUM** — Pre-promotion staging-vs-prod UX delta check

### rca-analyst
- **Sr 19 — RUM** — Frontend-error session-context RCA walker
- **Sr 19 — RUM** — Differential cohort RCA for error regression

## Skipped

| CSV | augmentable | automatable | not-applicable |
| --- | --- | --- | --- |
| FSO_RFP_RUM.csv | 15 | 6 | 8 |

_automatable rows: 3, 9, 10, 22, 24, 26 (JS snippet generation, relative time mapping, resource-loading phase breakdown, sampling controls, ITSM/notification integration, end-user context capture)._

_not-applicable rows: 1, 2, 4, 14, 15, 20, 21, 29 (integrated RUM module framing, browser support compatibility, framework compatibility, flame-chart viz widget, color-based bifurcation viz, HTTPS ingestion protocol, deployment model, "eliminate third-party tools" positioning)._
