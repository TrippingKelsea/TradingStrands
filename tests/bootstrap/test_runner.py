"""Tests for bootstrap — idempotency and clean-slate guarantees."""

from __future__ import annotations

from typing import Any

from trading_strands.authz.model import Role
from trading_strands.bootstrap.runner import (
    SUPERWOMAN_EMAIL,
    SYSTEM_ORG_NAME,
    bootstrap,
    delete_legacy_strategies,
    ensure_superwoman_user,
    ensure_system_org,
)
from trading_strands.tenancy.models import OrgType
from trading_strands.tenancy.store import TenancyStore


def test_bootstrap_on_empty_table_creates_everything(table: Any) -> None:
    report = bootstrap(table)
    assert report.system_org.name == SYSTEM_ORG_NAME
    assert report.system_org.org_type == OrgType.SYSTEM
    assert report.system_org_created is True
    assert report.superwoman.email == SUPERWOMAN_EMAIL
    assert report.superwoman_created is True
    assert report.superwoman_membership_added is True
    assert report.legacy_strategies_deleted == 0

    tenancy = TenancyStore(table)
    role = tenancy.role_of(report.superwoman.user_id, report.system_org.org_id)
    assert role == Role.ORGADMIN


def test_bootstrap_is_idempotent(table: Any) -> None:
    first = bootstrap(table)
    second = bootstrap(table)

    # Second run should find everything and report no creation.
    assert second.system_org.org_id == first.system_org.org_id
    assert second.system_org_created is False
    assert second.superwoman.user_id == first.superwoman.user_id
    assert second.superwoman_created is False
    assert second.superwoman_membership_added is False
    assert second.legacy_strategies_deleted == 0


def test_bootstrap_does_not_grant_sysadmin(table: Any) -> None:
    """Explicit invariant: superwoman is NOT automatically sysadmin."""

    report = bootstrap(table)
    tenancy = TenancyStore(table)
    assert tenancy.is_sysadmin(report.superwoman.user_id) is False


def test_bootstrap_deletes_legacy_strategies(table: Any) -> None:
    # Seed a legacy STRATEGY# item (missing org_id and author_user_id).
    table.put_item(Item={
        "pk": "STRATEGY#legacy-1",
        "strategy_id": "legacy-1",
        "name": "Pre-refactor",
        "markdown": "",
        "status": "active",
        "created_at": 1,
        "updated_at": 1,
    })

    report = bootstrap(table)
    assert report.legacy_strategies_deleted == 1

    # Re-run: nothing to delete.
    second = bootstrap(table)
    assert second.legacy_strategies_deleted == 0


def test_bootstrap_preserves_valid_strategies(table: Any) -> None:
    """Strategies with org_id + author_user_id must NOT be touched."""

    report = bootstrap(table)
    table.put_item(Item={
        "pk": "STRATEGY#keep-me",
        "strategy_id": "keep-me",
        "org_id": report.system_org.org_id,
        "author_user_id": report.superwoman.user_id,
        "name": "Valid",
        "markdown": "",
        "symbols": [],
        "capital": "1000",
        "status": "active",
        "created_at": 1,
        "updated_at": 1,
    })

    deleted = delete_legacy_strategies(table)
    assert deleted == 0
    resp = table.get_item(Key={"pk": "STRATEGY#keep-me"})
    assert resp.get("Item") is not None


def test_ensure_system_org_returns_existing(table: Any) -> None:
    tenancy = TenancyStore(table)
    org1, created1 = ensure_system_org(tenancy)
    assert created1 is True
    org2, created2 = ensure_system_org(tenancy)
    assert created2 is False
    assert org2.org_id == org1.org_id


def test_bootstrap_tolerates_legacy_org_rows(table: Any) -> None:
    """ORG# rows written by the pre-refactor API carry extra columns
    (session_max_age, no org_type). They must load without error so
    a system-org check on an existing deploy doesn't crash."""

    import time
    table.put_item(Item={
        "pk": "ORG#legacy-1",
        "org_id": "legacy-1",
        "name": "Pre-refactor Org",
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
        "session_max_age": 31_536_000,
        # deliberately missing org_type
    })
    # bootstrap scans orgs; this must not raise
    report = bootstrap(table)
    assert report.system_org is not None


def test_ensure_superwoman_user_adds_missing_membership(table: Any) -> None:
    """If the user exists but isn't a member of system org, add the membership."""

    tenancy = TenancyStore(table)
    # Create system org without superwoman
    system_org = tenancy.create_org(SYSTEM_ORG_NAME, OrgType.SYSTEM)
    # Create the user, no membership
    sw = tenancy.create_user(email=SUPERWOMAN_EMAIL)
    assert tenancy.role_of(sw.user_id, system_org.org_id) is None

    user, created, added = ensure_superwoman_user(tenancy, system_org)
    assert user.user_id == sw.user_id
    assert created is False
    assert added is True
    assert tenancy.role_of(user.user_id, system_org.org_id) == Role.ORGADMIN
