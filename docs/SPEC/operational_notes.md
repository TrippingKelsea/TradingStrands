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

## TODO: DDB scan pagination sweep

**Status:** partial. Tracked as a follow-up.

Observed bug class (three instances, same root cause):

- `StrategyStore.list_all` returned 1 of 2 ACTIVE strategies →
  `reconcile_all` skipped provisioning one bot's Fargate service.
- `StrategyStore.list_for_org` / `acl_users` / delete's ACL cleanup —
  same pattern, same fix.
- `TenancyStore.memberships_for_user` returned `[]` for a real
  member → `principal.memberships` empty → every org-scoped
  endpoint 403'd (observed on `/api/strategies`, `/api/tokens/today`).

Root cause: `table.scan(FilterExpression=...)` returns at most ~1 MB
of **pre-filter** items per call. The shared single-PK table mixes
many prefix families (`MARKETDATA#` dominates by volume, plus
`STRATEGY#`, `USERORG#`, `ORG#`, `CALENDAR#`, `TA_SNAPSHOT#`, `NEWS#`,
`FILING_INDEX#`, `SOCIAL#`, `TOOL_QUOTA#`, `SKILL#`, `ORG_TOOL#`,
`HALT_EVENT#`, `HEARTBEAT#`, `LEDGER#`, `RECOMMENDATION#`,
`STRATEGYPROPOSAL#`, `PROMPTSNAPSHOT#`, `DEPLOY#`). A filtered scan
on any lighter prefix can burn its 1 MB page budget on non-matching
rows and return fewer matches than exist. The fix is always
`LastEvaluatedKey` follow-up, which each store currently handles
(or doesn't) independently.

**Already fixed inline:** `strategies_store` (`_scan_all` helper) and
`tenancy` (same helper copied in).

**Pending stores with the same pattern, to be swept in one pass:**

- `org_tools/store.py`
- `strategy_proposals/store.py`
- `dashboard/publisher.py`
- `filings_store/stores.py`
- `recommendations_store/store.py`
- `halt/store.py`
- `skills_store/store.py`
- `heartbeat/store.py`
- `ledger_store/store.py`

**Proposed cleanup (future commit):**

1. Promote `_scan_all` to a shared module (e.g.
   `trading_strands.ddb.scan_all`) and have every store import it
   instead of duplicating the helper.
2. Emit a WARN log when the helper observes a `LastEvaluatedKey` on
   the *first* page — a live "this scan would have silently dropped
   rows in a one-shot call" signal that surfaces in CloudWatch
   before a user notices a bug.
3. Add a pytest AST check that walks `src/` and fails if it finds
   `.scan(` on a `self._table` outside the helper. Prevents the
   class of bug from re-entering via a hurried new store. Simpler
   than a custom ruff plugin and doesn't need a new dependency.

Lightweight library + lint guard rather than a repository/ORM layer —
the table is single-PK and the access patterns are narrow enough
that a thin helper plus a CI check closes the door without paying
for indirection on every row read.

## Dashboard XSS hardening (render helpers + CSP + input validators)

### Background

A security review of this session's commits turned up three stored-XSS
sites where user-derived fields were concatenated into `innerHTML`
string templates without escaping: the Proposals list, the Tool-calls
feed, and the Ledger positions panel, all on the per-strategy detail
view. The injection vectors were:

- `symbols` on a strategy (no server-side format validation; echoed by
  the bot into EMF tool-call records, rendered back via the feed)
- `rationale` / `proposed_markdown` on self-critique proposals (LLM
  output from Bedrock, written verbatim into DDB)
- position `symbol` on the ledger panel (same provenance as above)

Same-org exploitation is real: operator-role users can author
strategies; orgadmin / auditor / sysadmin users view the same detail
page. An operator XSS executes in a higher-privilege user's
authenticated session.

### Hardening layers

Four defenses land together, each independently closing the bug class:

1. **DOM render helpers** (`dashboard/templates/base.html`'s script
   block): `el(tag, attrs, ...children)`, `frag(...)`, `mount(container,
   ...)`, `clear(container)`. Strings in `children` become text nodes
   (auto-escaped); numbers stringify; other nodes mount as children.
   Event handlers attach as function references via `attrs.onClick`,
   not as `onclick="foo('<id>')"` strings — eliminates the class of
   "forgot to escape the id" bugs in handler wiring.

2. **Full conversion**: every `.innerHTML =` assignment in the
   dashboard templates is replaced with `mount()`. No exceptions on
   "this content is trusted" — the AST guard below forbids the
   pattern outright.

3. **Server-side validators** (Pydantic `@field_validator` on the
   request bodies): `StrategyCreate.symbols` must be `^[A-Z0-9.\-]
   {1,10}$`; `StrategyCreate.name` length + control-char rules;
   `StrategyCreate.markdown` size cap; skill names alphanumeric;
   tool-name path params constrained to the registry-known set. A
   regressed renderer cannot exploit what the validator never let
   into storage.

4. **CSP + security headers middleware**: `Content-Security-Policy:
   default-src 'self'; script-src 'self'; style-src 'self'
   'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'`
   plus `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
   `Referrer-Policy: same-origin`. `unsafe-inline` for style is
   kept for now because the layout uses inline `style="..."`
   attrs heavily; tightening to hashes/nonces is a later follow-up
   and does not change the XSS protection posture since `script-src`
   already forbids inline `<script>` and event-handler attributes
   like `onerror=`.

### CI guards

- `tests/dashboard/test_no_inner_html.py`: walks every template
  `<script>` block and fails if it finds `.innerHTML =` or
  `.innerHTML +=` assignments. Mirrors the DDB `test_no_bare_scan.py`
  pattern — a future hurried commit cannot re-introduce the bug
  without the failing test being explicitly skipped.

- `tests/dashboard/test_security_headers.py`: asserts the CSP and
  companion headers appear on every response from the `/` and `/api/*`
  endpoints.

### Related

- Security review finding at `af5cf33` motivated this work.
- The DDB pagination sweep (see §"TODO: DDB scan pagination sweep")
  used the same "one helper + AST guard" shape, chosen deliberately
  so the two hardening patterns are recognizable by future operators.

## Dependency updates (Dependabot)

`.github/dependabot.yml` enables weekly dependency updates in two
ecosystems: Python (pip) and GitHub Actions. Security advisories fire
immediately regardless of the schedule — the weekly cadence only
governs non-security feature bumps.

**Grouping.** Related packages batch into one PR so review + CI run
once per logical upgrade rather than once per package:

- `aws-deps` — boto3, botocore, awslambdaric, moto, mypy-boto3-*
- `llm-deps` — strands-agents, bedrock-agentcore, anthropic
- `broker-deps` — alpaca-py, robin-stocks, yfinance
- `dev-deps` — pytest, pytest-*, ruff, mypy
- `web-deps` — fastapi, uvicorn, starlette, jinja2, etc.
- `ci-actions` — every GitHub Action we use

A dep not covered by any group gets its own PR. Major bumps always
get individual PRs regardless of group so they get a deliberate
review (per-group `update-types` only covers minor + patch).

**Review expectations.** Dependabot PRs go through the same CI as
everything else (lint, type-check, test, deploy). A green dependabot
PR is mergeable; a red one blocks on the usual debug path. The
point of grouping is that a red CI on a group PR tells you "one of
these five deps broke something" — bisect by reverting one package
at a time in the branch.

**SHA pinning for Actions.** The `github-actions` ecosystem
automatically rewrites `@v4` / `@v6` tag pins to full commit SHAs.
This is the recommended posture — a compromised-tag supply-chain
attack on a popular action (e.g. someone moves `@v4` to a malicious
SHA) cannot affect CI because we pin by SHA. Readability cost is
small; security value is substantial.

## References

- [multi_tenancy.md](./multi_tenancy.md) — authz and schema-evolution rules
  referenced here
- [deployment.md](./deployment.md) — deployment model this doc supports
- [observability.md](./observability.md) — cost allocation and tagging are
  prerequisites for the cost dashboard widgets described there
- `CLAUDE.md` — highest-priority invariants (no backtesting, deterministic
  hot path, etc.) that this doc reinforces at the operational layer
