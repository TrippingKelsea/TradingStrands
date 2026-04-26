"""Tests for the Principal loader."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from trading_strands.authz.model import Role
from trading_strands.dashboard.principal import (
    SessionInvalidError,
    active_org_id,
    principal_from_session,
)
from trading_strands.tenancy.store import TenancyStore


@pytest.fixture
def table() -> Iterator[object]:
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-west-2")
        client.create_table(
            TableName="trading-strands-state",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client.Table("trading-strands-state")


def test_session_without_user_id_is_invalid(table: Any) -> None:
    with pytest.raises(SessionInvalidError):
        principal_from_session({}, table)


def test_session_with_unknown_user_is_invalid(table: Any) -> None:
    with pytest.raises(SessionInvalidError):
        principal_from_session({"user_id": "ghost"}, table)


def test_principal_loaded_with_memberships(table: Any) -> None:
    store = TenancyStore(table)
    alice = store.create_user(email="alice@x.com")
    org = store.create_org("Acme")
    store.add_membership(alice.user_id, org.org_id, Role.OPERATOR)

    p = principal_from_session({"user_id": alice.user_id}, table)
    assert p.user_id == alice.user_id
    assert p.email == "alice@x.com"
    assert p.memberships == {org.org_id: Role.OPERATOR}
    assert p.sysadmin is False


def test_sysadmin_flag_reflects_sentinel(table: Any) -> None:
    store = TenancyStore(table)
    alice = store.create_user(email="alice@x.com")
    store.grant_sysadmin(alice.user_id)

    p = principal_from_session({"user_id": alice.user_id}, table)
    assert p.sysadmin is True


def test_active_org_honors_explicit_claim(table: Any) -> None:
    store = TenancyStore(table)
    alice = store.create_user(email="alice@x.com")
    org_a = store.create_org("A")
    org_b = store.create_org("B")
    store.add_membership(alice.user_id, org_a.org_id, Role.OPERATOR)
    store.add_membership(alice.user_id, org_b.org_id, Role.VIEWER)

    p = principal_from_session({"user_id": alice.user_id}, table)
    assert active_org_id({"active_org_id": org_b.org_id}, p) == org_b.org_id


def test_active_org_rejects_claim_for_non_member_org(table: Any) -> None:
    """Privacy guard: if the session claims an org the user isn't in,
    we refuse to honor the claim."""

    store = TenancyStore(table)
    alice = store.create_user(email="alice@x.com")
    org_a = store.create_org("A")
    store.create_org("B")  # alice not a member
    store.add_membership(alice.user_id, org_a.org_id, Role.OPERATOR)

    p = principal_from_session({"user_id": alice.user_id}, table)
    # Alice claims org B but isn't a member. Should fall back to the only
    # org she IS in.
    assert active_org_id({"active_org_id": "org_b_fake"}, p) == org_a.org_id


def test_active_org_single_membership_autoselected(table: Any) -> None:
    store = TenancyStore(table)
    alice = store.create_user(email="alice@x.com")
    org = store.create_org("A")
    store.add_membership(alice.user_id, org.org_id, Role.OPERATOR)

    p = principal_from_session({"user_id": alice.user_id}, table)
    assert active_org_id({}, p) == org.org_id


def test_active_org_none_when_multiple_and_no_claim(table: Any) -> None:
    store = TenancyStore(table)
    alice = store.create_user(email="alice@x.com")
    org_a = store.create_org("A")
    org_b = store.create_org("B")
    store.add_membership(alice.user_id, org_a.org_id, Role.OPERATOR)
    store.add_membership(alice.user_id, org_b.org_id, Role.VIEWER)

    p = principal_from_session({"user_id": alice.user_id}, table)
    assert active_org_id({}, p) is None


def test_active_org_none_when_no_memberships(table: Any) -> None:
    store = TenancyStore(table)
    alice = store.create_user(email="alice@x.com")
    p = principal_from_session({"user_id": alice.user_id}, table)
    assert active_org_id({}, p) is None
