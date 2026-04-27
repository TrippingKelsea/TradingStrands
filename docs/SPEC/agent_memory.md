# Agent Memory

**Status:** Target (v1). Not yet implemented.
**Last updated:** 2026-04-26

> Today's Strategy Bot has only an in-process `_recent_decisions` list (cap 10)
> and loses everything on task restart. The durable ledger is in-memory and
> reconstituted-from-scratch each morning — a known gap blocking the
> scale-down scheduler from being safe across nightly cycles. This doc is
> the target model. The durable ledger (item 1 below) is the most urgent
> subset, because without it the reconciler starts from zero every day.

## Premise

Every Agent needs memory. Ephemeral in-process state is insufficient because:

- Agents restart daily (wake/sleep schedule, deploys, occasional Fargate task replacement).
- The chat feature requires the Agent to explain decisions it made yesterday.
- The Self-Critique Agent needs to read a full week of a Strategy Agent's reasoning.
- The durable ledger needs a stable external store (the Strategy Agent can't be the only copy).

Memory is per-Agent. An Agent cannot read another Agent's memory.

- **v1 target:** isolation is enforced by IAM. Each Agent has its own S3 bucket (or a dedicated prefix under an org bucket, if we've migrated off per-Agent buckets per the bucket-limit note below), and the Agent's IAM role grants access only to that resource. Cross-Agent reads are physically impossible even if an Agent's code had a bug that tried.
- **v0 reality:** isolation is enforced at the application layer by `AgentMemoryStore`'s prefix derivation. The store is constructed with `(org_id, agent_type, agent_id)` and derives every S3 key from that scope — callers don't construct keys. All bots share a single IAM role with access to the bucket, so a *compromised or buggy* bot code path could theoretically read across prefixes. The v0-to-v1 migration is the IAM hardening step; the application-layer scoping is already in place so v1 rolls out without data reshape.

Both layers are defense-in-depth when v1 lands. The rule "no Agent reads another Agent's memory" holds in v0 because only the store constructs keys and the store is constructed per-Agent; it hardens in v1 when IAM refuses the cross-Agent request at the AWS layer.

## Storage model

### Per-Agent S3 bucket

Each Agent gets a dedicated S3 bucket at deploy time. Naming:

```
trading-strands-agent-{agent_type}-{org_id}-{agent_id}
```

Example: `trading-strands-agent-strategy-741e1aeb-49cf7c5f`

Bucket limits (100/account default) are a real constraint. Mitigations in `deployment.md`.

### Layout within the bucket

```
/YYYY-MM-DD.md              — daily raw memory, append-only during the day
/YYYY-MM-DD.compressed.md   — end-of-day compacted memory, optimized for weekly reads
/lessons.md                 — append-only, cross-day insights the agent wants preserved
/recommendations.md         — (Risk/Compliance/Auditor only) advisories for orgadmin
```

The raw daily file is **never destroyed**. Compression produces a sibling file; it does not replace the original. This is an audit requirement.

## The daily file

### Structure

A single markdown file with three agent-populated sections:

```markdown
# 2026-04-26 — Strategy Agent momentum-spy

## Actions
- 09:31 ET — submitted BUY intent for SPY @ market. Rationale: RSI crossed 30
  on 15m chart while VIX was falling. Ref: [MARKETDATA#SPY#2026042609#09:29-09:31]
  [MARKETDATA#VIX#2026042609#09:29-09:31]. Broker approved, filled at 521.43.
- ...

## Considerations
- 10:05 ET — considered adding QQQ long on correlated strength but passed.
  Position concentration would have hit 40%. Ref: [LEDGER#momentum-spy#2026042610].
- ...

## Projections
- End of day: if SPY holds above 520 through 15:30, plan to hold overnight.
  Re-evaluate tomorrow morning based on overnight futures.
```

Sections are guidance, not a schema. If the LLM writes cross-sectionally, that's fine — the headers are for downstream readers' skimming, not for the agent's own structure.

### Anti-confabulation rule

Every concrete market-state claim in memory **must** carry a DynamoDB pointer:

```
[MARKETDATA#<symbol>#<yyyymmddhh>#<mm:ss-mm:ss>]
[LEDGER#<bot_id>#<yyyymmddhhmm>]
[DECISION#<bot_id>#<ts>]
```

Unreferenced claims about "the market was choppy" are discouraged in the system prompt. The agent either has the data (with a ref) or says it doesn't. This makes chat answers and weekly reviews trustworthy rather than plausible-sounding fiction.

The system prompt language that enforces this lives in [agents.md](./agents.md#self-driven-loops-vs-ticks) and is non-negotiable in every Agent's prompt.

## Compaction

Two distinct compactions happen, and keeping them separate matters:

### Live compaction (intra-day)

As the agent's working context approaches its window limit during the day, the agent folds older appended blocks into a rolling summary **within the same daily file**. This is driven by the Agent's own system prompt ("at X% context, summarize the earlier part of today and continue").

- The raw appends that were summarized are **not deleted from the file** — the summary gets prepended or inserted, and the raw appends remain below it with a marker.
- The agent loads from the file the summary + recent unsummarized appends, skipping the bulk-summarized raw text for its own working prompt.
- For downstream readers (weekly critique, chat), the raw appends are still there to be loaded.

This is the mechanism you described: the Agent manages its own working window, and the `load_day` retrieval skill can pull the full raw content back when needed.

### Batch compaction (end-of-day)

Separate from live compaction, a **Compactor Lambda** runs at end-of-day per Agent. It:

- Reads the full raw daily file
- Writes a `YYYY-MM-DD.compressed.md` optimized for **downstream readers** (tomorrow-the-agent, the weekend Self-Critique Agent, the chat feature)
- Uses a different system prompt than live compaction: "summarize today's decisions and their reasoning for an audience of future self, auditors, and critics. Preserve the decision trace. Drop fine-grained tick-level reasoning."

Why separate jobs, not just trust live compaction:
- Live compaction targets "what's relevant to my next tick." Batch compaction targets "what's useful to a week-later reader." Different summaries.
- Live compaction happens under time pressure during trading. Batch compaction runs idle, can use a larger model or take longer.
- If the agent crashes mid-day, live compaction state may be inconsistent; batch compaction is idempotent — reads raw file, writes compressed file.
- The weekly-read loader defaults to compressed files; raw is available on demand.

The Compactor Lambda uses STS assume-role to operate on the target Agent's bucket (see [deployment.md](./deployment.md#per-bot-iam-with-lambda-operators)).

## Lessons file

A single `lessons.md` per Agent, append-only across the Agent's lifetime.

Content: insights the agent believes are worth preserving beyond a week. Example entries:

```markdown
## 2026-04-17
Post-FOMC opens tend to chop for ~20 min before direction sets in.
Waiting until 10:00 ET before sizing up on Fed-day bull cases has been
better than committing at 09:30.
Source: decisions 2026-04-17 09:30-10:00 [DECISION#momentum-spy#1776970200+]

## 2026-04-23
RSI signals on earnings-day pre-market have been unreliable —
5 signals this quarter, 1 held, 4 reverted by 10:30.
Source: decisions 2026-04-03 2026-04-14 2026-04-21 2026-04-22 2026-04-23
```

### Append-only across prompt edits

A lesson written under one version of the strategy prompt **stays** even if the prompt later changes. The audit trail — "what did this agent *think* it had learned" — is the point. A user who edits the strategy and wants a fresh start can create a new strategy; the old lessons stay with the old agent.

The Agent's system prompt must not authorize lesson deletion. Only via direct S3 manipulation by an orgadmin or sysadmin with explicit intent can lessons be removed, and that action must produce an audit-log entry.

## Default working context on each reasoning call

When the Agent builds its prompt for a reasoning call, the default working context includes:

1. **The strategy prompt** (immutable user-authored markdown)
2. **`lessons.md`** (full; it's deliberately small and relevant)
3. **Last 5 trading days' compressed memory files** (`.compressed.md`)
4. **Today's rolling memory** — whatever the live-compaction window has kept visible
5. **The A2A tool schema** — what messages it can send, who's listening

Budget for this: aim for <30% of the model's context window, leaving the rest for the current decision's reasoning + reply. If (3) alone exceeds budget, the agent falls back to (3) = last 3 days; if still over, last 1 day + the rest stays loadable via `load_day`.

Every reasoning call logs its actual context-byte usage as a CloudWatch EMF metric.

## Retrieval skill: `load_day`

The Agent has a tool `load_day(date: str, compressed: bool = True) -> str` that returns the contents of a specific past day's memory file. Used when:

- The current working context doesn't contain what the agent needs to answer
- Chat feature invoked by a user requests a specific date's reasoning
- Self-Critique Agent pulling arbitrary weeks

The tool is IAM-scoped — it can only read this agent's own bucket. Cross-agent retrieval is not possible.

## Shared memory — explicitly not a thing

There is no "platform memory" or "shared wisdom pool." Each Agent has its own memory. Self-Critique Agents read their target Strategy Agent's memory via STS assume-role scoped to that specific target — never a group.

If in the future we want cross-strategy learning (an insight from strategy A helping strategy B), it must be mediated by a human or an orgadmin-gated workflow. Strategies do not leak memory sideways.

## Durable ledger

The ledger is a special case of memory that needs to live outside the markdown narrative because it's queried structurally, not read narratively.

**Where it lives:**

```
DDB: LEDGER#{bot_id} — current state (positions, cash, high water mark, open orders)
DDB: LEDGER_EVENT#{bot_id}#{ts} — append-only event log (fills, fees, partials)
S3 bucket /ledger/YYYY-MM-DD.json — daily snapshot, written end-of-day by Compactor
```

**Why DDB AND S3:**

- DDB is the hot-path source of truth during the trading day (fast reads for the Strategy Agent, the Broker Agent, the Auditor)
- S3 snapshots preserve historical states for weekly critique and compliance archival (cheap, immutable)

**Reload on Agent restart:**

- Strategy Agent on wake: reads `LEDGER#{bot_id}` from DDB, reconstructs its working model. Does NOT replay the event log (slow); relies on the snapshot + event log for audit.
- Auditor Agent: reads DDB + compares against broker API + against S3 snapshots for drift detection.

The v0 Reconciler in the codebase becomes the Auditor Agent when we get there. The durable-ledger migration is a prerequisite blocker — a Reconciler that starts from zero every morning is not a reconciler.

## Open questions

- **Memory versioning**: if an Agent's system prompt changes materially, are old memories still valid context? Default: yes, always include them, the Agent reasons about its own history. Revisit if this produces bad behavior.
- **Pruning**: never. Raw daily files stay forever (S3 Glacier tier after 90 days, maybe). Cost is cheap.
- **Memory-as-code**: could an Agent ever treat its own memory as executable (e.g., "these rules I wrote yesterday now govern today")? Out of scope for v1; the strategy prompt is the only executable.
