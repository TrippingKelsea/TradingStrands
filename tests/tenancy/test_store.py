"""Tests for TenancyStore — users, orgs, memberships, sysadmin."""

from __future__ import annotations

from typing import Any

import pytest

from trading_strands.authz.model import Role
from trading_strands.tenancy.models import OrgType
from trading_strands.tenancy.store import (
    EmailAlreadyInUseError,
    NotFoundError,
    TenancyStore,
)

# ── Orgs ──────────────────────────────────────────────────────────────


def test_create_and_get_org(table: Any) -> None:
    store = TenancyStore(table)
    org = store.create_org("Acme Trading")
    assert org.name == "Acme Trading"
    assert org.org_type == OrgType.CUSTOMER

    fetched = store.get_org(org.org_id)
    assert fetched.org_id == org.org_id
    assert fetched.name == "Acme Trading"


def test_create_system_org(table: Any) -> None:
    store = TenancyStore(table)
    org = store.create_org("Women with Super Powers", OrgType.SYSTEM)
    assert org.org_type == OrgType.SYSTEM
    assert store.find_system_org() is not None
    assert store.find_system_org().org_id == org.org_id


def test_find_system_org_none_when_no_system_org_exists(table: Any) -> None:
    store = TenancyStore(table)
    store.create_org("Acme")  # customer org only
    assert store.find_system_org() is None


def test_get_missing_org_raises(table: Any) -> None:
    store = TenancyStore(table)
    with pytest.raises(NotFoundError):
        store.get_org("does_not_exist")


def test_list_orgs_returns_all(table: Any) -> None:
    store = TenancyStore(table)
    store.create_org("A")
    store.create_org("B")
    store.create_org("C")
    orgs = store.list_orgs()
    assert len(orgs) == 3
    assert {o.name for o in orgs} == {"A", "B", "C"}


# ── Users ─────────────────────────────────────────────────────────────


def test_create_and_get_user(table: Any) -> None:
    store = TenancyStore(table)
    user = store.create_user(email="alice@example.com", cognito_sub="cog123")
    assert user.email == "alice@example.com"
    assert user.cognito_sub == "cog123"

    fetched = store.get_user(user.user_id)
    assert fetched.email == "alice@example.com"


def test_email_uniqueness_enforced(table: Any) -> None:
    store = TenancyStore(table)
    store.create_user(email="alice@example.com")
    with pytest.raises(EmailAlreadyInUseError):
        store.create_user(email="alice@example.com")


def test_email_uniqueness_is_case_insensitive(table: Any) -> None:
    store = TenancyStore(table)
    store.create_user(email="Alice@Example.com")
    with pytest.raises(EmailAlreadyInUseError):
        store.create_user(email="alice@example.com")
    with pytest.raises(EmailAlreadyInUseError):
        store.create_user(email="ALICE@EXAMPLE.COM")


def test_find_user_by_email_returns_user(table: Any) -> None:
    store = TenancyStore(table)
    user = store.create_user(email="alice@example.com")
    found = store.find_user_by_email("alice@example.com")
    assert found is not None
    assert found.user_id == user.user_id


def test_find_user_by_email_case_insensitive(table: Any) -> None:
    store = TenancyStore(table)
    user = store.create_user(email="Alice@Example.com")
    found = store.find_user_by_email("ALICE@example.COM")
    assert found is not None
    assert found.user_id == user.user_id


def test_find_user_by_email_returns_none_when_missing(table: Any) -> None:
    store = TenancyStore(table)
    assert store.find_user_by_email("nobody@example.com") is None


def test_set_last_active_org(table: Any) -> None:
    store = TenancyStore(table)
    user = store.create_user(email="alice@example.com")
    org = store.create_org("Acme")
    store.set_last_active_org(user.user_id, org.org_id)
    refreshed = store.get_user(user.user_id)
    assert refreshed.last_active_org_id == org.org_id


def test_list_users(table: Any) -> None:
    store = TenancyStore(table)
    store.create_user(email="a@x.com")
    store.create_user(email="b@x.com")
    users = store.list_users()
    assert len(users) == 2


# ── Membership ────────────────────────────────────────────────────────


def test_add_membership_writes_both_sides(table: Any) -> None:
    store = TenancyStore(table)
    user = store.create_user(email="alice@example.com")
    org = store.create_org("Acme")
    store.add_membership(user.user_id, org.org_id, Role.OPERATOR)

    user_memberships = store.memberships_for_user(user.user_id)
    org_memberships = store.memberships_for_org(org.org_id)
    assert len(user_memberships) == 1
    assert len(org_memberships) == 1
    assert user_memberships[0].role == Role.OPERATOR


def test_many_to_many_user_in_multiple_orgs(table: Any) -> None:
    store = TenancyStore(table)
    alice = store.create_user(email="alice@example.com")
    org_a = store.create_org("A")
    org_b = store.create_org("B")

    store.add_membership(alice.user_id, org_a.org_id, Role.OPERATOR)
    store.add_membership(alice.user_id, org_b.org_id, Role.ORGADMIN)

    memberships = store.memberships_for_user(alice.user_id)
    by_org = {m.org_id: m.role for m in memberships}
    assert by_org[org_a.org_id] == Role.OPERATOR
    assert by_org[org_b.org_id] == Role.ORGADMIN


def test_many_to_many_multiple_users_in_one_org(table: Any) -> None:
    store = TenancyStore(table)
    alice = store.create_user(email="alice@x.com")
    bob = store.create_user(email="bob@x.com")
    org = store.create_org("Shared")

    store.add_membership(alice.user_id, org.org_id, Role.OPERATOR)
    store.add_membership(bob.user_id, org.org_id, Role.VIEWER)

    members = store.memberships_for_org(org.org_id)
    assert len(members) == 2
    by_user = {m.user_id: m.role for m in members}
    assert by_user[alice.user_id] == Role.OPERATOR
    assert by_user[bob.user_id] == Role.VIEWER


def test_role_of_returns_role(table: Any) -> None:
    store = TenancyStore(table)
    alice = store.create_user(email="alice@x.com")
    org = store.create_org("Acme")
    store.add_membership(alice.user_id, org.org_id, Role.AUDITOR)
    assert store.role_of(alice.user_id, org.org_id) == Role.AUDITOR


def test_role_of_returns_none_for_non_member(table: Any) -> None:
    store = TenancyStore(table)
    alice = store.create_user(email="alice@x.com")
    org = store.create_org("Acme")
    assert store.role_of(alice.user_id, org.org_id) is None


def test_remove_membership_removes_both_sides(table: Any) -> None:
    store = TenancyStore(table)
    alice = store.create_user(email="alice@x.com")
    org = store.create_org("Acme")
    store.add_membership(alice.user_id, org.org_id, Role.OPERATOR)
    store.remove_membership(alice.user_id, org.org_id)
    assert store.memberships_for_user(alice.user_id) == []
    assert store.memberships_for_org(org.org_id) == []


# ── Sysadmin ──────────────────────────────────────────────────────────


def test_sysadmin_default_false(table: Any) -> None:
    store = TenancyStore(table)
    alice = store.create_user(email="alice@x.com")
    assert store.is_sysadmin(alice.user_id) is False


def test_grant_and_revoke_sysadmin(table: Any) -> None:
    store = TenancyStore(table)
    alice = store.create_user(email="alice@x.com")
    store.grant_sysadmin(alice.user_id)
    assert store.is_sysadmin(alice.user_id) is True
    store.revoke_sysadmin(alice.user_id)
    assert store.is_sysadmin(alice.user_id) is False
