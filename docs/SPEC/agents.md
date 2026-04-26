# Agents

**Status:** Target (v1). Not yet implemented.
**Last updated:** 2026-04-26

> The code today runs a single trading-service task that hosts all bots,
> a Trade Coordinator that serializes broker calls, and a deterministic
> Risk Manager in that same process. This doc describes the target v1
> shape: one Agent per task, Broker Agent as per-org chokepoint, AgentCore
> A2A for inter-agent messages. See `CLAUDE.md` for the v0 → v1 mapping.

## Definition

An **Agent** is the atomic unit of autonomous work in TradingStrands. Every Agent:

- Is backed by an LLM (the whole point; no LLM = not an Agent, it's a service)
- Has a durable memory store it owns exclusively (see [agent_memory.md](./agent_memory.md))
- Has a unique identity and IAM role scoped to its data
- Exposes a health check + heartbeat (see [observability.md](./observability.md))
- Communicates with other Agents over A2A (see [agent_communication.md](./agent_communication.md))
- Is deployed as either a Fargate task (long-running, stateful) or a Lambda (event-triggered, stateless) (see [deployment.md](./deployment.md))

Plumbing that doesn't reason (the market data subscriber, the provisioner, the EventBridge scale dispatcher) is **not** an Agent. Those are services. The distinction matters because only Agents have memory, token budgets, and heartbeats — services don't.

## Agent types

### Strategy Agent

- **One per active strategy** (per-bot, per-org). A user creates strategy X in org Y → BotProvisioner stands up a Strategy Agent for it.
- **Self-driven loop** during its active window. Observes the market, reasons about its prompt, emits trade intents via A2A when it decides to act. The orchestrator's tick is a synchronization point, not a schedule; the agent does **not** wait to be told to think.
- **Cannot trade directly.** Every trade intent flows through the Broker Agent (see below). The Strategy Agent has no broker credentials and no broker IAM.
- **Can read market data** from the platform market data subscriber (DynamoDB + optional A2A push). Can also load its own past-memory days on demand.
- **Lifecycle**: pre-market warmup → market hours → post-market reflection → `ready-for-turndown` → turndown.

### Broker Agent

- **One per org.** Always-on Fargate task. Never a Lambda — cold starts unacceptable in the trade path.
- **The single chokepoint for all broker calls.** No other Agent may call Alpaca directly. If the Broker Agent is halted, no trade happens — this is the property that makes the kill switch work.
- **Deterministic guardrails embedded in the intake path.** Before any trade intent reaches the broker API, it is checked against:
  - Deterministic Risk Manager rules (position caps, PDT, per-trade size limits, drawdown thresholds)
  - Deterministic Compliance rules (hard-coded regulatory checks)
  These checks are **code, not LLM calls**. They are in the hot path and must be fast and boring.
- **LLM layer is thin.** It reasons about intake queue behavior (retries, error reporting back to the submitting Strategy Agent, rate-limit etiquette). It does **not** decide whether trades happen — the deterministic checks do. The LLM layer behavior is intentionally under-specified in v0; let patterns emerge before codifying them.
- **Owns the Alpaca credentials** for its org (see secret path in deployment.md).
- **Honors halt signals** as the single enforcement point. When halted, all intents are rejected with a clear reason; Strategy Agents keep emitting (that's fine) but nothing reaches the broker.

### Risk Agent

- **One per org**, deployment mode (Lambda on-demand vs Fargate always-on) selected by orgadmin via a per-org switch (see [deployment.md](./deployment.md)).
- **Does not gate individual trades** — that's the Broker Agent's deterministic code.
- **Reviews patterns over time.** End-of-day and end-of-week reflection on drawdown trajectories, concentration risk, cross-strategy correlation. Produces `recommendations.md` entries in its own memory bucket.
- **Recommendations are human-consumable.** An orgadmin reviews them and may choose to adjust the deterministic Risk Manager's config. The Risk Agent itself does not mutate the deterministic config.

### Compliance Agent

- Same shape as Risk Agent: per-org, deployment mode switchable, does not gate individual trades.
- **Reviews strategy drift.** Is the Strategy Agent's behavior consistent with its stated mandate? Flags when a "long-only value" strategy starts shorting tech.
- **Reviews regulatory posture.** PDT violations, wash sale patterns, cross-org leakage (shouldn't happen but auditable).
- Output is also recommendations to orgadmin, same pattern.

### Auditor Agent

- Same deployment shape (per-org, switchable).
- **Reconciles ledger against broker.** Periodically pulls broker-side positions and compares to the durable ledger. Flags drift.
- **Has authority to halt the desk** if drift exceeds threshold (already implemented in v0 Reconciler; Auditor Agent is the agentified evolution).
- Crucially: the halt signal it emits flows through the Broker Agent, same as any other halt source.

### Self-Critique Agent

- **Per Strategy Agent**, but invoked weekend-only via scheduled Lambda.
- **Reads the Strategy Agent's week of memory** + referenced market data + the ledger.
- **Writes `lessons.md`** and/or proposes edits to the strategy prompt (delivered as a recommendation to the author, not applied automatically — authorship rules from [multi_tenancy.md](./multi_tenancy.md) apply).
- **This is the "weekend mode"** referenced in `CLAUDE.md` — not backtesting, not simulation. Self-reflection on what actually happened, using recorded real market data.

### Platform Supervisor

- **Renamed from "Orchestrator"** in v1. The old name implied it ran the trade loop; it does not.
- **One instance platform-wide.** Always-on Fargate.
- **Supervises all Agents across all orgs.** Monitors heartbeats, flags stuck or missing Agents, alerts sysadmin on anomaly, coordinates scheduled events (wake, turndown, compaction kickoff).
- **Does not place trades.** Does not reason about strategies. It's a traffic cop + health monitor + scheduler driver.
- **Honors the break-glass switch** for forced mode transitions outside normal market boundaries.

## Agent lifecycle

Strategy Agents specifically follow this lifecycle, because they're the only Agent type with a natural start/stop around market hours. Other Agents have simpler lifecycles (Lambda agents are created on invocation; always-on Fargate agents run continuously).

```
  strategy.created (DDB item appears)
          │
          ▼
   BotProvisioner:
    - creates IAM role
    - creates S3 memory bucket
    - creates ECS task definition
    - registers wake/sleep schedule
          │
          ▼
   status: draft     ←── user iterates / tests prompt; no task runs
          │
          ▼ (user activates)
   status: active
          │
          ▼ (next market open)
   Agent wakes ──► pre-market warmup
          │
          ▼
   market hours:
     self-driven loop reasoning + A2A trade intents to Broker Agent
          │
          ▼ (market close)
   post-market reflection:
     agent updates its daily memory file, flags ready-for-turndown
          │
          ▼
   (turndown cron verifies no in-flight work, then stops the task)
          │
          ▼
   (next day: wake schedule fires again)
```

Pause and stop states:

- `paused`: task scaled to 0. Memory preserved. Re-activation resumes from last memory state. No cold-start penalty beyond the Fargate task start time.
- `stopped`: task scaled to 0, considered inactive long-term. Memory preserved for audit.
- `deleted`: Deprovisioner tears down all AWS resources (task def, IAM role, bucket). Audit trail retained in DynamoDB for compliance (see multi_tenancy.md for retention rules).

## The trade-gating invariant

This is the most important rule in this document:

> **Every trade intent, regardless of which Strategy Agent produced it, passes through the Broker Agent's deterministic Risk + Compliance checks before reaching the broker API. No agent may call the broker directly. The Broker Agent is the single chokepoint; halt, rate limits, and kill switches operate here.**

Consequences this rule preserves:

- Every trade hits the deterministic checks, always. Bypassing is structurally impossible — no other Agent has broker credentials.
- "Halt the desk" means "set Broker Agent's halt flag." A single place to cut the cord.
- Rate limits and PDT rules are enforceable because all intents queue through one Agent.
- Audit trails are complete: every trade has a Broker Agent decision recorded.

"Deterministic code in the hot path" (from CLAUDE.md) now specifically means the deterministic Risk + Compliance code **inside the Broker Agent's intake**. The rule did not go away — it moved to a more coherent home.

## Self-driven loops vs. ticks

Strategy Agents (and, when in Fargate mode, Compliance / Risk / Auditor) run continuous self-driven loops during their active windows. What this means concretely:

- The agent's main loop is **its own scheduler** for reasoning work: pre-market, mid-session reflection, post-market reflection, memory housekeeping.
- The agent **may decide to trade at any moment** during market hours. Trade decisions are not gated on an orchestrator tick. Trading latency is a function of (agent reasoning speed) + (A2A round-trip) + (Broker Agent deterministic check speed), not of any supervisor's tick cadence.
- The Platform Supervisor sends ticks for **synchronization and heartbeat**. An agent that fails to ack a tick within N seconds is flagged stuck. The tick does not contain decision input — it's a liveness probe.
- The tick is also the natural moment to propagate platform state changes (halt flag, new market data availability, end-of-day trigger). But the agent's decision loop is free-running between ticks.

## Self-flag for turndown

At end-of-session (after market close + any post-market reflection the agent wants to do), the agent **advises** it is ready for turndown by emitting a `ready-for-turndown` A2A message.

- **Advisory, not authoritative.** The turndown Lambda independently verifies safety before actually stopping the task: no in-flight trade intents, no pending A2A responses, no open broker orders via the Broker Agent. An agent's ready-flag is "you *may* turn me down"; the turndown Lambda decides whether to.
- **Revocable.** If new work arrives after an agent sets ready-for-turndown, it un-sets the flag.
- **Upper bound.** The turndown cron fires at a configured time (default 9pm ET) regardless of ready-flag state. If an agent hasn't flagged ready by then, the turndown Lambda logs a warning and turns it down anyway — infra capacity isn't held hostage by a confused agent.

## Health checks

Every Agent must expose:

- A **heartbeat** (A2A message or HTTP endpoint, TBD in [agent_communication.md](./agent_communication.md)) that the Platform Supervisor polls. Stale heartbeat → agent flagged for investigation.
- A **current-state summary**: what it's currently doing, last decision timestamp, memory file cursor, queue depth. Surfaced in the dashboard's per-agent view.
- An **error-state report**: if the agent has detected something wrong with itself (memory corruption, missing data, unable to reach Bedrock), it reports it here rather than silently misbehaving.

Health checks are a requirement, not an optional observability feature. An Agent without a health check is not a valid deployment.

## Open questions deferred from this doc

- Exact structure of the A2A message types (trade intent, recommendation, halt signal, heartbeat) → [agent_communication.md](./agent_communication.md)
- How Broker Agent's LLM layer evolves beyond "thin" → deferred; let patterns emerge
- Cross-org Platform Supervisor scaling → single instance is fine at current scale; revisit if we hit limits
