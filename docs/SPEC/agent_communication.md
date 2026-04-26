# Agent Communication

**Status:** Draft
**Last updated:** 2026-04-26

## Protocol: AgentCore-native A2A

All inter-Agent messages use the A2A (Agent2Agent) protocol as implemented by AWS Bedrock AgentCore. We commit to AgentCore as the runtime because:

- Strands and AgentCore are tightly coupled; using a different runtime forfeits benefits we already pay for.
- A2A specifies task lifecycle, artifact passing, and streaming updates in a way EventBridge-based ad-hoc schemas don't.
- Identity + provenance on messages is a native concern in AgentCore, not a bolt-on.

Trade-off accepted: AgentCore runtime coupling. We will not swap message transports later without meaningful work. Justified by simpler-right-now vs. portability-in-the-abstract.

## Identity and provenance

Every A2A message carries:

- **Sender identity** — the IAM role ARN of the Agent sending. AgentCore provides this via SigV4 signing; receivers verify.
- **Logical agent ID** — our domain ID (e.g., `strategy/org-741e1aeb/momentum-spy`). Redundant with IAM role in correct state, but carried in the envelope for human debugging.
- **Task ID** — A2A-native, groups a request/response pair.
- **Trace ID** — correlates across multiple agents in a single trade-decision flow. Reused in CloudWatch log fields for cross-agent tracing.

**Verification rule:** every receiving Agent validates that (IAM role ARN) matches the (logical agent ID) pattern expected for the claimed sender type. A message claiming to be from the Broker Agent but signed by a Strategy Agent's IAM role is rejected and logged as a security event.

## Message flow: trade intent

The trade-gating invariant from [agents.md](./agents.md) maps to a specific message flow:

```
┌─────────────────┐                    ┌──────────────────┐
│ Strategy Agent  │                    │   Broker Agent   │
│ (org-X, bot-Y)  │ ─── A2A: submit ──►│   (org-X)        │
└─────────────────┘    (trade-intent)  └──────────────────┘
                                              │
                                              ▼
                                       Deterministic Risk check
                                       Deterministic Compliance check
                                              │
                              ┌───────────────┴────────────────┐
                              │                                │
                              ▼                                ▼
                        REJECT (back to                 APPROVE →
                        Strategy Agent                  Alpaca API call
                        with reason)                    → fill
                                                              │
                                                              ▼
                                                       Write LEDGER_EVENT
                                                       Write DECISION trace
                                                              │
                                                              ▼
                                                       A2A: filled reply
                                                       back to Strategy Agent
```

Key properties this flow preserves:

- Strategy Agent never holds broker credentials; the IAM condition on its role blocks `alpaca:*` and blocks writes to `LEDGER#*`.
- Broker Agent is the only thing with broker credentials, and its intake contains the deterministic checks.
- Every trade intent produces a `DECISION` trace item in DynamoDB (see [agent_memory.md](./agent_memory.md) for pointer format). This trace is what chat and audit later read.
- Rejection propagates back to the Strategy Agent so its memory file can record "intent rejected: reason X" — anti-confabulation applies.

## Message types (v0 surface)

Small on purpose. Add more only when a concrete need justifies it.

| Message | From | To | Purpose |
|---|---|---|---|
| `trade-intent` | Strategy Agent | Broker Agent (its org) | Propose a trade |
| `trade-result` | Broker Agent | Strategy Agent (originator) | Filled / rejected / partial |
| `halt-signal` | Risk / Compliance / Auditor / sysadmin | Broker Agent (its org) | Stop accepting intents |
| `resume-signal` | Same | Same | Resume after halt |
| `heartbeat` | Platform Supervisor | All Agents | Liveness ping |
| `heartbeat-ack` | Any Agent | Platform Supervisor | Liveness response + state summary |
| `ready-for-turndown` | Any Agent in Fargate | Platform Supervisor | Advisory ready signal |
| `revoke-turndown` | Same | Same | Un-set ready flag |
| `market-data-tick` | Market Data Subscriber | Subscribed Strategy Agents | Push update (optional, pull-from-DDB also supported) |
| `recommendation` | Risk / Compliance / Auditor | Org's dashboard / orgadmin inbox | Human-consumable advisory |

## Synchronous vs. asynchronous

- **`trade-intent` / `trade-result`**: synchronous request/reply from the Strategy Agent's perspective. The Strategy Agent awaits the Broker Agent's response before deciding what to record in memory.
- **`halt-signal`**: fire-and-forget, but with an `ack` expected within a short window. Platform Supervisor logs and alerts if no ack.
- **`heartbeat` / `heartbeat-ack`**: synchronous — the Supervisor waits for ack with a timeout.
- **`recommendation`**: fire-and-forget; stored to S3 / DynamoDB on the receiver side.
- **`market-data-tick`**: push model, fire-and-forget. Agents may also pull from DynamoDB.

## Market data push vs. pull

Both supported, as agreed earlier:

- **Push** via A2A (`market-data-tick`) is the default for Strategy Agents during trading hours. Lower latency.
- **Pull** from DynamoDB is always available. Used by:
  - Agents loading historical data for memory refs
  - The Compactor / Self-Critique Lambdas (which aren't online to receive pushes)
  - Any Agent that explicitly unsubscribes from push to reduce wake-ups

A Strategy Agent subscribes to push at startup for the symbols in its strategy config. If the Market Data Subscriber crashes or restarts, the Agent falls back to pulling from DynamoDB until push is restored.

## Halt propagation

Single chokepoint, multiple signal sources:

```
Risk Agent ────┐
               │
Compliance ────┤
               │   halt-signal    ┌──────────────┐
Auditor ───────┼──────────────────►│ Broker Agent │
               │                  │   (org X)    │
Sysadmin ──────┤                  └──────────────┘
               │                       │
Strategy self-halt ─┐                  ▼
                    │              set halt flag
(e.g., on stale ────┘              (DDB: CONTROL#org-X)
 market data)                          │
                                       ▼
                              all subsequent trade-intents
                              rejected with reason "halted by {source}"
```

- Strategy Agents continue to emit intents during halt — they don't know/care that they're halted from their own perspective. The Broker Agent enforces.
- The halt flag is **persisted to DynamoDB** so a Broker Agent restart still sees it. Halts survive Fargate task replacement.
- The Auditor Agent periodically verifies the halt flag state against the Broker Agent's behavior. A Broker Agent that's accepting trades while halt=true is a critical alert.

## Recommendations flow (Risk / Compliance / Auditor → orgadmin)

These agents produce advisory output, not direct action. The flow:

```
Agent (Risk / Compliance / Auditor)
  │
  ▼
Writes to its own S3 bucket: /recommendations.md (append)
  │
  ▼
Sends A2A `recommendation` message to the platform's Recommendation Aggregator
  │
  ▼
Recommendation Aggregator writes to DynamoDB: RECOMMENDATION#{org_id}#{ts}
  │
  ▼
Dashboard surfaces under "Org Advisories" for orgadmins
  │
  ▼
Orgadmin reviews, optionally updates deterministic Risk config or strategy prompt
  │
  ▼
Action captured in audit trail
```

The deterministic Risk Manager's config is **not** mutable by any Agent directly. It's mutable only by a human (orgadmin, sysadmin) through the dashboard. This preserves "deterministic code in the hot path" — the LLM layer can propose changes but cannot apply them.

## Error handling

- **Timeout on synchronous A2A**: the caller's behavior depends on message type. Trade intents time out to "reject, infrastructure failure" and the Strategy Agent writes that to memory. Heartbeats time out to "Supervisor flags the Agent stuck."
- **Malformed message**: receiver rejects with a typed error, logs it, alerts sysadmin. No silent tolerance of malformed A2A — these are a bug in the sender and need to be seen.
- **Identity mismatch**: receiver rejects, logs as security event, alerts sysadmin immediately. This is the only message-handling path that's an alert, not a warning.

## Audit trail

Every A2A message (envelope metadata, not payload for privacy) is logged to CloudWatch via EMF with structured fields: sender_id, receiver_id, message_type, trace_id, timestamp, latency_ms. This is the primary material the Auditor Agent reviews. Retention: 90 days hot, archive to S3 thereafter.

Trade intents and their results are *also* logged as full-payload DECISION items in DynamoDB (see memory spec) — this is stronger than A2A envelope logging because it preserves the full reasoning trace for chat.

## Open questions

- **Batch operations**: if a Strategy Agent wants to submit 10 trade intents atomically (all-or-none), is that a single A2A message with a list, or 10 separate messages with a correlation ID? Defer — no current use case.
- **Streaming A2A**: A2A supports streamed responses. Worth using for long-running Compliance reviews that want to return partial analysis as it completes. Defer to when Compliance reviews get slow enough to care.
