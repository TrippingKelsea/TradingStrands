"""Core authorization types. Pure data, no side effects."""

from __future__ import annotations

from enum import StrEnum
from typing import NamedTuple


class Role(StrEnum):
    """Per-org role. A user's capabilities in org X depend on their role in X.

    VIEWER and OPERATOR deliberately have no visibility into other users —
    they cannot list org members. AUDITOR is like VIEWER but can see strategy
    authorship (for compliance review). ORGADMIN manages users + secrets
    within their org(s). SYSADMIN is NOT here — it's a global flag on
    `Principal.sysadmin`, not a per-org role.
    """

    VIEWER = "viewer"
    OPERATOR = "operator"
    AUDITOR = "auditor"
    ORGADMIN = "orgadmin"


class ResourceType(StrEnum):
    """What kind of thing is being acted on."""

    STRATEGY = "strategy"
    ORG = "org"
    USER = "user"
    ALPACA_SECRET = "alpaca_secret"  # noqa: S105 -- resource type name, not a credential
    COST_DATA = "cost_data"
    INFRA_TELEMETRY = "infra_telemetry"
    MARKET_DATA = "market_data"
    SYSTEM_CONFIG = "system_config"


class Action(StrEnum):
    """What's being attempted. Kept deliberately coarse — we don't need
    more granularity than this right now."""

    READ = "read"
    LIST = "list"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class Resource(NamedTuple):
    """A thing the principal wants to act on.

    `org_id` is the owning org; None means the resource is system-wide
    (e.g., infra telemetry) and only sysadmin can touch it.

    `author_user_id` is set for user-authored artifacts (strategies).
    The authorization policy uses this to enforce the "only the author
    can edit their own strategy" rule.
    """

    type: ResourceType
    org_id: str | None = None
    author_user_id: str | None = None
    # Users in STRATEGYACL#{id}#{user_id} items are treated as co-authors
    # for edit purposes. Populated by the caller from DynamoDB.
    delegated_user_ids: frozenset[str] = frozenset()


class Principal(NamedTuple):
    """The actor. `memberships` maps org_id -> role for every org they're
    a member of. `sysadmin` is a global flag that, combined with the deploy
    flag `sysadmin_can_read_org_data`, determines cross-org visibility.

    `sysadmin` is intentionally a separate field from role because:
      1. It's global, not per-org
      2. It's only assignable to members of the system org
      3. Keeping it separate makes it impossible to accidentally grant
         sysadmin via a normal role update
    """

    user_id: str
    email: str
    memberships: dict[str, Role]  # org_id -> role
    sysadmin: bool = False


class Permission(NamedTuple):
    """Outcome of an authorization check. Use `allowed` for gating and
    `reason` for audit logs / error messages."""

    allowed: bool
    reason: str
