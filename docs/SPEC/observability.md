# Observability

**Status:** Target (v1). Not yet implemented.
**Last updated:** 2026-04-26

> Today we emit structlog JSON logs to CloudWatch and have a half-wired
> `/api/costs` endpoint (Cost Explorer call) plus a `/api/telemetry` endpoint
> that reads the most-recent trading-service snapshot from DynamoDB. There
> is no EMF emission, no health check contract, no token telemetry.
>
> This doc is the target. Token telemetry is the most urgent subset — LLM
> cost is variable per decision and currently invisible.

## Telemetry substrate: CloudWatch EMF

All Agent and service metrics are emitted via CloudWatch Embedded Metric Format (EMF) — a structured JSON log line that CloudWatch Logs interprets as a metric. Chosen over Prometheus for these reasons:

- Service discovery for dynamically-provisioned Fargate Agents is nontrivial under pull-scrape.
- Ephemeral Lambdas (Compactor, Self-Critique) don't have scrape targets.
- Metrics are preserved even after the emitter dies (the log line is persisted).
- Same pipeline as regular logs; no new infra component to operate.
- Zero new cost beyond CloudWatch Logs we already pay for.

Trade-off accepted: less powerful than real Prometheus for ad-hoc queries, and CloudWatch Metrics has higher latency for the freshest data. Acceptable at our scale.

## Emitter contract

Every Agent and every service emits EMF lines tagged with at minimum:

- `project=TradingStrands`
- `component=<agent_type or service>`
- `org_id` where applicable
- `agent_id` where applicable
- `trace_id` when part of a cross-agent flow

Agents emit, at minimum:

| Metric | When | Unit |
|---|---|---|
| `agent.heartbeat.age_s` | Every heartbeat-ack | Seconds |
| `agent.decision.latency_ms` | Every decision completes | Milliseconds |
| `agent.decision.count` | Every decision completes (dimension: decision_type) | Count |
| `agent.memory.context_bytes` | Every reasoning call | Bytes |
| `agent.memory.context_pct` | Every reasoning call | Percent (of model window) |
| `agent.memory.compaction_duration_ms` | Live or batch compaction runs | Milliseconds |
| `agent.llm.input_tokens` | Every Bedrock call | Count |
| `agent.llm.output_tokens` | Every Bedrock call | Count |
| `agent.llm.cost_usd_est` | Every Bedrock call | Dollars (estimated from model pricing) |
| `agent.a2a.sent.count` | Every A2A send (dimension: message_type, target_agent_type) | Count |
| `agent.a2a.receive.latency_ms` | Every A2A receive→process | Milliseconds |
| `agent.error.count` | Any error caught in Agent loop (dimension: error_type) | Count |

Broker Agent additionally emits:

| Metric | When | Unit |
|---|---|---|
| `broker.intent.received.count` | Every trade-intent received (dimension: source_strategy) | Count |
| `broker.intent.approved.count` | Pass deterministic checks | Count |
| `broker.intent.rejected.count` | Fail checks (dimension: rejection_reason) | Count |
| `broker.alpaca.latency_ms` | Each Alpaca call | Milliseconds |
| `broker.alpaca.error.count` | Alpaca errors (dimension: error_code) | Count |
| `broker.halt.active` | Gauge (0 or 1) | Binary |

Services (Market Data Subscriber, Dashboard, Platform Supervisor) emit their own shape; at minimum a heartbeat, request count, error count.

## Token telemetry

LLM tokens are the single biggest variable cost. Every Bedrock call emits:

```
{
  "agent_id": "...",
  "org_id": "...",
  "agent_type": "strategy",
  "strategy_id": "...",
  "model": "claude-sonnet-4-6",
  "input_tokens": 1234,
  "output_tokens": 567,
  "cost_usd_est": 0.0123,
  "decision_trace_id": "...",
  "_aws": { "CloudWatchMetrics": [...] }
}
```

### Aggregation

A scheduled Lambda (runs every 15 min) rolls individual call metrics into DynamoDB summary items:

```
TOKEN#{org_id}#{date}                    — daily org total
TOKEN#{org_id}#{agent_id}#{date}          — daily per-agent total
TOKENEVENT#{org_id}#{ts}                  — raw per-call record (24h TTL)
```

The 24h TTL on raw events keeps costs bounded. Daily aggregates are kept indefinitely for historical billing.

### Dashboard surfacing

The Token Telemetry widget shows:

- Today's token burn vs. org budget (if budget configured)
- Per-agent breakdown for the current day
- 30-day trend
- Outliers: agents that spiked > 2σ above their usual daily consumption

Correlation with trading outcomes is a v2 concern (TOKEN × PnL chart).

## Health checks

Every Agent exposes a heartbeat. Platform Supervisor polls at 60s interval. Failed heartbeat → three retries over 3 minutes → mark Agent stuck, alert sysadmin, surface in dashboard.

Health check contract:

- **heartbeat message** returns within 5 seconds or is declared failed.
- **heartbeat-ack payload** includes:
  - `status`: `healthy` | `degraded` | `error`
  - `current_activity`: short string describing what the agent is doing (memory.flush, reasoning, idle, etc.)
  - `last_decision_at`: timestamp
  - `memory_file_cursor`: bytes into today's memory file
  - `queue_depth`: pending A2A messages inbound
  - `errors_last_hour`: count, for trend
- A degraded status is flagged in dashboard but doesn't page; an error status pages sysadmin.

## Dashboard graphs

Phase 1 widgets (minimum viable observability):

- **Platform health**: Agent count per type, stuck agents count, error rate across platform.
- **Per-org token usage** (line): last 24h, last 7d, last 30d.
- **Per-agent decision latency** (heatmap): p50 / p95 / p99 over time.
- **Broker throughput** (line): intents received, approved, rejected per org.
- **Halt state** (binary indicator): current state per org, history of last 30 days.
- **Deploy markers** (annotations on all graphs): every CI deploy overlays a vertical line.

Phase 2 widgets (product-facing):

- **Agent reasoning cost breakdown**: tokens-by-decision-type per strategy.
- **Trade outcome vs. reasoning effort**: were expensive reasoning sessions more likely to trade? Did they outperform?
- **Memory health**: context utilization % per agent over time, compaction frequency.

Phase 3 (far future):

- **Decision replay**: pick a past decision, load the A2A trace + memory file + market data refs, render a timeline.

## Per-strategy detail page

Distinct from the fleet-level graphs above. Clicking a strategy card on
the Strategies tab opens a **detail view** for that specific bot. The
existing edit form is reachable from the detail view via an explicit
"Edit strategy" button — the first click on a card is no longer a
direct entry into the edit form.

### Tabs

1. **Overview** — the operator's default "what is this bot doing?" view.
   - Header: status, service state, last heartbeat age, `current_activity`
     from the heartbeat payload, active model, watched symbols, current
     equity + realized PnL.
   - **Thought log** (primary panel): rendered agent memory markdown
     for the selected date range, newest-first. `[MARKETDATA#...]`,
     `[LEDGER#...]`, and `[DECISION#...]` pointers render as inline
     references; a future iteration may make them clickable.
   - Recent decisions: one row per decision (action, symbol, quantity,
     rationale, latency, token cost). Derived from the memory file's
     decision lines to avoid a second persistence hop.
   - Tool calls: recent `tool.call.count` events with tool name,
     symbol, outcome (`cache_hit`, `quota_exceeded`, `ok`, etc.).
     Pulled via CloudWatch Logs Insights, scoped to this bot.
   - Open positions + ledger summary.

2. **Prompt** — read-only view of the last prompt the agent saw.
   - Full rendered **system prompt** (base framing + composed skills +
     strategy markdown).
   - Full rendered **user prompt** (the `_DECISION_PROMPT_TEMPLATE`
     output, i.e., market data + calendar + TA + portfolio state +
     recent decisions block as it was supplied to the LLM).
   - A "last rendered at `<ts>`" line so operators know this is a
     snapshot, not static.
   - Written by `StrategyBot.decide` each tick as a single DDB row
     `PROMPTSNAPSHOT#{bot_id}` (overwritten in place). Best-effort —
     a prompt-snapshot write failure never interrupts the decide path.

3. **Self-critique** — existing data, collected here for this bot:
   - Lessons stream from `lessons.md`.
   - Proposals list (pending / applied / rejected) with apply + reject
     buttons gated on the existing UPDATE authz.

### Time range

A single date-range picker at the top of the Overview tab drives
thought log + decisions + tool calls. Default: **today UTC**. No
presets — operators type the range they need. Prompt tab has no range
control (it's always the current snapshot); Self-critique tab shows
full history.

### Backend endpoints

- `GET /api/strategies/{sid}/memory?start=YYYY-MM-DD&end=YYYY-MM-DD`
  — returns raw memory markdown joined for the day range. Authz: READ
  on the Strategy resource.
- `GET /api/strategies/{sid}/tool-calls?start=...&end=...` — queries
  CloudWatch Logs Insights over the trading log group filtered on
  `strategy_id = <bot_id>`. Authz: READ.
- `GET /api/strategies/{sid}/prompt-snapshot` — one DDB read.
  Authz: READ.

Decisions are derived client-side from the memory markdown; no
dedicated endpoint. Open positions come from the existing ledger
summary path.

### Non-goals for v1 of this page

- Decision replay (Phase 3 above) — out of scope; the data pointers in
  the thought log are enough to reconstruct manually.
- Streaming updates. Page fetches on load + a manual refresh button.
  SSE can come later if the view becomes the operator's always-on
  surface.

## Metric query API

Dashboard calls `/api/metrics/query` which wraps CloudWatch `GetMetricData`. Query parameters:

- `metric`: dotted path (`agent.llm.output_tokens`)
- `dimensions`: filters (`org_id=X`, `agent_type=strategy`)
- `time_range`: start and end
- `period`: aggregation bucket (60s, 5m, 1h)
- `stat`: `Sum` / `Average` / `p95` / etc.

Authz on this endpoint:

- **sysadmin** queries any dimension (subject to `SYSADMIN_CAN_READ_ORG_DATA` for non-platform dimensions)
- **orgadmin** queries within their org's dimensions only; cross-org pivots denied
- **operator / viewer / auditor** query only their own strategies' metrics

The authz check runs BEFORE the CloudWatch call — we don't pay for cross-org queries we're about to reject.

## Audit log

A2A envelope metadata, IAM role assumptions, break-glass actions, secret reads, and Agent status transitions are logged to a dedicated CloudWatch log group `trading-strands-audit` with **structured fields only** — no payloads, no PII, no strategy-content.

Retention: 90 days hot, archived to S3 Glacier thereafter, kept 7 years (compliance expectation for financial applications).

Access to the audit log is sysadmin-only in v1. Eventual plan: scoped audit views per org, read-only to orgadmin for their org's entries.

## Alerting

> **Status (2026-04-27):** alarms are defined in CDK and the dashboard
> surfaces their state on the Supervisor panel, but `actions_enabled=False`
> on all three. SNS wiring is deferred pending an operator decision on the
> alert destination (email vs PagerDuty vs Slack webhook). The dashboard's
> `/api/supervisor/alarms` endpoint flags `actions_enabled=False` so a
> silent-but-firing alarm is visible on the UI — that's the v0 mitigation
> until a destination is chosen. When the destination is picked, the CDK
> stack gets an `alarm_action_topic` parameter and each MetricAlarm gets
> `.add_alarm_action(topic)`; nothing else in the code path changes.

Minimum alerts that page sysadmin (via SNS → future integration with PagerDuty or equivalent):

- Broker Agent down for >2 minutes
- Broker Agent accepting trades while halt=true (security invariant violation)
- Any Agent stuck >10 minutes without heartbeat-ack
- Any failed deploy that leaves stack in inconsistent state
- Cost anomaly: daily token spend >3x the 7-day average
- A2A identity mismatch (any occurrence)

Non-paging but dashboard-visible:

- Strategy Agent error rate >5%/min
- Compactor Lambda failure
- S3 bucket empty-on-delete hanging >24h
- Any audit-log gap (missing sequence number)

## Cost accounting

Per-Agent cost is composed of:

- **Fargate cost** (if Fargate-deployed): hours running × vCPU/memory rate. Tagged `AgentId`.
- **Lambda cost** (if Lambda-deployed): invocations + duration. Tagged `AgentId`.
- **S3 cost**: storage + requests on the Agent's bucket. Per-bucket billing.
- **DynamoDB cost**: per-request, tagged via `AgentId` attribute on every write (we own this).
- **Bedrock cost**: estimated via EMF `agent.llm.cost_usd_est`, aggregated to daily.
- **Data egress**: too small to track at Agent granularity in v1.

Dashboard "Cost per Agent" widget aggregates these into a single $/day figure per Agent. Sysadmin view shows all orgs; orgadmin sees only their own.

Known gap: Bedrock cost from `cost_usd_est` is *our estimate*, not AWS's bill. Reconciliation with AWS bill happens monthly; discrepancies are a known early-stage concern, expected to converge.

## Open questions

- **Metric cardinality**: `source_strategy` dimension on `broker.intent.received.count` explodes per-strategy. CloudWatch has a 30-dimension-value default limit per metric; might need to split or aggregate. Revisit at 10+ strategies per org.
- **Anomaly detection**: simple threshold alerting in v1. Later: CloudWatch Anomaly Detection on token spend, decision latency, broker error rate.
- **Dashboard update rate**: CloudWatch metric latency is ~1-2 min from emission. Users looking for sub-minute fidelity need to wait for streaming metrics (v2).
