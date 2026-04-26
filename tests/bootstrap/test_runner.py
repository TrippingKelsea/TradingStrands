"""Tests for bootstrap — idempotency and clean-slate guarantees."""

from __future__ import annotations

import json
from typing import Any

import boto3
from moto import mock_aws

from trading_strands.alpaca_secrets.store import (
    AlpacaSecretsStore,
    secret_name_for,
)
from trading_strands.authz.model import Role
from trading_strands.bootstrap.runner import (
    LEGACY_ALPACA_SECRET_NAME,
    SUPERWOMAN_EMAIL,
    SYSTEM_ORG_NAME,
    bootstrap,
    delete_legacy_strategies,
    ensure_superwoman_user,
    ensure_system_org,
    seed_system_org_alpaca,
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
    assert report.superwoman_sysadmin_granted is True
    assert report.legacy_strategies_deleted == 0
    assert report.system_org_alpaca_seeded is False  # no SM client passed

    tenancy = TenancyStore(table)
    role = tenancy.role_of(report.superwoman.user_id, report.system_org.org_id)
    assert role == Role.ORGADMIN
    assert tenancy.is_sysadmin(report.superwoman.user_id) is True


def test_bootstrap_is_idempotent(table: Any) -> None:
    first = bootstrap(table)
    second = bootstrap(table)

    assert second.system_org.org_id == first.system_org.org_id
    assert second.system_org_created is False
    assert second.superwoman.user_id == first.superwoman.user_id
    assert second.superwoman_created is False
    assert second.superwoman_membership_added is False
    assert second.superwoman_sysadmin_granted is False  # already granted
    assert second.legacy_strategies_deleted == 0


def test_bootstrap_does_not_regrant_sysadmin_after_revocation(
    table: Any,
) -> None:
    """If sysadmin is deliberately revoked, bootstrap must NOT re-grant on
    the next run. Grant-on-first-create, never grant-on-rerun — otherwise
    an operator can't demote the default account."""

    first = bootstrap(table)
    assert first.superwoman_sysadmin_granted is True

    tenancy = TenancyStore(table)
    tenancy.revoke_sysadmin(first.superwoman.user_id)

    second = bootstrap(table)
    assert second.superwoman_sysadmin_granted is False
    assert tenancy.is_sysadmin(first.superwoman.user_id) is False


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
    """If the user exists but isn't a member of system org, add the membership.

    Critically: a pre-existing user that's missing the membership does
    NOT get sysadmin auto-granted — grant is first-create-only. The
    operator who made the user out-of-band had their chance to grant
    sysadmin explicitly; bootstrap won't elevate them retroactively.
    """

    tenancy = TenancyStore(table)
    system_org = tenancy.create_org(SYSTEM_ORG_NAME, OrgType.SYSTEM)
    sw = tenancy.create_user(email=SUPERWOMAN_EMAIL)
    assert tenancy.role_of(sw.user_id, system_org.org_id) is None

    user, created, added, sysadmin_granted = ensure_superwoman_user(
        tenancy, system_org,
    )
    assert user.user_id == sw.user_id
    assert created is False
    assert added is True
    assert sysadmin_granted is False  # pre-existing user: not granted
    assert tenancy.role_of(user.user_id, system_org.org_id) == Role.ORGADMIN
    assert tenancy.is_sysadmin(user.user_id) is False


# ── Alpaca secret seeding ─────────────────────────────────────────────


def _seed_legacy_secret(
    sm: Any, api_key: str = "K", secret_key: str = "S", paper: bool = True,
) -> None:
    """Create the pre-refactor global trading-strands/alpaca secret."""

    sm.create_secret(
        Name=LEGACY_ALPACA_SECRET_NAME,
        SecretString=json.dumps({
            "ALPACA_API_KEY": api_key,
            "ALPACA_SECRET_KEY": secret_key,
            "ALPACA_PAPER": "true" if paper else "false",
        }),
    )


def test_bootstrap_seeds_system_org_alpaca_from_legacy(table: Any) -> None:
    with mock_aws():
        sm = boto3.client("secretsmanager", region_name="us-west-2")
        _seed_legacy_secret(sm, api_key="PAPER_K", secret_key="PAPER_S", paper=True)

        report = bootstrap(table, secretsmanager_client=sm)
        assert report.system_org_alpaca_seeded is True

        status = AlpacaSecretsStore(sm).status(report.system_org.org_id)
        assert status.configured is True
        assert status.paper is True
        # Confirm the per-org secret exists at the expected path.
        name = secret_name_for(report.system_org.org_id)
        resp = sm.get_secret_value(SecretId=name)
        payload = json.loads(resp["SecretString"])
        assert payload["ALPACA_API_KEY"] == "PAPER_K"
        assert payload["ALPACA_SECRET_KEY"] == "PAPER_S"


def test_bootstrap_alpaca_seed_is_noop_if_per_org_already_set(
    table: Any,
) -> None:
    """Never overwrite an orgadmin's manually-set creds on redeploy."""

    with mock_aws():
        sm = boto3.client("secretsmanager", region_name="us-west-2")
        _seed_legacy_secret(sm, api_key="OLD_K", secret_key="OLD_S")

        # First run: seeding happens.
        first = bootstrap(table, secretsmanager_client=sm)
        assert first.system_org_alpaca_seeded is True

        # Orgadmin changes the creds through the UI (simulated here).
        store = AlpacaSecretsStore(sm)
        store.upsert(
            first.system_org.org_id,
            api_key="NEW_K", secret_key="NEW_S", paper=False,
        )

        # Even if legacy secret still exists, second run must not overwrite.
        second = bootstrap(table, secretsmanager_client=sm)
        assert second.system_org_alpaca_seeded is False
        status = store.status(first.system_org.org_id)
        assert status.paper is False  # operator's choice preserved


def test_bootstrap_alpaca_seed_noop_without_legacy_secret(
    table: Any,
) -> None:
    """If the legacy secret is missing entirely, seeding skips silently.

    This matters for genuinely fresh deploys where CI hasn't seeded the
    global secret yet — bootstrap shouldn't crash."""

    with mock_aws():
        sm = boto3.client("secretsmanager", region_name="us-west-2")

        report = bootstrap(table, secretsmanager_client=sm)
        assert report.system_org_alpaca_seeded is False


def test_seed_system_org_alpaca_handles_invalid_legacy_json() -> None:
    with mock_aws():
        sm = boto3.client("secretsmanager", region_name="us-west-2")
        sm.create_secret(
            Name=LEGACY_ALPACA_SECRET_NAME, SecretString="not json",
        )
        # Need a system org to pass in.
        from trading_strands.tenancy.models import Org
        fake_org = Org(
            org_id="sys1", name="sys", org_type=OrgType.SYSTEM,
            created_at=1, updated_at=1,
        )
        assert seed_system_org_alpaca(sm, fake_org) is False
