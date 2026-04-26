"""Authorization — roles, permissions, and deny-by-default policy.

This module is the single source of truth for who can do what. It knows
nothing about HTTP, FastAPI, Cognito, or DynamoDB. It answers one question:
given a `Principal` and an `Action` on a `Resource`, is the action allowed?

Invariants (enforced by tests):

1. Deny by default. `can()` returns False unless an explicit rule allows.
2. `Role` is per-org. A principal's global identity is ONLY `sysadmin` (a
   flag, not a role). All other capabilities flow from a (user, org, role)
   triple stored in a `USERORG#` item.
3. `sysadmin` is read-mostly. Cross-org write operations on customer data
   require an explicit deploy flag `sysadmin_can_read_org_data=true`.
4. A strategy's author is the only non-admin who can mutate it. Edit rights
   to a strategy can be delegated via a `STRATEGYACL#` item.
5. Resources are org-scoped. A `Resource` without an `org_id` can only be
   acted on by sysadmin.
"""

from trading_strands.authz.model import (
    Action,
    Permission,
    Principal,
    Resource,
    ResourceType,
    Role,
)
from trading_strands.authz.policy import can, require

__all__ = [
    "Action",
    "Permission",
    "Principal",
    "Resource",
    "ResourceType",
    "Role",
    "can",
    "require",
]
