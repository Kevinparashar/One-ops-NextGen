# ObserveOps Agentic AI Use-Case Catalog
_Scope: metric · Generated: 2026-04-23 · 10 use-cases across 4 features_

## Top 10 cross-domain plays
_All 10 surfaced use-cases, ranked by Impact then Effort. Metric Explorer is inherently cross-domain — every use-case here operates on KPIs drawn from APM/Infra/Logs/RUM/Flow/Network, and several chain into rca-analyst/forecaster/narrator._

| Rank | Sr No | Use-case | Agent(s) | Why it's cross-domain | Impact | Effort |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 4 | Forecast explanation in plain English | narrator + forecaster | Extends FSO_RFP_AI_Features forecasting; applies to any KPI source | H | L |
| 2 | 3 | Period-over-period regression narrative | narrator | Applies to APM/Infra/RUM/Flow/SLO KPIs | H | L |
| 3 | 4 | Anomaly narration on top of detector output | narrator + rca-analyst | Extends AI_Features anomaly signals with LLM narration of cluster + co-moving KPIs | H | M |
| 4 | 2 | LLM-suggested KPI group for overlay chart | correlator + rca-analyst | Suggests cross-domain logical groupings (e.g., JVM heap + GC + checkout latency) | H | M |
| 5 | 2 | Co-movement explainer across overlaid KPIs | correlator + narrator | Pairwise co-movement bundle w/ lag, direction, confidence | H | M |
| 6 | 7 | NL → curated metric view template | query-translator | Golden-signal views reusable across APM/Infra/RUM monitors | H | M |
| 7 | 7 | Example monitor → portable view template | query-translator | Templatizes a well-tuned monitor for peer monitors | H | M |
| 8 | 3 | Behavioral-shift callout annotations on overlay chart | narrator | Inline chart annotations for material divergence | M | M |
| 9 | 4 | Delta/derivative interpretation helper | narrator | Rate-of-change read-out for any KPI transform | M | L |
| 10 | 4 | Log-scale / moving-average "what am I looking at" helper | narrator | Explains how the view reshapes data for any KPI | L | L |

## By domain

### Metric Explorer

| Sr No | Feature (excerpt, ≤80 chars) | Use-case | Agent | Trigger | Output | Impact | Effort |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | Correlative comparison of multiple KPIs — overlay metrics on unified chart | LLM-suggested KPI group for overlay chart | correlator | User selects a seed KPI or opens Metric Explorer | Ranked KPI groupings (same unit / logical family) with 1-line rationale + one-click overlay | H | M |
| 2 | Correlative comparison of multiple KPIs — overlay metrics on unified chart | Co-movement explainer across overlaid KPIs | correlator | User overlays 2+ KPIs or clicks "explain correlation" | Pairwise co-movement bundle with lag, direction, confidence + auditable rationale | H | M |
| 3 | Interactive timeline comparison — current vs previous periods | Period-over-period regression narrative | narrator | User selects compare-to-previous (week/month/year) on a metric | 3-5 bullet plain-English summary of what shifted (magnitude, direction, time-of-day pattern changes, notable new spikes/dips) | H | L |
| 3 | Interactive timeline comparison — current vs previous periods | Behavioral-shift callout annotations on overlay chart | narrator | Comparison overlay rendered with statistically material divergence | Inline chart annotations (e.g., "Tue 14:00: p95 latency +38% vs last week, sustained 2h") with one-sentence description per callout | M | M |
| 4 | Statistical transformations — anomaly, forecast, MA, delta, log | Anomaly narration on top of detector output | narrator | Anomaly detector flags points on the selected KPI | 2-3 sentence description per anomaly cluster: when, how far from expected band, shape (spike/dip/drift), concurrent co-moving KPIs | H | M |
| 4 | Statistical transformations — anomaly, forecast, MA, delta, log | Forecast explanation in plain English | narrator | User applies Forecasting transform to a KPI | Short narrative: projected direction, confidence band width, seasonality picked up, when forecast crosses any visible threshold | H | L |
| 4 | Statistical transformations — anomaly, forecast, MA, delta, log | Delta/derivative interpretation helper | narrator | User applies Delta or Derivative transform | One-paragraph read-out: rate-of-change regime, inflection timestamps, whether slope is accelerating/decelerating vs prior window | M | L |
| 4 | Statistical transformations — anomaly, forecast, MA, delta, log | Log-scale / moving-average "what am I looking at" helper | narrator | User toggles Log Scale or Moving Average on a chart | Tooltip-style 1-2 sentence explanation of how the view reshapes the data and which features become visible/hidden | L | L |
| 7 | Curated metric views reapplied across monitors of similar types | NL to curated metric view template | query-translator | User types intent (e.g. "golden signals for JVM app servers") in Metric Explorer | Reusable view spec with KPIs, default groupings, metric transformations, and live preview rendered on one representative monitor | H | M |
| 7 | Curated metric views reapplied across monitors of similar types | Example monitor to portable view template | query-translator | User selects one well-tuned monitor and clicks "Templatize this view" | Parameterized view template (monitor-type placeholders, metric mappings) with dry-run application on 2-3 peer monitors for validation | H | M |

## By capability

### narrator
- **Sr 3 — Metric Explorer** — Period-over-period regression narrative
- **Sr 3 — Metric Explorer** — Behavioral-shift callout annotations on overlay chart
- **Sr 4 — Metric Explorer** — Anomaly narration on top of detector output
- **Sr 4 — Metric Explorer** — Forecast explanation in plain English
- **Sr 4 — Metric Explorer** — Delta/derivative interpretation helper
- **Sr 4 — Metric Explorer** — Log-scale / moving-average "what am I looking at" helper

### correlator
- **Sr 2 — Metric Explorer** — LLM-suggested KPI group for overlay chart
- **Sr 2 — Metric Explorer** — Co-movement explainer across overlaid KPIs

### query-translator
- **Sr 7 — Metric Explorer** — NL to curated metric view template
- **Sr 7 — Metric Explorer** — Example monitor to portable view template

## Skipped

| CSV | augmentable | automatable | not-applicable |
| --- | --- | --- | --- |
| FSO_RFP_Metric_Explorer.csv | 4 | 2 | 1 |

_automatable rows: 5, 6 (save customized views per user/team, share visualizations via email/Teams)._

_not-applicable rows: 1 (in-built Metric Explorer interface — product framing)._
