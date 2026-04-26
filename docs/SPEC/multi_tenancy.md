# Multi-tenancy & Authorization

**Status:** Current (v0 + v1). Implemented as of commit `822b234`.
**Last updated:** 2026-04-26

## Premise

TradingStrands is multi-tenant from the data plane up. Multiple organizations
coexist in the same infrastructure; data from one org never flows to another
except through narrowly-scoped paths explicitly authorized by policy.

The three core concerns:

1. **Org-scoped data** — strategies, snapshots, Alpaca secrets, events all belong
   to a specific org and never leak across org boundaries.
2. **Role-based capabilities** — a user's abilities depend on their role in the
   specific org they are acting in; a user can hold different roles in different
   orgs.
3. **Deny by default** — authorization answers "no" unless an explicit rule says
   "yes." A missing policy rule is a `403`, not a `500`.

## Domain model

### Org

An organization is a first-class entity with a type:

- `customer`: a customer org. Owns strategies, users (via membership), Alpaca
  credentials, runs Agents.
- `system`: the platform's own org. Exactly one exists at any time, seeded by
  bootstrap (`Women with Super Powers`). Only members of this org are eligible
  for the sysadmin flag. A `customer` org can never promote a user to sysadmin.

### User

First-class identity. `USER#{user_id}` in DynamoDB; `user_id` is ours and
survives Cognito pool rebuilds. `cognito_sub` is a pointer to Cognito, which
is the authoritative store for *authentication* only (email + password). All
*authorization* information lives in DynamoDB.

### Membership

The join between User and Org. `USERORG#{user_id}#{org_id}` carries the user's
role in that org. Membership is many-to-many: one user can belong to many orgs;
one org has many users. Both sides of the join are written (`USERORG#` and
`ORGUSER#`) so querying "which orgs does user X belong to" and "which users
belong to org Y" are both cheap prefix scans.

### Sysadmin

A separate global flag, not a role. Stored as a sentinel item `SYSADMIN#{user_id}`.
Absence is the default (deny). Sysadmin is independent of any specific org
membership — it crosses org boundaries by construction, which is why it's not
a `Role` (roles are per-org).

## Roles

Per-org, assigned to a user via a membership. All four roles are deliberate;
each exists to serve a specific capability set:

| Role | Meant for |
|---|---|
| `viewer` | Read-only participant. No mutation, no user management, zero visibility into other users. Lowest-privilege default. |
| `operator` | Strategy author. Submits and mutates their own strategies. No user management. Zero visibility into other users. |
| `auditor` | Compliance reviewer. Read-only like viewer, **plus** visibility into strategy authorship (who wrote what, when). Cannot mutate. |
| `orgadmin` | Manages users, Alpaca credentials, and any strategy within the org. |

### Role implementation notes

- `viewer` and `operator` deliberately cannot enumerate other users. This is a
  privacy concern, not just a capability reduction — a user should not learn
  who else is in the org through the API surface.
- `auditor`'s distinction from `viewer` is narrow: they get authorship
  visibility on strategies. In practice this means the strategy detail API
  includes `author_user_id` for auditors but may redact it for plain viewers.
  (v0 returns it to everyone; auditor-specific redaction is deferred until
  there's a concrete user story needing it.)

### Sysadmin

Distinct from the per-org roles. A global flag granted only to members of the
system org and never defaulted at creation (bootstrap sets the flag only on
first-create of the `superwoman@tradingstrands.xyz` account; re-runs never
re-grant).

Sysadmin capabilities, *in general*:

- Create and mutate orgs
- Manage users (including cross-org)
- See platform infrastructure (health, cost, telemetry)
- Grant/revoke sysadmin on other users

Sysadmin capabilities, *specifically constrained*:

- **Cannot read customer Alpaca secrets, ever**, regardless of any deploy flag.
  This is a hard carve-out. Customer credentials are off-limits to platform
  operators.
- **Cannot read customer strategies, positions, or PnL** unless the deploy flag
  `SYSADMIN_CAN_READ_ORG_DATA` is `true`. Default false.
- **Cannot create strategies in customer orgs.** Customer trading is never
  initiated by platform operators.
- **Cannot revoke the last remaining sysadmin.** The platform must always have
  at least one.

## The authorization policy

Implementation: `src/trading_strands/authz/`. Tests: `tests/authz/test_policy.py`
(38 tests defining the matrix).

### Core type

```python
can(principal: Principal, action: Action, resource: Resource) -> Permission
```

- `Principal`: user_id, email, `dict[org_id, Role]` memberships, sysadmin flag.
- `Action`: `READ | LIST | CREATE | UPDATE | DELETE`.
- `Resource`: type + optional `org_id` + optional `author_user_id` + delegated
  user IDs.
- `Permission`: `allowed` bool + human-readable `reason` (the name of the rule
  that fired, or "no rule matched — denied by default").

### Predicate chain

The policy is a list of small pure predicates, evaluated in order. Each one
returns either "yes, this rule allows it" or `None` (abstain). If every
predicate abstains, the request is denied.

Order within the chain is deliberate — specific allows first, then broad ones.
Every predicate is named after the reason it fires, so a permissive decision
carries an auditable reason in the `Permission.reason` field.

This shape gives three properties we want:

1. **Deny-by-default is structural**, not a forgotten `else` branch. If no
   predicate explicitly grants, access is denied.
2. **Each rule is small and testable** in isolation.
3. **Adding a capability is additive** — a new predicate added to the chain,
   not a modification of existing ones.

### The authorization matrix

| Action | viewer | operator | auditor | orgadmin | sysadmin |
|---|---|---|---|---|---|
| Strategy READ (own org) | ✓ | ✓ | ✓ (+authorship) | ✓ | flag-gated |
| Strategy READ (other org) | ✗ | ✗ | ✗ | ✗ | flag-gated |
| Strategy LIST (own org) | ✓ | ✓ | ✓ | ✓ | flag-gated |
| Strategy CREATE (own org) | ✗ | ✓ | ✗ | ✓ | ✗ |
| Strategy CREATE (other org) | ✗ | ✗ | ✗ | ✗ | ✗ |
| Strategy UPDATE (own, self-authored) | ✗ | ✓ | ✗ | ✓ | ✗ |
| Strategy UPDATE (own, others'-authored) | ✗ | ✗ | ✗ | ✓ | ✗ |
| Strategy UPDATE (ACL delegated) | ✗ | ✓ (if ≥ operator) | ✗ | ✓ | ✗ |
| Strategy DELETE | same as UPDATE | | | | |
| Org CREATE | ✗ | ✗ | ✗ | ✗ | ✓ |
| Org READ (own) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Org UPDATE/DELETE (own) | ✗ | ✗ | ✗ | ✓ | ✗ |
| User management (own org) | ✗ | ✗ | ✗ | ✓ | ✓ |
| User management (cross-org) | ✗ | ✗ | ✗ | ✗ | ✓ |
| Alpaca secret (any) | ✗ | ✗ | ✗ | ✓ (own org) | **✗ always** |
| System config | ✗ | ✗ | ✗ | ✗ | ✓ |
| Cost data / infra telemetry | ✗ | ✗ | ✗ | ✗ | ✓ |
| Market data (platform-wide) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Sysadmin grant/revoke | ✗ | ✗ | ✗ | ✗ | ✓ (gated) |

"flag-gated" means sysadmin can only when `SYSADMIN_CAN_READ_ORG_DATA=true` at
deploy time. Alpaca secret access is never flag-gated — that row is a hard no.

Market data is intentionally cross-org-readable: it's shared infrastructure,
not per-org accounting. See [agent_memory.md](./agent_memory.md) for how it's
stored.

### Strategy mutation — the detailed rule

Mutating a strategy (UPDATE/DELETE) requires **all** of:

1. The mutator is either the author (`p.user_id == r.author_user_id`), a
   co-author via ACL (`p.user_id in r.delegated_user_ids`), OR an orgadmin of
   the owning org.
2. The mutator currently has role ≥ operator in the owning org (unless they are
   orgadmin, in which case orgadmin is sufficient).

The conjunction matters: a former employee whose `user_id` still appears as
`author_user_id` on an old strategy, but who has been demoted to viewer or
removed from the org, cannot mutate that strategy. Authorship alone is not the
permission; current role + org membership is.

### Strategy ACL delegation

`STRATEGYACL#{strategy_id}#{user_id}` items grant co-author mutation rights to
a specific user. The author adds/removes entries. The delegated user still
needs to be a member of the org at role ≥ operator. ACL doesn't override
org membership or role requirements — it only extends authorship.

## Deploy-time configuration

### `SYSADMIN_CAN_READ_ORG_DATA`

Environment variable read by the policy at each check. Default `false`.

- `false` (default): sysadmin sees orgs, users, infra metrics, cost — but not
  strategy content, positions, PnL, or trades of non-system orgs.
- `true`: sysadmin can read org-scoped data across orgs for debugging /
  support.

Intentionally *not* a per-org setting — this is a platform-wide operational
posture. Flipping it requires a redeploy (or at minimum a task restart), which
is audit-friendly.

Alpaca secrets are **never** readable by sysadmin, even with this flag on.
That rule is in the predicate `_allow_sysadmin_read` directly, not a
configuration, so it cannot be flipped by a deploy variable.

## Session and identity

### Session cookie (v2 shape)

Issued on successful Cognito authentication + USER# provisioning:

```
{
  "user_id":       <our USER# id>,           # REQUIRED; v1 sessions lack this
  "email":         <cognito email>,           # cached for display
  "active_org_id": <org_id or None>,          # scope of current requests
  "access_token":  <cognito access token>,    # for future lower-level calls
  "login_at":      <unix ts>
}
```

Signed with itsdangerous + a server-side `SESSION_SECRET`. Separate salt from
URL tokens (below) so tokens of one kind cannot be substituted for the other.
Default max-age 1 year for dev, configurable per-org.

v1 session cookies (pre-refactor, missing `user_id`) are **rejected** on load;
users are forced to re-login. This is a one-way migration — there is no
in-flight compatibility layer.

### Principal resolution per request

On every authenticated request:

1. Middleware validates the session cookie → session dict attached to
   `request.state.session`.
2. Endpoint handler calls `principal_from_session(session, table)` which:
   - Loads the `USER#` record (fail → 401, session stale)
   - Loads memberships via `USERORG#{user_id}#*` scan
   - Reads the `SYSADMIN#` sentinel
   - Returns a fully-populated `Principal`
3. Endpoint calls `authz.require(principal, action, resource)` → may raise
   `Unauthorized` (→ 403).

Crucially, **memberships and sysadmin are read per-request** from DynamoDB,
not cached in the session cookie. Revocation is therefore immediate — removing
a user from an org takes effect on the next request, not at next login. This
trades a DDB round-trip per request for "no stale permissions ever." At our
scale, the round-trip is cheap; we revisit if it becomes a bottleneck.

### Active org resolution

When an endpoint needs to know which org the principal is acting in, the
`_get_active_org` helper resolves in this order:

1. `?org=<org_id>` query parameter, if present (authoritative for the single
   request). Must match a membership the principal holds.
2. `session.active_org_id`, if present. Same membership validation.
3. If the principal has exactly one membership, auto-select it.
4. Otherwise 400 "No active organization selected."

**Privacy invariant:** if the caller claims an `active_org_id` they're not a
member of (tampered cookie, stale session, etc.), the claim is ignored and
resolution falls through. Claimed access to non-member orgs never leaks data.

### Signed URL tokens

For short-lived, single-use URL parameters (error messages after a redirect,
Cognito challenge session carry-through), use `create_url_token(data)` /
`decode_url_token(token)`. These are signed with itsdangerous, expire after
60 seconds, and use a different salt from session cookies.

Reasons for this pattern:

- URLs carry state across redirects in the browser address bar. Unsigned
  parameters would be tamperable and could be used to spoof error messages
  or replay auth challenges.
- Cookies are out of scope for this kind of single-hop state. See the "no
  cookies for extra data" design rule.
- A short expiry keeps a leaked URL from being replayable beyond the redirect.

## Force-password-change flow

When an admin creates a user (or bootstrap creates superwoman), the Cognito
user is left in the `FORCE_CHANGE_PASSWORD` state. First-login flow:

1. User submits email + temporary password to `/auth/login`.
2. Cognito returns a `NEW_PASSWORD_REQUIRED` challenge with an opaque
   `Session`.
3. `/auth/login` wraps email + Cognito session in a signed URL token and
   redirects to `/change-password?t=<token>`.
4. `/change-password` renders a form that POSTs back with the token + new
   password.
5. `/auth/change-password` validates the token, runs the password strength
   policy (see below), calls Cognito's `respond_to_auth_challenge`, and on
   success issues a session cookie.

The Cognito session never appears in the browser URL — only the signed-token
envelope does. A token captured from a URL expires in 60 seconds regardless.

## Password policy

Cognito's own password policy (12+ chars, upper+lower+digit+symbol) is
necessary but not sufficient. On top of Cognito we run a server-side policy
using zxcvbn:

- **zxcvbn score must be ≥ 3** (moderate strength, per zxcvbn 0–4 scale).
- **Hard blocklist**: `tradingstrands`, `changemeonfirstlogin`, `superwoman`.
- **User-context blocklist**: email local-part, any `forbidden` tokens passed
  by the caller (notably the starter password during the change-password
  flow, to prevent literal reuse).

Rejection returns a specific `reason` that the UI surfaces — users should
know *why* the password was rejected, not just that it was.

Applied at two points:

- `/auth/change-password` — user self-service.
- `/api/admin/users/{user_id}/reset-password` — orgadmin reset. Admins can't
  set weak passwords for their users either.

Implementation: `src/trading_strands/dashboard/password_policy.py`.

## Bootstrap

Implementation: `src/trading_strands/bootstrap/`. Runs post-deploy in CI.

### Invariants

- **Idempotent**: running on an empty table and on an already-bootstrapped
  table both produce the same final state. Tests cover both paths.
- **Grant-on-first-create, never on re-run**: sysadmin is granted to superwoman
  on her initial provisioning. If sysadmin is later deliberately revoked
  (demoting the default account), bootstrap must not re-grant on the next
  deploy. Revocation is sticky.
- **Never overwrite configured secrets**: the Alpaca secret seed from the
  legacy global `trading-strands/alpaca` into the system org's per-org secret
  (`trading-strands/org/{system_org_id}/alpaca`) happens only if the per-org
  secret is not already configured. An orgadmin who has manually set their
  keys must not lose them on redeploy.
- **Legacy strategy pruning**: any `STRATEGY#` item lacking `org_id` or
  `author_user_id` (pre-refactor schema) is deleted. Valid strategies are
  preserved.

### Steps

1. Ensure the system org (`Women with Super Powers`, type=system) exists.
2. Ensure the superwoman user exists in DynamoDB (`USER#` + `USEREMAIL#`
   index). If created for the first time: add her as orgadmin of the system
   org AND grant sysadmin.
3. Delete legacy-shape strategies.
4. Seed the system org's Alpaca secret from the legacy global secret, if
   the per-org secret is not already configured.

Cognito-side provisioning of the superwoman Cognito account happens in CI as a
separate step (`admin-create-user` with a known starter password, leaving the
user in `FORCE_CHANGE_PASSWORD`). Bootstrap and Cognito provisioning are
decoupled so DDB state survives a Cognito pool rebuild.

## Schema evolution

Pydantic models for `Org`, `User`, and `Strategy` use `extra='ignore'`
(not `'forbid'`) so pre-refactor DDB rows can be loaded without a migration
step. This is deliberate:

- **New fields go in `settings`** (or wherever the schema has space), not as
  top-level columns, to avoid churn on existing items.
- **Old top-level fields are dropped on next write** (pydantic's `model_dump`
  only emits known fields).
- **Strict validation at the write boundary is preserved** by using specific
  schemas for inputs (the API-surface models remain strict); relaxed
  validation is only at the read boundary.

This lets the schema evolve forward without a per-change migration job.

## Known multi-tenancy gotchas

### The many-to-many dual-write without transactions

`USERORG#` and `ORGUSER#` are written as two sequential `PutItem` calls with a
rollback on the second-put failure. They are NOT in a DynamoDB transaction
because of a known issue in our test stack (moto 5.1.22): transact_write_items
with ConditionExpression fails with a `DynamoType is not hashable` error.

At our scale (low write concurrency, bounded failure modes), the rollback
approach is correct. If one side fails:

- Second put failed → first put is deleted, caller sees the exception.
- First put succeeded, second put partially visible → idempotent retry by the
  caller converges state.

The Cognito-sync job (future work) would also detect orphaned single-sided
memberships during its reconcile pass.

### Strategy authorship drift

`author_user_id` is immutable after create. A user leaving the org does not
update their strategies' authorship — they just can't mutate them anymore.
The audit trail ("who wrote this") survives; the capability does not.

Orgadmin takeover is therefore the documented recovery path: if a strategy's
author leaves, an orgadmin is the only non-sysadmin who can continue to
mutate or delete it.

### First-login active-org auto-resolution

A user newly-added to exactly one org auto-selects it as the active org on
first login. A user added to multiple orgs lands without an `active_org_id`
and the dashboard will show the pre-dashboard org picker (see deployment /
frontend notes — not yet implemented as of this doc). API endpoints that
need an `active_org_id` return `400` with a specific message telling the
caller to select an org.

### Org deletion

Aggressive cleanup pattern: delete memberships (both sides), strategies,
Alpaca secrets, CloudWatch alarms, then the org itself. The dashboard marks
the org as deleted immediately; a background reconciler completes the real
teardown. See [deployment.md](./deployment.md#deprovisioner).

Users who had memberships in the deleted org lose those memberships but
remain users (their USER# record persists) — they may still be members of
other orgs, or may be left with no memberships (in which case they can log in
but have no capabilities until granted membership somewhere).

## References

- `src/trading_strands/authz/model.py` — Principal, Role, Resource, Action,
  Permission, ResourceType
- `src/trading_strands/authz/policy.py` — predicate chain + `can()` + `require()`
- `src/trading_strands/tenancy/` — Org, User, Membership storage
- `src/trading_strands/strategies_store/` — org-scoped strategy model + ACL
- `src/trading_strands/bootstrap/` — idempotent first-deploy seeding
- `src/trading_strands/dashboard/principal.py` — session → Principal loader
- `src/trading_strands/dashboard/password_policy.py` — zxcvbn + blocklist
- `tests/authz/test_policy.py` — 38 tests defining the full policy matrix
