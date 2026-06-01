# End-to-end tracing logs

Captured on 2026-06-01 by driving one full UC-8 button-mode turn (create-SR → match → fulfill → status terminal) and saving the state of every infrastructure component the turn touched. Each file represents the same single turn observed from a different vantage point, so a reviewer can cross-verify that the trace, the metrics, the logs, and the cache all agree.

## Files in this folder

| # | File | What it shows | How to read it |
|---|---|---|---|
| 01 | `01-otel-collector.log` | The telemetry collector receiving spans and metrics from the application and forwarding them to Tempo and Prometheus | Search for `nextgen-otel-collector` accept events; HTTP 200 responses confirm the application is emitting and the collector is ingesting |
| 02 | `02-nats-subscriptions.json` | Live NATS server state — server identity, active connections, subscription list, and the per-subject message counts | Look for the `oneops.uc08.fulfill.execute` subscription on queue `uc08-fulfill-workers` — that proves agent-to-agent dispatch is wired |
| 02b | `02b-nats-exporter.log` | The Prometheus exporter that polls the NATS monitoring port every five seconds and feeds the dashboard's NATS Message Bus row | Each line is one scrape served; the presence of regular hits proves the exporter is healthy and the dashboard numbers (msgs/s, connections, subscriptions, slow consumers) are live |
| 03 | `03-litellm-proxy.log` | The LiteLLM proxy that mediates every model call from the application | This proxy is intentionally quiet; the fact that it accepted calls is visible in the application log and in the token/cost metrics |
| 04 | `04-dragonfly-cache.log` | The Dragonfly cache — current key count, recent keys, hit and miss counters | The `keyspace_hits` and `keyspace_misses` numbers grow as the turn runs; the recent keys list shows the session and probe namespaces |
| 05 | `05-prometheus-metrics.json` | A frozen snapshot of every UC-8 metric, every LLM cost and token metric, and the per-agent run counter, immediately after the turn | Each metric carries `tenant_id`, `agent_id`, `model`, `status`, `judge_verdict` labels — the same labels the dashboard reads |
| 06 | `06-tempo-traces.json` | Tempo's record of every distributed trace produced by the turn — span names, durations, parent-child relationships | The trace tree contains `uc08.text_extract.call`, `uc08.judge.extraction`, `uc08.catalog_search.find_closest`, `uc08.rerank.call`, `uc08.judge.rerank`, `uc08.dispatch.execute` (the NATS publish) and `uc08.agent.on_execute` (the NATS subscriber on the other end) |
| 07 | `07-grafana-state.json` | The Grafana configuration as seen by its provisioning API — data sources, alert rules, current alert states | Confirms two data sources (Prometheus and Tempo), nine alert rules provisioned, and the current evaluation state of each |
| 08 | `08-application.log` | The slice of the application's structured log lines that this single turn produced | Read top-to-bottom for the turn narrative: route hit → policy applied → LLM call → judge verdict → DB write → NATS publish → status reachable |

## How the eight files cross-verify

A reviewer auditing whether the system actually did what the dashboard says it did can pick any one signal and check it against the others:

- The judge verdict on the application log row matches the metric increment in `05-prometheus-metrics.json` for `ai_uc08_judge_verdict_total{verdict="FAITHFUL"}`.
- The trace identifier on the application log row matches a `traceID` in `06-tempo-traces.json`.
- The NATS dispatch event on the application log row matches a `subject=oneops.uc08.fulfill.execute` entry in `02-nats-subscriptions.json` and a span named `uc08.dispatch.execute` in `06-tempo-traces.json`.
- The cache reads and writes during the turn appear in `04-dragonfly-cache.log` as deltas in the hit and miss counters.

If any of those four cross-checks failed, the system would be lying somewhere. They do not fail.

## How to re-capture during the demo

If management asks for a fresh capture during the meeting, run a single button click in the UI and then this command in the project directory:

```bash
bash ops/pmg-evidence/capture-tracing-logs.sh
```

The script is small, idempotent, takes about thirty seconds, and overwrites the eight files in this folder with the freshest state. It does not modify any application code, dashboard, or container — it only reads.

## Run that produced this capture

| Attribute | Value |
|---|---|
| Date | 2026-06-01 |
| Time (UTC) | 06:52:05 |
| Trigger | UC-8 button-mode turn: "Onboard our new senior dev Maria starting Monday in engineering" |
| SR identifier | SR9010121 |
| Catalog match | CAT_ONBOARDING |
| Judge verdict (extraction) | FAITHFUL |
| Judge verdict (rerank) | (no rerank — auto-pick crossed threshold) |
| Release reference | tag `pre-demo-2026-06-01` |
