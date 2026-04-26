"""Tests for StrategyStore.

Key guarantees covered here:
  - Cross-org reads are physically impossible via list_for_org
  - org_id and author_user_id are immutable after create
  - ACL add/remove plumbs through to resource_for's delegated_user_ids
"""

from __future__ import annotations

from typing import Any

import pytest

from trading_strands.authz.model import Principal, Role
from trading_strands.strategies_store.store import (
    StrategyNotFoundError,
    StrategyStatus,
    StrategyStore,
    can_perform,
    resource_for,
)


def _store(table: Any) -> StrategyStore:
    return StrategyStore(table)


def test_create_and_get(table: Any) -> None:
    s = _store(table)
    strat = s.create(
        org_id="org_a", author_user_id="alice",
        name="Momentum", markdown="# rules",
    )
    assert strat.org_id == "org_a"
    assert strat.author_user_id == "alice"
    assert strat.status == StrategyStatus.ACTIVE

    fetched = s.get(strat.strategy_id)
    assert fetched.strategy_id == strat.strategy_id
    assert fetched.name == "Momentum"


def test_get_missing_raises(table: Any) -> None:
    s = _store(table)
    with pytest.raises(StrategyNotFoundError):
        s.get("nope")


def test_list_for_org_scopes_results(table: Any) -> None:
    """Critical: a scan for org A must not return org B's strategies."""

    s = _store(table)
    s.create(org_id="org_a", author_user_id="alice", name="A1", markdown="")
    s.create(org_id="org_a", author_user_id="alice", name="A2", markdown="")
    s.create(org_id="org_b", author_user_id="bob", name="B1", markdown="")

    a_list = s.list_for_org("org_a")
    b_list = s.list_for_org("org_b")

    assert {x.name for x in a_list} == {"A1", "A2"}
    assert {x.name for x in b_list} == {"B1"}


def test_list_all_returns_everything(table: Any) -> None:
    """list_all is for sysadmin meta-view; no scoping."""

    s = _store(table)
    s.create(org_id="org_a", author_user_id="alice", name="A", markdown="")
    s.create(org_id="org_b", author_user_id="bob", name="B", markdown="")
    assert len(s.list_all()) == 2


def test_update_cannot_change_org_or_author(table: Any) -> None:
    """Org and author are immutable. Attempts to change them are silently
    dropped — the alternative (error) would let callers discover immutable
    fields by probing. Silent drop is the least-leak option."""

    s = _store(table)
    strat = s.create(
        org_id="org_a", author_user_id="alice", name="A", markdown="",
    )
    updated = s.update(strat.strategy_id, {
        "name": "Renamed",
        "org_id": "org_b",           # should be ignored
        "author_user_id": "eve",     # should be ignored
        "created_at": 0,             # should be ignored
    })
    assert updated.name == "Renamed"
    assert updated.org_id == "org_a"
    assert updated.author_user_id == "alice"
    assert updated.created_at == strat.created_at


def test_update_known_fields(table: Any) -> None:
    s = _store(table)
    strat = s.create(
        org_id="org_a", author_user_id="alice", name="A", markdown="",
    )
    updated = s.update(strat.strategy_id, {
        "name": "Renamed",
        "markdown": "# new",
        "status": StrategyStatus.PAUSED.value,
        "capital": "2000",
        "symbols": ["AAPL", "MSFT"],
    })
    assert updated.name == "Renamed"
    assert updated.markdown == "# new"
    assert updated.status == StrategyStatus.PAUSED
    assert updated.capital == "2000"
    assert updated.symbols == ["AAPL", "MSFT"]


def test_delete_removes_strategy(table: Any) -> None:
    s = _store(table)
    strat = s.create(
        org_id="org_a", author_user_id="alice", name="A", markdown="",
    )
    s.delete(strat.strategy_id)
    with pytest.raises(StrategyNotFoundError):
        s.get(strat.strategy_id)


# ── ACL ──────────────────────────────────────────────────────────────


def test_acl_add_and_query(table: Any) -> None:
    s = _store(table)
    strat = s.create(
        org_id="org_a", author_user_id="alice", name="A", markdown="",
    )
    s.add_acl(strat.strategy_id, "bob", granted_by="alice")
    assert s.acl_users(strat.strategy_id) == frozenset({"bob"})


def test_acl_remove(table: Any) -> None:
    s = _store(table)
    strat = s.create(
        org_id="org_a", author_user_id="alice", name="A", markdown="",
    )
    s.add_acl(strat.strategy_id, "bob", granted_by="alice")
    s.remove_acl(strat.strategy_id, "bob")
    assert s.acl_users(strat.strategy_id) == frozenset()


def test_delete_cleans_up_acls(table: Any) -> None:
    s = _store(table)
    strat = s.create(
        org_id="org_a", author_user_id="alice", name="A", markdown="",
    )
    s.add_acl(strat.strategy_id, "bob", granted_by="alice")
    s.add_acl(strat.strategy_id, "carol", granted_by="alice")
    s.delete(strat.strategy_id)
    assert s.acl_users(strat.strategy_id) == frozenset()


# ── Authz integration ────────────────────────────────────────────────


def _principal(uid: str, org: str, role: Role) -> Principal:
    return Principal(uid, f"{uid}@x", {org: role})


def test_resource_for_includes_acl(table: Any) -> None:
    s = _store(table)
    strat = s.create(
        org_id="org_a", author_user_id="alice", name="A", markdown="",
    )
    s.add_acl(strat.strategy_id, "bob", granted_by="alice")
    acl = s.acl_users(strat.strategy_id)
    resource = resource_for(strat, acl)
    assert "bob" in resource.delegated_user_ids


def test_can_perform_author_updates_own(table: Any) -> None:
    s = _store(table)
    strat = s.create(
        org_id="org_a", author_user_id="alice", name="A", markdown="",
    )
    alice = _principal("alice", "org_a", Role.OPERATOR)
    from trading_strands.authz.model import Action

    assert can_perform(alice, Action.UPDATE, strategy=strat)


def test_can_perform_non_author_cannot_update(table: Any) -> None:
    s = _store(table)
    strat = s.create(
        org_id="org_a", author_user_id="alice", name="A", markdown="",
    )
    bob = _principal("bob", "org_a", Role.OPERATOR)
    from trading_strands.authz.model import Action

    assert not can_perform(bob, Action.UPDATE, strategy=strat)


def test_can_perform_acl_delegates_update_rights(table: Any) -> None:
    s = _store(table)
    strat = s.create(
        org_id="org_a", author_user_id="alice", name="A", markdown="",
    )
    s.add_acl(strat.strategy_id, "bob", granted_by="alice")
    bob = _principal("bob", "org_a", Role.OPERATOR)
    from trading_strands.authz.model import Action

    assert can_perform(
        bob, Action.UPDATE, strategy=strat,
        acl=s.acl_users(strat.strategy_id),
    )
