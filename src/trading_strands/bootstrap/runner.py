"""Bootstrap execution — idempotent steps, explicit reporting.

The functions here take a TenancyStore handle (injected) so tests can run
against moto. They never call boto3 directly — transport is elsewhere.
Each step returns what it did so the CLI / CI step can log a useful
summary without the function having to know about logging conventions.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from boto3.dynamodb.conditions import Attr

from trading_strands.authz.model import Role
from trading_strands.tenancy.models import Org, OrgType, User
from trading_strands.tenancy.store import TenancyStore

SUPERWOMAN_EMAIL = "superwoman@tradingstrands.xyz"
SYSTEM_ORG_NAME = "Women with Super Powers"


class BootstrapReport(NamedTuple):
    """Summary of what bootstrap did so operators can audit deploys."""

    system_org: Org
    system_org_created: bool
    superwoman: User
    superwoman_created: bool
    superwoman_membership_added: bool
    superwoman_sysadmin_granted: bool
    legacy_strategies_deleted: int


def ensure_system_org(tenancy: TenancyStore) -> tuple[Org, bool]:
    """Return the system org, creating it if absent.

    Idempotent: if a system org already exists, returns it unchanged. If
    there's somehow more than one system org (shouldn't happen but we
    don't want to corrupt on edge cases), returns the first and leaves
    the rest alone — cleanup of duplicates is out of scope.
    """

    existing = tenancy.find_system_org()
    if existing is not None:
        return existing, False
    org = tenancy.create_org(SYSTEM_ORG_NAME, OrgType.SYSTEM)
    return org, True


def ensure_superwoman_user(
    tenancy: TenancyStore, system_org: Org,
) -> tuple[User, bool, bool, bool]:
    """Return (user, created, membership_added, sysadmin_granted).

    First-deploy behavior: if superwoman doesn't exist yet, create her,
    add her as orgadmin of the system org, AND grant sysadmin. This is
    how a fresh deploy produces an operable account without the operator
    having to touch DynamoDB by hand.

    Re-run safety: if superwoman already exists, we DON'T re-grant
    sysadmin. Once someone deliberately revokes sysadmin (to demote the
    default account), bootstrap must not un-do that on the next deploy.
    Grant-on-first-create, never grant-on-rerun, is the rule.

    Does NOT touch Cognito — Cognito provisioning happens in CI after
    CDK deploy. If Cognito has no superwoman user, login will fail; if
    Cognito has one, login will find-or-create the matching USER# and
    this pre-created record ensures the user_id is stable across deploys.
    """

    existing = tenancy.find_user_by_email(SUPERWOMAN_EMAIL)
    if existing is not None:
        user = existing
        created = False
    else:
        user = tenancy.create_user(email=SUPERWOMAN_EMAIL)
        created = True

    current_role = tenancy.role_of(user.user_id, system_org.org_id)
    if current_role is None:
        tenancy.add_membership(user.user_id, system_org.org_id, Role.ORGADMIN)
        membership_added = True
    else:
        membership_added = False

    # Sysadmin grant: ONLY on first creation. Do not re-grant on re-runs,
    # so a revocation stays sticky.
    sysadmin_granted = False
    if created and not tenancy.is_sysadmin(user.user_id):
        tenancy.grant_sysadmin(user.user_id)
        sysadmin_granted = True

    return user, created, membership_added, sysadmin_granted


def delete_legacy_strategies(table: Any) -> int:
    """Remove STRATEGY# items that predate the refactor.

    Identified by the absence of `org_id` or `author_user_id` — those are
    required on the v2 schema. Returns the count deleted.

    We delete (not migrate) because:
      - This is dev/pre-alpha; legacy rows are test data.
      - Migrating would require inventing an owner for each, which would
        either lie (about authorship) or pool all legacy rows under the
        superwoman account, muddying the clean-slate property.
    """

    resp = table.scan(
        FilterExpression=Attr("pk").begins_with("STRATEGY#"),
    )
    deleted = 0
    for item in resp.get("Items", []):
        if "org_id" not in item or "author_user_id" not in item:
            table.delete_item(Key={"pk": item["pk"]})
            deleted += 1
    return deleted


def bootstrap(table: Any) -> BootstrapReport:
    """Run all bootstrap steps against the given DynamoDB table."""

    tenancy = TenancyStore(table)
    system_org, system_org_created = ensure_system_org(tenancy)
    (
        superwoman, sw_created, membership_added, sysadmin_granted,
    ) = ensure_superwoman_user(tenancy, system_org)
    legacy_count = delete_legacy_strategies(table)
    return BootstrapReport(
        system_org=system_org,
        system_org_created=system_org_created,
        superwoman=superwoman,
        superwoman_created=sw_created,
        superwoman_membership_added=membership_added,
        superwoman_sysadmin_granted=sysadmin_granted,
        legacy_strategies_deleted=legacy_count,
    )
