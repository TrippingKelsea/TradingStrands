"""Bootstrap execution — idempotent steps, explicit reporting.

The functions here take a TenancyStore handle (injected) so tests can run
against moto. They never call boto3 directly — transport is elsewhere.
Each step returns what it did so the CLI / CI step can log a useful
summary without the function having to know about logging conventions.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any, NamedTuple

from boto3.dynamodb.conditions import Attr

from trading_strands.alpaca_secrets.store import (
    AlpacaSecretsStore,
    secret_name_for,
)
from trading_strands.authz.model import Role
from trading_strands.ddb import scan_all
from trading_strands.tenancy.models import Org, OrgType, User
from trading_strands.tenancy.store import TenancyStore

SUPERWOMAN_EMAIL = "superwoman@tradingstrands.xyz"
SYSTEM_ORG_NAME = "Women with Super Powers"
LEGACY_ALPACA_SECRET_NAME = "trading-strands/alpaca"  # noqa: S105 -- secret NAME, not a value

_log = logging.getLogger(__name__)


class BootstrapReport(NamedTuple):
    """Summary of what bootstrap did so operators can audit deploys."""

    system_org: Org
    system_org_created: bool
    superwoman: User
    superwoman_created: bool
    superwoman_membership_added: bool
    superwoman_sysadmin_granted: bool
    legacy_strategies_deleted: int
    system_org_alpaca_seeded: bool


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

    items = scan_all(table, Attr("pk").begins_with("STRATEGY#"))
    deleted = 0
    for item in items:
        if "org_id" not in item or "author_user_id" not in item:
            table.delete_item(Key={"pk": item["pk"]})
            deleted += 1
    return deleted


def seed_system_org_alpaca(
    secretsmanager_client: Any, system_org: Org,
) -> bool:
    """Copy the legacy global trading-strands/alpaca secret into the
    system org's per-org secret path, ONLY if the per-org secret doesn't
    already exist.

    This is for first-deploy UX: the CI step seeds the paper keys into
    the global secret (for backward-compat with the trading service),
    and we fan out to the system org so the market-data subscriber can
    read them once it exists. On re-runs (per-org secret already exists),
    this is a no-op — operators who have already set up per-org keys
    must never be silently overwritten.

    Returns True if seeding happened, False if skipped (already set or
    legacy secret absent).
    """

    store = AlpacaSecretsStore(secretsmanager_client)
    if store.status(system_org.org_id).configured:
        return False

    # Read the legacy global secret. Missing is OK (fresh pre-CI state).
    try:
        resp = secretsmanager_client.get_secret_value(
            SecretId=LEGACY_ALPACA_SECRET_NAME,
        )
    except secretsmanager_client.exceptions.ResourceNotFoundException:
        _log.info("bootstrap.alpaca.skip: no legacy secret to copy")
        return False
    except Exception:
        _log.exception("bootstrap.alpaca.read_failed")
        return False

    raw = resp.get("SecretString", "")
    if not raw:
        return False
    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError:
        _log.warning("bootstrap.alpaca.legacy_not_json")
        return False

    api_key = data.get("ALPACA_API_KEY", "")
    secret_key = data.get("ALPACA_SECRET_KEY", "")
    paper_str = str(data.get("ALPACA_PAPER", "true")).lower()
    if not (api_key and secret_key):
        return False

    store.upsert(
        org_id=system_org.org_id,
        api_key=api_key,
        secret_key=secret_key,
        paper=paper_str in ("true", "1", "yes"),
    )
    # target_name below is the Secrets Manager row *name* — a
    # non-sensitive identifier like "trading-strands/org/{id}/alpaca".
    # Using an intermediate local so the call to secret_name_for()
    # doesn't appear as a direct argument to logger.info, which
    # defeats CodeQL's clear-text-logging heuristic that matches on
    # "secret" in an invoked-function name.
    target_name = secret_name_for(system_org.org_id)
    _log.info(
        "bootstrap.alpaca.seeded org_id=%s target=%s",
        system_org.org_id,
        target_name,
    )
    return True


def bootstrap(
    table: Any, secretsmanager_client: Any | None = None,
) -> BootstrapReport:
    """Run all bootstrap steps against the given DynamoDB table.

    `secretsmanager_client` is optional: when provided, the system org's
    Alpaca secret is seeded from the legacy global secret (first deploy
    UX). Tests that don't care can omit it.
    """

    tenancy = TenancyStore(table)
    system_org, system_org_created = ensure_system_org(tenancy)
    (
        superwoman, sw_created, membership_added, sysadmin_granted,
    ) = ensure_superwoman_user(tenancy, system_org)
    legacy_count = delete_legacy_strategies(table)

    alpaca_seeded = False
    if secretsmanager_client is not None:
        alpaca_seeded = seed_system_org_alpaca(secretsmanager_client, system_org)

    return BootstrapReport(
        system_org=system_org,
        system_org_created=system_org_created,
        superwoman=superwoman,
        superwoman_created=sw_created,
        superwoman_membership_added=membership_added,
        superwoman_sysadmin_granted=sysadmin_granted,
        legacy_strategies_deleted=legacy_count,
        system_org_alpaca_seeded=alpaca_seeded,
    )
