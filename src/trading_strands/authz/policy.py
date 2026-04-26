"""Authorization policy — pure functions answering can(principal, action, resource).

Organized as small predicates composed into the main `can()` decision. Every
rule fails closed: if no predicate allows the action, it's denied. This is
the only module in the codebase that says "yes" to an authorization question,
and it does so explicitly.

Composability is the point: each _allow_* predicate encodes one reason to
allow, named after the reason. Adding a capability means adding a new
predicate and listing it in `_allow_chain` for the relevant resource type.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from trading_strands.authz.model import (
    Action,
    Permission,
    Principal,
    Resource,
    ResourceType,
    Role,
)

Predicate = Callable[[Principal, Action, Resource], Permission | None]


def _sysadmin_can_read_org_data() -> bool:
    """Deploy flag: when true, sysadmins see customer strategy/pnl data.

    Default false — sysadmin is by default isolated from customer data.
    Flipped per-deploy via the `SYSADMIN_CAN_READ_ORG_DATA` env var.
    """

    return os.environ.get("SYSADMIN_CAN_READ_ORG_DATA", "false").lower() == "true"


# ── Membership helpers ────────────────────────────────────────────────


def _role_in(principal: Principal, org_id: str | None) -> Role | None:
    """Role the principal has in this org, or None if not a member.

    A None org_id means a system-wide resource — never a per-org role.
    """

    if org_id is None:
        return None
    return principal.memberships.get(org_id)


def _is_org_member(principal: Principal, org_id: str | None) -> bool:
    return _role_in(principal, org_id) is not None


def _is_orgadmin(principal: Principal, org_id: str | None) -> bool:
    return _role_in(principal, org_id) == Role.ORGADMIN


# ── Predicates ────────────────────────────────────────────────────────
# Each returns a Permission to allow, or None to abstain. Denies happen at
# the end of the chain only.


def _allow_sysadmin_read(
    p: Principal, a: Action, r: Resource,
) -> Permission | None:
    """Sysadmin can read system-wide resources, and OPTIONALLY org-scoped
    resources when the deploy flag is set — with a hard carve-out for
    Alpaca secrets, which are always off-limits.

    The carve-out is structural, not configurable: customer API keys are
    a compliance concern distinct from customer data visibility. An ops
    engineer might legitimately need to debug a strategy (flag=true); they
    never have a legitimate reason to see the customer's trading keys.
    """

    if not p.sysadmin:
        return None
    if a not in (Action.READ, Action.LIST):
        return None
    if r.type == ResourceType.ALPACA_SECRET:
        return None  # hard no, regardless of deploy flag
    if r.org_id is None:
        return Permission(True, "sysadmin reads system resource")
    if _sysadmin_can_read_org_data():
        return Permission(True, "sysadmin reads org (deploy flag allows)")
    return None


def _allow_sysadmin_system_config(
    p: Principal, a: Action, r: Resource,
) -> Permission | None:
    """Sysadmin writes to system-wide config (retention, market hours, etc.)."""

    if not p.sysadmin:
        return None
    if r.type == ResourceType.SYSTEM_CONFIG and r.org_id is None:
        return Permission(True, "sysadmin writes system config")
    return None


def _allow_strategy_read(
    p: Principal, a: Action, r: Resource,
) -> Permission | None:
    """Any member of the org can READ/LIST strategies in that org.

    This is the visibility rule: viewers/operators/auditors all see strategies
    in their org. Authorship is exposed to the caller; the UI decides what
    to show. Naming leakage is accepted — see design doc.
    """

    if r.type != ResourceType.STRATEGY:
        return None
    if a not in (Action.READ, Action.LIST):
        return None
    if _is_org_member(p, r.org_id):
        return Permission(True, "org member reads strategy")
    return None


def _allow_strategy_author_mutate(
    p: Principal, a: Action, r: Resource,
) -> Permission | None:
    """The author of a strategy can update or delete it, provided they're
    still a member of the owning org AND have at least operator-level role.

    Two gotchas this rule guards against:
      1. A former employee whose user_id matches an old strategy's author
         but who is no longer a member of the org must not be able to mutate.
      2. A viewer/auditor whose user_id somehow matches author (data-entry
         mistake or legacy record) must not gain edit rights — authorship
         alone is not the permission; their role in the org is.
    """

    if r.type != ResourceType.STRATEGY:
        return None
    if a not in (Action.UPDATE, Action.DELETE):
        return None
    if r.author_user_id is None:
        return None

    role = _role_in(p, r.org_id)
    if role not in (Role.OPERATOR, Role.ORGADMIN):
        return None

    if p.user_id == r.author_user_id:
        return Permission(True, "author mutates own strategy")
    if p.user_id in r.delegated_user_ids:
        return Permission(True, "co-author (ACL) mutates strategy")
    return None


def _allow_operator_create_strategy(
    p: Principal, a: Action, r: Resource,
) -> Permission | None:
    """Operators and orgadmins can create new strategies in their org.

    Viewers and auditors cannot — they're read-only shapes. The `Resource`
    passed at creation time should set `author_user_id` to the principal,
    but we don't require that here; that's the caller's responsibility.
    """

    if r.type != ResourceType.STRATEGY or a != Action.CREATE:
        return None
    role = _role_in(p, r.org_id)
    if role in (Role.OPERATOR, Role.ORGADMIN):
        return Permission(True, f"{role.value} creates strategy in org")
    return None


def _allow_orgadmin_any_strategy(
    p: Principal, a: Action, r: Resource,
) -> Permission | None:
    """Orgadmin mutates ANY strategy within an org they admin.

    Covers the case where an author leaves the org and someone else must
    clean up their strategies.
    """

    if r.type != ResourceType.STRATEGY:
        return None
    if a not in (Action.UPDATE, Action.DELETE):
        return None
    if _is_orgadmin(p, r.org_id):
        return Permission(True, "orgadmin mutates strategy in own org")
    return None


def _allow_orgadmin_user_mgmt(
    p: Principal, a: Action, r: Resource,
) -> Permission | None:
    """Orgadmin manages users and Alpaca secrets WITHIN their org(s).

    Note: orgadmins in org A cannot manage users in org B even if they're
    also orgadmin of B — each action is scoped to the resource's org.
    """

    if r.type not in (ResourceType.USER, ResourceType.ALPACA_SECRET):
        return None
    if _is_orgadmin(p, r.org_id):
        return Permission(True, f"orgadmin manages {r.type.value} in own org")
    return None


def _allow_sysadmin_user_mgmt(
    p: Principal, a: Action, r: Resource,
) -> Permission | None:
    """Sysadmin can manage users across the system (but not read Alpaca keys).

    Alpaca secrets remain isolated from sysadmin regardless of the deploy
    flag — customer API keys are off-limits even for operational support.
    """

    if not p.sysadmin:
        return None
    if r.type == ResourceType.USER:
        return Permission(True, "sysadmin manages user")
    return None


def _allow_org_create_by_sysadmin(
    p: Principal, a: Action, r: Resource,
) -> Permission | None:
    """Only sysadmin can create new orgs."""

    if r.type == ResourceType.ORG and a == Action.CREATE and p.sysadmin:
        return Permission(True, "sysadmin creates org")
    return None


def _allow_org_mutate_by_orgadmin(
    p: Principal, a: Action, r: Resource,
) -> Permission | None:
    """Orgadmin can update their own org's settings and delete the org."""

    if r.type != ResourceType.ORG:
        return None
    if a in (Action.UPDATE, Action.DELETE) and _is_orgadmin(p, r.org_id):
        return Permission(True, "orgadmin mutates own org")
    return None


def _allow_org_read_by_member(
    p: Principal, a: Action, r: Resource,
) -> Permission | None:
    """Any member of an org can read that org's metadata (name, settings)."""

    if r.type != ResourceType.ORG:
        return None
    if a in (Action.READ, Action.LIST) and _is_org_member(p, r.org_id):
        return Permission(True, "member reads own org")
    return None


def _allow_market_data_read(
    p: Principal, a: Action, r: Resource,
) -> Permission | None:
    """Market data is a shared island — every authenticated user reads it."""

    if r.type != ResourceType.MARKET_DATA:
        return None
    if a in (Action.READ, Action.LIST):
        return Permission(True, "any authenticated user reads market data")
    return None


# Chain order is deliberate: specific allows first, then broader ones.
_ALLOW_CHAIN: tuple[Predicate, ...] = (
    _allow_sysadmin_read,
    _allow_sysadmin_system_config,
    _allow_sysadmin_user_mgmt,
    _allow_strategy_author_mutate,
    _allow_orgadmin_any_strategy,
    _allow_operator_create_strategy,
    _allow_strategy_read,
    _allow_orgadmin_user_mgmt,
    _allow_org_create_by_sysadmin,
    _allow_org_mutate_by_orgadmin,
    _allow_org_read_by_member,
    _allow_market_data_read,
)


def can(principal: Principal, action: Action, resource: Resource) -> Permission:
    """Top-level authorization decision. Deny by default.

    Each predicate in the chain may return a Permission to allow, or None
    to abstain. The first allow wins. If all predicates abstain, we deny.
    The returned Permission is suitable for audit logging — the `reason`
    field names the allow rule that fired (or "no rule matched").
    """

    for predicate in _ALLOW_CHAIN:
        result = predicate(principal, action, resource)
        if result is not None:
            return result
    return Permission(False, "no rule matched — denied by default")


class Unauthorized(Exception):
    """Raised by `require()` when authorization fails. Carries the reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def require(
    principal: Principal, action: Action, resource: Resource,
) -> None:
    """Raise Unauthorized if the principal cannot perform the action."""

    perm = can(principal, action, resource)
    if not perm.allowed:
        raise Unauthorized(perm.reason)
