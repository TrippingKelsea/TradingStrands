# Operational Notes

**Status:** Current. Living document.
**Last updated:** 2026-04-26

These are operational rules and invariants learned from running the platform.
They aren't features — they're the constraints and conventions that keep the
infrastructure sane across deploys and rebuilds.

## Resource tagging for cost accounting

Every AWS resource we create is tagged. At minimum:

| Tag | Purpose |
|---|---|
| `Project=TradingStrands` | Scopes everything to this product for Cost Explorer filtering |
| `Environment=<dev\|staging\|production>` | Distinguishes account-shared resources |
| `ManagedBy=CDK` | Signals "don't modify by hand; changes via IaC only" |
| `Component=<trading-service\|dashboard-service\|agent-memory\|...>` | Enables per-component cost attribution |
| `OrgId=<uuid>` | Where applicable — lets us bill per-org cost |
| `AgentId=<uuid>` | For per-Agent resources (memory buckets, task definitions, IAM roles) |
| `AgentType=<strategy\|broker\|risk\|compliance\|...>` | Per-agent-type aggregation |

### Why tagging matters early

- **Cost allocation tags must be activated in the billing console** before
  Cost Explorer groups by them. This is a one-time action per tag name per
  account; a fresh deploy into a new account needs this step or the cost
  dashboard returns empty data.
- **Tags are the primary attribution mechanism** for understanding "which
  Agent is burning Bedrock budget" and "which org is driving Fargate spend."
- **Tags are a precondition**, not an afterthought: adding them later means
  historical cost data can't be broken down by the new tags.

### Rule

All CDK-created resources must be tagged at the stack level
(`cdk.Tags.of(self).add(...)`) for Project/Environment/ManagedBy, and at the
per-construct level for Component/OrgId/AgentId where relevant. Provisioner
Lambdas that create resources outside CDK (per-bot buckets, IAM roles) must
apply the same tags.

## Security-group discipline for load balancers

Public-facing load balancers (ALBs) must never accept `0.0.0.0/0` on their
application ports without an explicit reason. Unrestricted public listeners
are a known attack surface and trigger automated mitigations in many managed
environments.

### Rules

1. **`AllowedCidr` is a required CDK parameter**, not optional. Deploys that
   don't supply it fail fast.
2. **The CDK `ecs_patterns.ApplicationLoadBalancedFargateService`'s default
   behavior creates a `0.0.0.0/0` SG.** We override with `open_listener=False`
   and attach our own restrictive SG.
3. **Operator access** uses a CIDR-restricted SG scoped to the operator's IP
   or network range. Stored as a GitHub Actions secret (`ALLOWED_CIDR`); CI
   passes it to CDK at deploy time.
4. **Production** will use WAF + CloudFront + proper auth in front of the ALB,
   not CIDR restriction. CIDR is a dev-environment posture.

### Why this matters beyond mitigations

- Public ALB listeners without auth are scanned constantly. Even with the
  dashboard's own auth layer, the surface area being probed is a reputational
  and operational concern.
- Least-privilege on the network boundary is defense-in-depth for
  least-privilege everywhere else.
- CIDR restriction at the SG layer makes auth bugs in the dashboard layer
  non-exploitable from the open internet during the window before they're
  patched.

## IAM role types and update paths

Two classes of IAM roles in the system, managed differently:

### CDK-managed roles

Created by the CDK stack. Updated by changing the CDK code and redeploying.
Do not edit in the console or via CLI; CDK will reconcile drift on the next
deploy and your change will be lost.

Examples: `TradingTaskRole`, `DashboardTaskRole`, per-bot task roles (once
the provisioner exists).

### Bootstrap roles (not CDK-managed)

Created once, outside CDK, to *enable* CDK deploys. Updated via direct
`aws iam put-role-policy` calls with a JSON policy document.

Examples: `github-actions-deploy` — the role CI assumes to deploy CDK.

### Rule

When a post-deploy CI step fails with `AccessDenied`, **the fix is almost
always in the IAM policy for the bootstrap role**, not in the CI workflow.
Grep the current inline policy for the denied action; if absent, add it.

Use case record (the things we've needed to add so far):

- `secretsmanager:PutSecretValue`, `CreateSecret`, `DescribeSecret`,
  `GetSecretValue` on `trading-strands/*-*` — for seeding Cognito client
  secret, Alpaca secret, and per-org Alpaca secrets
- `cognito-idp:DescribeUserPoolClient`, `AdminGetUser`, `AdminCreateUser` —
  for seeding the Cognito client secret and provisioning the superwoman user
- `ecs:ListClusters`, `ListServices`, `UpdateService`, `DescribeServices` —
  for post-deploy force-redeploy of ECS services
- `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem`, `Scan`, `Query`
  on the state table — for the bootstrap idempotent migration

### Why bootstrap roles aren't in CDK

Because CDK can't deploy without them. The role that runs `cdk deploy` must
exist before the first `cdk deploy`. Chicken-and-egg. Keep the bootstrap
role minimal, scoped specifically to what the CI workflow does.

## CloudFormation chicken-and-egg with Secrets Manager

**Rule:** any secret that the ECS task reads at startup must exist before the
first deploy of the task definition.

### Why

ECS task definitions reference secrets by ARN. On first stack creation, if
the task def references a secret that doesn't exist yet, the ECS service
fails to start, the stack hangs in `CREATE_IN_PROGRESS` waiting for service
stabilization, and eventually rolls back.

### Pattern

Create CDK-managed placeholder secrets in the stack:

```python
cognito_client_secret = secretsmanager.Secret(
    self, "CognitoClientSecret",
    secret_name="trading-strands/cognito-client-secret",
    description="Cognito app client secret — seeded by CI after stack deploy",
)
```

The CDK-created secret exists at deploy time with a CDK-generated placeholder
value. A post-deploy CI step then overwrites the value. ECS never references
a non-existent secret.

This is worth codifying: any secret with a dynamic value (something
post-deploy CI computes) gets a CDK-managed placeholder and a CI
`put-secret-value` step. Don't assume "we'll just create the secret manually
before first deploy" — the first deploy is CI's first deploy too.

## Bucket limits and multi-tenant scaling

**Default quota: 100 S3 buckets per account.** Soft limit; request increase
via support case up to ~1000.

Per-Agent buckets offer clean cost accounting and atomic IAM scoping. At our
current scale (< 100 agents), this is clearly right. Past 100 agents, options:

1. Request limit increase (cheapest path)
2. Collapse to per-org buckets with prefix-based IAM scoping
3. Collapse to a single platform bucket with per-agent prefix + condition keys
   in IAM

We default to (1) at 100 and defer the decision between (2) and (3) until
then. Logged here so the choice isn't remade later under pressure.

## Test infrastructure

### `moto` for DynamoDB / Secrets Manager / Cognito in tests

We use `moto` (moto[dynamodb]) to back a real-semantics DDB table for tests.
This is materially better than `unittest.mock` for testing persistence code —
transactions, conditional puts, and scans all behave like real DDB.

### Known moto quirks to work around

- **`transact_write_items` with `ConditionExpression` fails** in the currently
  pinned version (moto 5.1.22) with `DynamoType is not hashable`. Workaround:
  use `put_item` with conditional expression and rollback-on-failure instead
  of `transact_write_items`. At our scale, the absence of true transactions
  is acceptable — documented in [multi_tenancy.md](./multi_tenancy.md).
- **`NULL` attribute type rejected** in some write paths. Omit `None` fields
  from items entirely (pydantic's `model_dump(exclude_none=True)`).

### Required env vars

Tests need AWS environment variables to avoid `NoRegionError`:

- `AWS_DEFAULT_REGION=us-west-2`
- `AWS_ACCESS_KEY_ID=testing`
- `AWS_SECRET_ACCESS_KEY=testing`

Set in each test module's top-level via `os.environ.setdefault` so local dev
and CI both work without external configuration.

## CI reliability patterns

### Post-deploy steps should tolerate partial success

Some post-deploy steps are optimizations, not corrections. `continue-on-error`
is appropriate for:

- `ecs:UpdateService --force-new-deployment` (CDK already handles task
  replacement on image-tag changes)

`continue-on-error` is **not** appropriate for:

- Bootstrap (missing bootstrap = broken app)
- Secret seeding (missing secrets = ECS tasks fail to start next deploy)
- Cognito user provisioning (missing user = cannot log in)

### Ordering

Post-deploy steps run in this order for a reason:

1. **Cognito client secret seed** — overwrites the CDK placeholder with the
   real client secret. Must run before any service that needs it restarts.
2. **Alpaca credential seed** — pulls CI secrets into Secrets Manager so the
   trading service has something to read.
3. **Cognito user provisioning** (superwoman) — needs Cognito pool to exist.
4. **Bootstrap** (DynamoDB) — creates org + user records. Must run after
   Cognito provisioning so USER# has a valid cognito_sub on first login.
5. **Force ECS redeploy** — picks up the new secrets and any immutable
   container config changes.

Changing this order can work, but document the reasoning.

## Deploy discipline

### CDK context parameters vs. hardcoded

Values that differ between environments (domain names, CIDR ranges, account
IDs) go in CDK context parameters passed at deploy time:

```
cdk deploy -c domain=app.tradingstrands.xyz \
           -c zone_name=tradingstrands.xyz \
           -c zone_id=Z04993921Q8ZI5T322JJX \
           --parameters AllowedCidr="1.2.3.4/32"
```

Values invariant across all environments stay hardcoded in CDK. This keeps
per-environment delta small and reviewable.

### Branch protection on `master`

CI runs on every push to `master`. Deploy is gated on `master` pushes only
(not PRs). Pre-production we do not enforce PR review — the operator is the
only developer. Production will have branch protection.

### Deploy failure recovery

A failed `cdk deploy` leaves the stack in `UPDATE_ROLLBACK_*` or
`CREATE_FAILED` status. Recovery patterns:

- **`CREATE_FAILED` / fresh install**: stack is empty; delete it (`aws
  cloudformation delete-stack`) and retry the deploy.
- **`UPDATE_ROLLBACK_COMPLETE`**: fix the root cause in CDK code, push, let
  CI retry.
- **`UPDATE_ROLLBACK_FAILED`** or stuck in `IN_PROGRESS`: requires
  CloudFormation intervention. Don't force; diagnose.

Known teardown failure modes we've hit:

- **Target groups stuck "in use"** when listeners have been deleted outside
  CloudFormation. CloudFormation still references the listener; we clear by
  deleting the target group directly, then retrying the stack delete.
- **ACM certificates stuck "in use"** after load balancer deletion — usually
  clears within a minute as AWS updates `InUseBy`. Retry the delete.

## Pre-alpha dev discipline

Current posture: single-operator dev account. Production is a separate AWS
account that doesn't yet exist.

Rules this imposes:

- **Destructive operations are acceptable** — "delete everything and redeploy"
  is a valid reset strategy in dev. Data is disposable.
- **Production migrations will need rigor we're not applying now.** When we
  spin up the production account, the full multi-tenancy + privacy refactor
  work gets re-validated against an empty production environment before
  customer data ever exists.
- **Operational rules in this document are about the dev account** unless
  otherwise noted. Production has stricter rules (branch protection, PR
  review, change management, incident response).

## Weekend mode vs. backtesting (rule, not operational concern)

Included here because it's an invariant operators may be tempted to violate:

**"Weekend self-critique on recorded live data" is NOT backtesting.**

- Backtesting: simulate trades against historical data as if the strategy
  were running. **Forbidden.**
- Weekend self-critique: the Self-Critique Agent reads what the Strategy
  Agent actually did during the past week and produces feedback. **Allowed.**

The distinction is: weekend mode never computes a "what would have happened
if I'd traded at time T." It only reasons about actual decisions that were
made, using actual recorded market data. No phantom fills, no simulated PnL.

This rule is codified in `CLAUDE.md` and repeated in [agents.md](./agents.md).

## References

- [multi_tenancy.md](./multi_tenancy.md) — authz and schema-evolution rules
  referenced here
- [deployment.md](./deployment.md) — deployment model this doc supports
- [observability.md](./observability.md) — cost allocation and tagging are
  prerequisites for the cost dashboard widgets described there
- `CLAUDE.md` — highest-priority invariants (no backtesting, deterministic
  hot path, etc.) that this doc reinforces at the operational layer
