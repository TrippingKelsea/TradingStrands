# Deployment

**Status:** Partially implemented.
**Last updated:** 2026-04-26

> **Implemented now (v0):** CDK stack with ECS Fargate services for trading +
> dashboard, DynamoDB state table, Cognito user pool, per-org Alpaca secrets
> in Secrets Manager, resource tagging for cost attribution, EventBridge
> scale-down for the trading service off-hours, CDK-managed placeholder
> secrets to solve the chicken-and-egg problem with ECS + Secrets Manager.
>
> **Target (v1), not implemented:** per-bot Fargate tasks, per-bot IAM roles
> via STS assume-role from Lambda operators, BotProvisioner + Deprovisioner
> lifecycle Lambdas, blue-green for dashboard + Broker Agent, Scale
> Dispatcher Lambda, per-org Risk/Compliance/Auditor mode switches.
>
> The below describes the target. See [operational_notes.md](./operational_notes.md)
> for the discipline and conventions that apply at both v0 and v1 today.

## Workload classification

| Workload shape | Deployment | Examples |
|---|---|---|
| Long-lived streaming with warm state | Fargate task | Strategy Agent, Broker Agent, Market Data Subscriber, Dashboard, Platform Supervisor |
| Event-triggered, short-running, stateless | Lambda | BotProvisioner, Deprovisioner, Compactor, Self-Critique Agent, Scale Dispatcher, token-usage aggregator |
| Event-triggered, agent with memory, switchable latency | Fargate **or** Lambda (per-org switch) | Risk Agent, Compliance Agent, Auditor Agent |

The classification is not "Strategy-like things are Fargate, everything else is Lambda." It's **usage pattern**:

- Streaming + warm state + hot path → Fargate
- Event + bounded work + cold-start-tolerable → Lambda
- In between → switchable by operator choice

## Container image strategy

All Agents use container-image Lambdas where Lambda is the deployment. This gives us:

- A single Docker build pipeline shared by Fargate Agents and Lambda Agents
- Same `trading_strands` Python package installed everywhere
- Per-Agent-type Dockerfiles that inherit from a base image and override the entrypoint
- ~500ms cold-start penalty vs. zip Lambdas — acceptable

Layout:

```
Dockerfile.base            — Python, uv, trading_strands package, shared deps
Dockerfile.strategy        — FROM base, CMD runs strategy agent loop
Dockerfile.broker          — FROM base, CMD runs broker agent
Dockerfile.compliance      — FROM base, CMD runs compliance agent (Lambda or Fargate)
Dockerfile.compactor       — FROM base, CMD runs memory compactor (Lambda)
...
```

CI builds all images, tags with commit SHA, pushes to ECR. CDK references by tag.

## Per-bot IAM model

**The invariant:** every Agent has a unique IAM role scoped exclusively to that Agent's data.

### Strategy Agent

Role name: `ts-strategy-{org_id}-{bot_id}`

Permissions:

- `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` on its own bucket only (resource-scoped)
- `dynamodb:Query` on `MARKETDATA#*` (shared read access)
- `dynamodb:GetItem` on `LEDGER#{bot_id}` (its own ledger only; enforced via condition key matching on the item key)
- `dynamodb:Query` on `DECISION#{bot_id}#*` (its own decision log)
- `bedrock:InvokeModel*` on the configured model ARN
- A2A publish/subscribe permissions scoped to messages addressed to/from its logical agent ID
- **Explicitly denied**: `alpaca:*` (tool deny; it has no Alpaca credentials secret read), writes to `LEDGER#*`, reads to any other agent's bucket

### Broker Agent

Role name: `ts-broker-{org_id}`

Permissions:

- `secretsmanager:GetSecretValue` on `trading-strands/org/{org_id}/alpaca`
- `dynamodb:PutItem`, `UpdateItem` on `LEDGER#*` and `LEDGER_EVENT#*` **for bots in its org**
- Network egress to Alpaca endpoints
- Read `CONTROL#{org_id}` for halt state
- A2A for org-scoped message patterns
- Write its own memory bucket, same S3 model as Strategy Agents

### Per-bot IAM with Lambda operators

A Lambda that operates on a specific bot's data (Compactor, Self-Critique) does **not** get a per-bot IAM role. Instead:

- The Lambda has a base role that allows `sts:AssumeRole` on `ts-strategy-*` (or `ts-*-lambda-operator` where needed).
- At invocation time, the Lambda receives `bot_id` as input, assumes the role `ts-strategy-{org_id}-{bot_id}`, and uses that credential for the actual work.
- This keeps Lambda function count small (one Compactor, not N) while preserving per-bot IAM isolation at runtime.

**Assume-role chain restrictions**: the target role's trust policy only allows `sts:AssumeRole` from the specific operator Lambda's role ARN — a compromised service Lambda can't assume into arbitrary bot roles without being on the operator allow-list.

## S3 bucket model

**Per-Agent bucket** where AWS account bucket limits permit (default: 100 buckets/account).

Bucket name format: `trading-strands-agent-{agent_type}-{org_id}-{agent_id}`

### When we'll outgrow 100 buckets

At ~100 Agents total (across all orgs), we hit the default limit. At that point:

- **Option A**: request a limit increase (AWS supports up to 1000 per account on request).
- **Option B**: collapse to one bucket per org with prefix-based IAM scoping (still per-bot IAM, but the prefix is the scope).

**Decision deferred.** Start with per-Agent buckets at small scale (<100 Agents) where atomic cost accounting is a meaningful win. Flip to per-org buckets if we ever hit the limit, or optionally earlier if the limit-increase request is easier than the migration.

Recorded here so future us doesn't forget the choice was made on convenience, not principle.

### Bucket settings (applied by Provisioner)

- Versioning: enabled (recoverable accidental overwrites)
- Lifecycle: raw daily files → Glacier Deep Archive at 90 days; compressed and lessons stay Standard
- Encryption: SSE-S3 default; KMS per-org optional (future)
- Block public access: on
- Tagging: `Project=TradingStrands`, `Component=agent-memory`, `OrgId=<>`, `AgentId=<>`, `AgentType=<>`

Tags feed CloudWatch cost allocation reports, letting us render per-Agent cost in the dashboard.

## BotProvisioner

A Lambda triggered on a DynamoDB Stream event when a strategy's status transitions to `active` (first time) or manually invoked for other Agent provisioning.

Responsibilities:

1. Create the per-Agent S3 bucket, apply settings and tags
2. Create the per-Agent IAM role with the appropriate policy template
3. (For Fargate Agents) Create the ECS task definition with the Agent-type container image and the IAM role
4. (For Fargate Agents) Create / update the ECS service for the Agent, desired count 0 until activation
5. Register wake/sleep schedule entries if applicable (see Scale Dispatcher below)
6. Record `AGENT_INFRA#{agent_id}` in DynamoDB with all created resource ARNs for future deprovisioning
7. Emit `agent.provisioned` event on EventBridge

Failures in any step are caught and produce `AGENT_INFRA#{agent_id}` with `status=provisioning_failed` and error details. A sysadmin alert is raised. Partial state is never hidden.

Provisioning latency is async — the strategy's status remains `provisioning` until the Provisioner emits success. UI polls / streams the status.

## Deprovisioner

A Lambda triggered on `strategy.deleted` (or any Agent deletion). Follows a **tombstone-and-reconcile** pattern:

1. Mark `AGENT_INFRA#{agent_id}` with `status=decommissioning`
2. Stop the ECS service (desired count 0), wait for tasks to drain
3. Delete ECS service
4. Delete ECS task definition (or mark for deletion — AWS retains them indefinitely)
5. Empty the S3 bucket (slow step; can take hours for large buckets); after empty, delete
6. Delete the IAM role (after confirming no active assumed-role sessions exist)
7. Deregister wake/sleep schedule entries
8. Mark `AGENT_INFRA#{agent_id}` with `status=gone` **only after all above steps succeed**

A background **reconciler Lambda** runs every 15 minutes, scans for `status=decommissioning` records older than a threshold, retries the stuck step, and logs progress. Failure modes that can't be auto-recovered page sysadmin.

The UI reflects `gone` immediately after tombstone (status=decommissioning) — from the user's perspective the Agent is deleted. The real teardown happens in the background.

## Scale Dispatcher

A single Lambda, triggered by EventBridge cron rules at market-open and market-close times (configurable per sysadmin).

On wake event: query DynamoDB for all Agents in active orgs whose strategies are `active`. For each, issue `ecs:UpdateService --desired-count 1`.

On sleep event: same query, desired count 0.

Why a centralized dispatcher vs. per-Agent cron rules:

- **One cron rule fires, one Lambda enumerates** — new strategies get included automatically without registering new rules.
- **Back-pressure control** — the dispatcher can stagger wake-ups to avoid hammering Fargate with 50 simultaneous task starts.
- **Better auditing** — one Lambda's log shows every wake/sleep decision in one place.

## Per-org deployment mode switches

Three independent switches per org, stored in DynamoDB:

```
ORG_MODE#{org_id}#compliance = "lambda" | "fargate"
ORG_MODE#{org_id}#risk       = "lambda" | "fargate"
ORG_MODE#{org_id}#auditor    = "lambda" | "fargate"
```

Default: all three `lambda` for new orgs. Orgadmin changes via the dashboard.

### Mode change semantics

- **Change request** submitted by orgadmin → stored as pending with effective-at timestamp set to **next market open** (default).
- **At effective time**, Provisioner picks up the pending change:
  - If moving to Fargate: creates the task definition + service, starts task, verifies health.
  - If moving to Lambda: waits for Fargate task to drain any in-flight work, stops it, leaves the Lambda function intact (Lambdas are always-deployed; the "mode" just controls whether invocations route to Lambda or to an always-on Fargate task).
- **UI shows** "Compliance Agent mode: Lambda (change to Fargate scheduled for 2026-04-27 06:30 ET)"

### Break-glass

A sysadmin-only button "Apply mode change now" bypasses the market-boundary constraint. Expected consequences (documented in the UI):

- In-flight reviews in the mode being torn down will fail.
- Strategy Agents won't receive Compliance/Risk reviews during the transition window (~30 seconds).
- Halt state is preserved via DynamoDB, but pending recommendations may be lost.

Break-glass is for emergencies (a misbehaving agent that needs to be decommissioned during market hours). Ordinary mode changes use the scheduled path.

## Deploy strategy

**Blue-green for dashboard and Broker Agent** starting in v1. For these two, a bad deploy is high-impact (dashboard: everyone's locked out; Broker Agent: trades rejected). Blue-green lets us validate the new task before routing ALB traffic + A2A addresses.

For Strategy Agents, a rolling replacement (1 at a time, health-checked) is fine — single-bot impact is bounded.

For Lambdas, CodeDeploy canary (10% for N minutes, then 100%) where it's configurable cheaply.

Deploy ordering (from GitHub Actions):

1. Lint, typecheck, unit tests
2. Build + push all container images
3. CDK diff; sysadmin approval for destructive changes
4. CDK deploy — creates/updates resources but does not force task replacement
5. Post-deploy migration scripts (bootstrap, schema migrations)
6. Blue-green cutover for dashboard + Broker Agents
7. Rolling restart for Strategy Agents (wave of ~5 at a time)
8. Smoke tests against fresh deploy
9. Revert on failure: re-point ALB to blue; restart old Broker Agent tasks

## Observability during deploy

Every deploy step emits CloudWatch EMF metrics:

- `deploy.cdk.diff_resources` — number of resources being changed
- `deploy.cdk.deploy_duration_s`
- `deploy.bluegreen.cutover_duration_s`
- `deploy.rolling.agent_restart_count`
- `deploy.smoke_test.failures`

These graph in the dashboard so deploys become observable without needing AWS console access.

## Secrets

Secret paths follow the hierarchy:

```
trading-strands/cognito-client-secret              — dashboard auth
trading-strands/session-signing-key                — signed URL tokens
trading-strands/org/{org_id}/alpaca                — per-org broker creds
trading-strands/org/{org_id}/bloomberg             — future: per-org specialized data
trading-strands/agent/{agent_id}/feed-license      — future: agent-specific licenses
```

Per-Agent secrets (the last line) are optional — most Agents have none. Exist so we can attach specialized license creds to a specific Strategy Agent later without rewiring.

Read permissions:

- Agents can read secrets at their prefix (`trading-strands/agent/{my_agent_id}/*`)
- Broker Agent can read its org's broker creds
- No Agent has access to any other org's secrets
- sysadmin with break-glass role can read anything (audit-logged)

## Blast radius

This is the property the whole deployment model is optimizing for:

- **A Fargate node failing** affects only Agents that were on that node. AWS re-schedules; other orgs unaffected.
- **A single Strategy Agent crashing** affects only that bot. Same org's Broker Agent and other strategies keep going.
- **A Broker Agent crashing** affects all strategies in that one org (they can't trade). Other orgs unaffected. Auto-restart by ECS; halt flag persists through restart; in-flight intents are marked rejected.
- **A Lambda execution failing** affects only that one invocation. Next invocation is unaffected.
- **The Platform Supervisor crashing** affects nobody's trades (it doesn't gate trades). Heartbeat monitoring gaps until restart.
- **A full regional AWS outage** — everyone's down. Out of scope for v1. Documented as known limitation.

## Open questions

- Multi-region: not in v1. Single region (us-west-2). Revisit if regulatory drives multi-region.
- Fargate Spot: tempting for Strategy Agents (cheap), but Spot interruption during trading would be bad. Defer until we have a cleaner interruption-recovery story.
- Agent version pinning: can an orgadmin pin a Strategy Agent to a specific container version to avoid deploy-driven restarts? Defer; unlikely to be needed at current scale.
