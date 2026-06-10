# PMG demo runbook

**Audience.** Product Management Group sign-off meeting.
**Time budget.** 45 minutes of demo + 15 minutes Q&A.
**Pre-flight.** Run `make pmg-verify` 30 minutes before the meeting and
confirm every phase row in `ops/pmg-evidence/REPORT.md` is green. If
anything is red, fix it before opening the meeting.
**Companion docs.** Hand out [`docs/briefings/manager-decision-package.md`](docs/briefings/manager-decision-package.md)
at the start — it frames the 10 binding-answer questions for after the demo.

---

## Pre-flight checklist (T-30 min)

```bash
# 1. All containers up
docker ps --format '{{.Names}}: {{.Status}}' | grep -E "nextgen-|ai-service-"

# 2. API server live + UC-8 NATS agent registered
tail -20 /tmp/uc08_prod_server.log | grep -E "ready|uc08_agent_started"

# 3. Evidence verification
make pmg-verify
```

Open these tabs in the browser:
| Tab | URL | Credentials |
|---|---|---|
| OneOps UI | `http://127.0.0.1:8765/static/index.html` | tenant=T001, user=USR00001, role=service_desk_agent |
| Grafana — Overview dashboard | `http://localhost:3041/d/oneops-overview` | oneops / oneops |
| Grafana — Explore (Tempo) | `http://localhost:3041/explore` | (same) — datasource Tempo |
| Grafana — Alerting → Alert rules | `http://localhost:3041/alerting/list` | (same) |
| Prometheus | `http://localhost:9391/graph` | — |
| Live server log | `tail -f /tmp/uc08_prod_server.log` | — |

---

## Demo script (45 min)

### Act 1 — Architecture & substrate (5 min)

Open `docs/planning/production-maturity-plan.md` §F-LOCKED. State plainly:
> The Day-1 cut is 7 steps. All 7 are now green. The PMG-evidence
> directory has a verifier — `make pmg-verify` — that walks every
> phase and confirms each artefact. If any row is red, the gate
> blocks; if all green, we ship.

Show `ops/pmg-evidence/REPORT.md` on screen — point at the seven
green rows.

### Act 2 — UC-8 end-to-end button flow (15 min)

Driving sentence: **"UC-8 is the showcase because it touches every
production component in one click."**

In the OneOps UI click **✨ Fulfill Catalog Request**, type:

> *Onboard our new senior dev Maria starting Monday in the
> engineering team — full kit please.*

Press **✨ Auto-create SR & match**.

**While the modal renders, narrate**:
1. The textarea text becomes the user_text on `/api/uc08/create-sr`.
2. LLM extracts the title (`gpt-4o-mini`, via the LiteLLM gateway on
   port 4301). Description is preserved verbatim — audit-grade.
3. A second LLM call — the **judge** — independently scores the
   extraction FAITHFUL / UNFAITHFUL / UNCERTAIN with confidence.
   That verdict surfaces on the response.
4. The SR id is **deterministic** (`SR` + 7-digit sequential per
   tenant). Title/description LLM-generated; priority, SLA, IDs,
   category — all deterministic from the 4×4 Motadata matrix and
   catalog metadata.

When the match card appears, narrate:
5. Embedding cosine search picked the catalog candidates. If top-1
   sits in the soft confidence zone, an LLM reranker breaks the tie.
6. A second judge validates the rerank decision.
7. Enrichment is filled (priority, owner_group, SLA due, historical
   suggestions from past SRs).

Click **✅ Proceed with these values**. While the progress bar
animates, narrate:
8. `/api/uc08/fulfill` persisted the RITM + tasks then published
   `oneops.uc08.fulfill.execute` to NATS (queue group
   `uc08-fulfill-workers`).
9. A separate agent worker received the message, ran `execute_plan`,
   went through the task DAG, and wrote progress to Postgres as it
   went. The browser polls `/status/{ritm_id}` every 2 seconds.

After completion, switch to **Grafana → Explore → Tempo**, paste the
trace id shown in the chat reply (or grab from
`/tmp/uc08_prod_server.log` — search for the latest
`uc08.fulfill.completed`). Show the **11-span tree** including:
- `uc08.text_extract.call`
- `uc08.judge.extraction`
- `uc08.catalog_search.find_closest`
- `uc08.rerank.call`
- `uc08.judge.rerank`
- `uc08.dispatch.execute` (the NATS publish)
- `uc08.agent.on_execute` (the NATS subscriber — different agent)
- `uc08.priority.derive`, `uc08.historical_suggest.run`,
  `uc08.sr_id.mint`, `uc08.core.fulfill_request`

> **The dispatch.execute → agent.on_execute hop is the agent-to-agent
> over NATS.** Different worker, different trace branch, same root
> trace id.

### Act 3 — Observability + cost (5 min)

Open the **OneOps Overview dashboard**. Walk the panels:
- Top row: turns/min, active agents, **cumulative LLM cost ($)**, success ratio.
- **Per-UC latency p50/p95/p99**.
- **LLM cost per minute by tenant** ($/min).
- **Cost & Usage — per model × per tenant** (finance-billing view).
- Cache hit ratio.

Talk track:
> Every panel is fed by real Prometheus metrics from the running
> system. The `ai_llm_cost_usd_micros_total` counter is wired at
> the gateway boundary so every LLM call lands here regardless of
> which UC made it. Per-tenant cost is the basis for billing and
> for the *TenantLLMCostBurst* alert.

### Act 4 — Alerting chain (5 min)

Open **Grafana → Alerting → Alert rules → OneOps folder**. Show **9
rules** — 6 baseline + 3 UC-8.

Open `ops/pmg-evidence/day1-am-alert-fired.log`. Show:
- Live PromQL eval of each UC-8 alert against real Prometheus.
- Forced-breach simulation (threshold 0): expressions return
  "FIRING (would page)" — proves the evaluator → condition →
  comparator chain is wired, won't be discovered broken during a
  real incident.
- Webhook contact point active (`oneops-default`).

### Act 5 — Synthetic probes (3 min)

Open `ops/pmg-evidence/day1-am-slo-probes.log`. Show two cycles of
the four-UC probe loop with measured latencies. Run live:

```bash
PROBE_CYCLES=1 ./ops/probes/run-all-loop.sh
```

Talk track:
> Probes are bash, not Python — no deps beyond `curl` and `date`.
> They replace cron in production with a `kubectl CronJob` pointing
> at the same script. The format is parse-friendly so Loki / Tempo
> can ingest each line as a structured event.

### Act 6 — Local CI gate (3 min)

Open `ops/pmg-evidence/day2-pm-ci-gate.log` (green run, exit 0) and
`ops/pmg-evidence/day2-pm-ci-blocks-bad-merge.log` (planted
violation, exit 2). Run live:

```bash
make ci-fast      # ruff → mypy → unit (~10s)
```

Show `.git/hooks/pre-commit` — every commit automatically runs the
fast gate. Bypass only with `--no-verify`; the next push is gated by
`make ci`.

Talk track:
> Until GitHub access lands, this script IS the CI. When GitHub
> comes online, the workflow YAML is a 10-line wrapper around
> `scripts/ci.sh` — logic stays portable. Ruff and mypy are running
> a documented ratchet baseline; new violations in any non-ignored
> category fail the gate.

### Act 7 — Decision package + scope honesty (4 min)

Hand out `docs/briefings/manager-decision-package.md`. Page through the 10
questions in §G and the recommended answers + cost-of-alternative.

Be explicit about what is **NOT** in the cut and why:
| Deferred item | Why deferred | Estimated rebuild |
|---|---|---|
| JWT verification at the door | Needs IdP integration spec, JWKS rotation. Half-done is a security regression. | 2–3 days |
| Materialised RBAC matrix | Twice-enforcement plumbing + migration. | 2–3 days |
| Drift detector + full CI eval gate | Sized for ~2 days alone after Day 2 PM. | ~2 days |
| EKS / Istio / Lambda / Bridge / ClickHouse / multi-region DR | Whole infra port. | 8–12 weeks |
| UC-2..UC-29 + intent ontology + ITOM + Studio | Capability expansion roadmap. | 12–24 weeks |

> **The Day-1 cut delivers ~80% of the 22-doc target with
> production-grade rigour on each deliverable. The remaining ~20%
> is named, sized, sequenced — it is on the post-PMG roadmap, not
> hidden.**

---

## Q&A — Likely manager questions + grounded answers

**Q. Why UC-8 first and not the chat path?**
The button flow exercises every component (LLM gateway, judge,
embeddings, NATS dispatch, executor, status polling, dashboard,
alerts). Chat path adds router complexity but no new integration
surface. We can wire UC-8 into chat in a focused 4–6 hour follow-on
(memory record `project_oneops_uc08_chat_wiring_post_demo`).

**Q. Why is the LLM-as-judge a validator, not a gate?**
Production rule choice: rejecting LLM output on judge disagreement
adds latency + caller-visible failures. Surfacing the verdict on the
response lets the caller (UI / downstream agent) make the
calibrated decision. The judge metric `ai_uc08_judge_verdict_total`
gives us drift signal — when UNFAITHFUL share rises >10% over 10m
we page (alert `UC08JudgeUnfaithfulHigh`).

**Q. How do you know the NATS hop actually fires?**
Three converging signals: (1) `ai_uc08_fulfill_total{dispatch="nats"}`
counter increments per request, (2) `ai_uc08_agent_events_total{outcome="received"}`
on the agent side, (3) the `uc08.dispatch.execute` and
`uc08.agent.on_execute` spans appear in Tempo with a shared trace id
across different process boundaries. Evidence at
`ops/pmg-evidence/phase-4-uc08-fulfillment.log`.

**Q. What's the rollback story if the NATS path fails?**
Graceful fallback to in-process `asyncio.create_task`. If NATS
publish errors, the route logs a warning, switches `dispatch_via` to
`"asyncio"`, and proceeds. The user never sees a NATS-related error.

**Q. Per-tenant cost — is it actually per tenant?**
Yes. The `ai_llm_cost_usd_micros_total{tenant_id, model}` series
is keyed on `tenant_id` at the gateway level. Live data for T001
is captured in `ops/pmg-evidence/day1-am-cost-dashboard.log`. The
finance-billing panel (id 43 on the overview dashboard) slices by
tenant × model.

**Q. What stops a developer from shipping broken code?**
Three layers. (1) Pre-commit hook runs `make ci-fast` automatically
on every commit. (2) `make ci` runs the full suite before push. (3)
Until GitHub Actions, the operator runs `make pmg-verify` before
tagging. Evidence:
`ops/pmg-evidence/day2-pm-ci-blocks-bad-merge.log`.

**Q. Why a ratchet baseline instead of fixing all ruff/mypy errors?**
Two reasons. (1) Each ignored category is documented with rationale
and a TBD tag; the ratchet tightens as bucket-by-bucket gets paid
down — measured progress instead of one giant sweep. (2) The gate
already enforces every *non-ignored* category, so new debt cannot
accumulate. Task #18 tracks the full sweep post-PMG.

---

## Close-out

1. State the recommendation: **proceed to Day 2** of the locked plan
   (`AgentManifest` export/import + eval gate scope).
2. Ask for binding answers on the 10 `§G` decision-package questions.
3. Confirm next checkpoint date.

Hand over: this runbook, the manager decision package, and the live
`ops/pmg-evidence/REPORT.md` print-out.
