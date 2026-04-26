"""Tenancy — users, orgs, and many-to-many memberships.

This module owns the DynamoDB persistence for identity and org membership.
Cognito is the authoritative store for auth credentials; DynamoDB is the
authoritative store for user↔org relationships and per-org roles.

Schema (all in the single `trading-strands-state` table, partitioned by `pk`):

    USER#{user_id}           — a user profile. Keyed by our own user_id
                               (not Cognito sub) so we own the identifier
                               and can survive Cognito pool rebuilds.
    USEREMAIL#{email_lower}  — email→user_id index (unique; enforced via
                               conditional put). Lower-cased for
                               case-insensitive login.
    ORG#{org_id}             — org metadata (name, org_type, settings).
    USERORG#{user_id}#{org_id}
                             — membership join. Carries the per-org role.
                               (user_id first so list-orgs-for-user is a
                               cheap begins_with query.)
    ORGUSER#{org_id}#{user_id}
                             — reverse index. (org_id first so list-users-
                               in-org is a cheap begins_with query.)
    SYSADMIN#{user_id}       — sentinel. Presence means the user is a
                               sysadmin. Absence is the deny default.

The join is written with both USERORG and ORGUSER items so we avoid GSI
costs and avoid hot partitions.

All functions here are pure-ish: they take a DynamoDB table handle and
return plain dicts / domain objects. No FastAPI, no authz, no Cognito.
"""

from trading_strands.tenancy.models import (
    Membership,
    Org,
    OrgType,
    User,
)
from trading_strands.tenancy.store import TenancyStore

__all__ = [
    "Membership",
    "Org",
    "OrgType",
    "TenancyStore",
    "User",
]
