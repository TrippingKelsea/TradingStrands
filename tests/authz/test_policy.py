"""Tests for the authorization policy.

Every case the policy says "yes" to must be explicitly tested here. Every
case it says "no" to needs at least one negative test. The matrix is large
on purpose — this module is the privacy/security gate.
"""

from __future__ import annotations

import pytest

from trading_strands.authz import (
    Action,
    Principal,
    Resource,
    ResourceType,
    Role,
    can,
    require,
)
from trading_strands.authz.policy import Unauthorized

# ── Factories ─────────────────────────────────────────────────────────

ORG_A = "org_a"
ORG_B = "org_b"
USER_ALICE = "user_alice"
USER_BOB = "user_bob"
USER_SYS = "user_sys"


def viewer(user_id: str = USER_ALICE, org: str = ORG_A) -> Principal:
    return Principal(user_id, f"{user_id}@x", {org: Role.VIEWER})


def operator(user_id: str = USER_ALICE, org: str = ORG_A) -> Principal:
    return Principal(user_id, f"{user_id}@x", {org: Role.OPERATOR})


def auditor(user_id: str = USER_ALICE, org: str = ORG_A) -> Principal:
    return Principal(user_id, f"{user_id}@x", {org: Role.AUDITOR})


def orgadmin(user_id: str = USER_ALICE, org: str = ORG_A) -> Principal:
    return Principal(user_id, f"{user_id}@x", {org: Role.ORGADMIN})


def sysadmin(user_id: str = USER_SYS) -> Principal:
    return Principal(user_id, f"{user_id}@x", memberships={}, sysadmin=True)


def strategy(
    org: str = ORG_A, author: str = USER_ALICE,
    acl: frozenset[str] = frozenset(),
) -> Resource:
    return Resource(
        ResourceType.STRATEGY, org_id=org,
        author_user_id=author, delegated_user_ids=acl,
    )


def org_resource(org: str = ORG_A) -> Resource:
    return Resource(ResourceType.ORG, org_id=org)


def user_resource(org: str = ORG_A) -> Resource:
    return Resource(ResourceType.USER, org_id=org)


def alpaca_resource(org: str = ORG_A) -> Resource:
    return Resource(ResourceType.ALPACA_SECRET, org_id=org)


def system_config() -> Resource:
    return Resource(ResourceType.SYSTEM_CONFIG, org_id=None)


def cost_data() -> Resource:
    return Resource(ResourceType.COST_DATA, org_id=None)


def market_data() -> Resource:
    return Resource(ResourceType.MARKET_DATA, org_id=None)


# ── Deny by default ───────────────────────────────────────────────────


def test_no_memberships_denied_except_market_data() -> None:
    """A logged-in user who belongs to no orgs cannot touch any org-scoped
    resource. Market data is the only exception — it's the shared island
    every authenticated user can read."""

    nobody = Principal("nobody", "nobody@x", memberships={})
    for action in Action:
        for rtype in ResourceType:
            if rtype == ResourceType.MARKET_DATA and action in (Action.READ, Action.LIST):
                continue  # market data is intentionally open
            r = Resource(rtype, org_id=ORG_A)
            assert not can(nobody, action, r).allowed, f"{action}/{rtype} should deny"


def test_denial_reason_is_informative() -> None:
    nobody = Principal("nobody", "nobody@x", memberships={})
    perm = can(nobody, Action.READ, strategy())
    assert not perm.allowed
    assert "denied by default" in perm.reason


# ── Strategy read visibility ──────────────────────────────────────────


def test_viewer_can_read_strategies_in_their_org() -> None:
    assert can(viewer(), Action.READ, strategy()).allowed
    assert can(viewer(), Action.LIST, strategy()).allowed


def test_operator_can_read_strategies_in_their_org() -> None:
    assert can(operator(), Action.READ, strategy()).allowed


def test_auditor_can_read_strategies_in_their_org() -> None:
    assert can(auditor(), Action.READ, strategy()).allowed


def test_user_cannot_read_strategy_in_other_org() -> None:
    assert not can(viewer(org=ORG_A), Action.READ, strategy(org=ORG_B)).allowed
    assert not can(operator(org=ORG_A), Action.READ, strategy(org=ORG_B)).allowed
    assert not can(orgadmin(org=ORG_A), Action.READ, strategy(org=ORG_B)).allowed


# ── Strategy authorship ───────────────────────────────────────────────


def test_author_can_update_their_own_strategy() -> None:
    alice = operator(USER_ALICE, ORG_A)
    assert can(alice, Action.UPDATE, strategy(author=USER_ALICE)).allowed
    assert can(alice, Action.DELETE, strategy(author=USER_ALICE)).allowed


def test_operator_cannot_edit_someone_elses_strategy_in_same_org() -> None:
    alice = operator(USER_ALICE, ORG_A)
    bob_strategy = strategy(org=ORG_A, author=USER_BOB)
    assert not can(alice, Action.UPDATE, bob_strategy).allowed
    assert not can(alice, Action.DELETE, bob_strategy).allowed


def test_operator_can_read_but_not_edit_others_strategies() -> None:
    alice = operator(USER_ALICE, ORG_A)
    bob_strategy = strategy(org=ORG_A, author=USER_BOB)
    assert can(alice, Action.READ, bob_strategy).allowed
    assert not can(alice, Action.UPDATE, bob_strategy).allowed


def test_viewer_cannot_create_or_mutate_strategies() -> None:
    v = viewer()
    assert not can(v, Action.CREATE, strategy()).allowed
    assert not can(v, Action.UPDATE, strategy(author=USER_ALICE)).allowed


def test_auditor_cannot_create_or_mutate_strategies() -> None:
    a = auditor()
    assert not can(a, Action.CREATE, strategy()).allowed
    assert not can(a, Action.UPDATE, strategy(author=USER_ALICE)).allowed


def test_operator_can_create_strategies_in_their_org() -> None:
    assert can(operator(), Action.CREATE, strategy()).allowed


def test_operator_cannot_create_strategy_in_other_org() -> None:
    alice = operator(USER_ALICE, ORG_A)
    assert not can(alice, Action.CREATE, strategy(org=ORG_B)).allowed


def test_delegated_user_can_edit_strategy_via_acl() -> None:
    alice = operator(USER_ALICE, ORG_A)
    bob_strategy_with_alice_acl = strategy(
        org=ORG_A, author=USER_BOB, acl=frozenset({USER_ALICE}),
    )
    assert can(alice, Action.UPDATE, bob_strategy_with_alice_acl).allowed


def test_non_delegated_user_still_cannot_edit() -> None:
    carol = operator("user_carol", ORG_A)
    bob_strategy_with_alice_acl = strategy(
        org=ORG_A, author=USER_BOB, acl=frozenset({USER_ALICE}),
    )
    assert not can(carol, Action.UPDATE, bob_strategy_with_alice_acl).allowed


# ── Orgadmin ──────────────────────────────────────────────────────────


def test_orgadmin_can_update_any_strategy_in_their_org() -> None:
    admin_a = orgadmin(USER_ALICE, ORG_A)
    bob_strategy = strategy(org=ORG_A, author=USER_BOB)
    assert can(admin_a, Action.UPDATE, bob_strategy).allowed
    assert can(admin_a, Action.DELETE, bob_strategy).allowed


def test_orgadmin_cannot_touch_other_org() -> None:
    admin_a = orgadmin(USER_ALICE, ORG_A)
    assert not can(admin_a, Action.UPDATE, strategy(org=ORG_B)).allowed
    assert not can(admin_a, Action.UPDATE, user_resource(org=ORG_B)).allowed
    assert not can(admin_a, Action.READ, strategy(org=ORG_B)).allowed


def test_orgadmin_manages_users_in_their_org() -> None:
    admin_a = orgadmin(USER_ALICE, ORG_A)
    assert can(admin_a, Action.CREATE, user_resource(org=ORG_A)).allowed
    assert can(admin_a, Action.UPDATE, user_resource(org=ORG_A)).allowed
    assert can(admin_a, Action.DELETE, user_resource(org=ORG_A)).allowed


def test_operator_cannot_manage_users() -> None:
    op = operator()
    assert not can(op, Action.CREATE, user_resource()).allowed
    assert not can(op, Action.LIST, user_resource()).allowed


def test_orgadmin_manages_alpaca_secrets_in_their_org() -> None:
    admin_a = orgadmin(USER_ALICE, ORG_A)
    assert can(admin_a, Action.UPDATE, alpaca_resource(org=ORG_A)).allowed
    assert can(admin_a, Action.READ, alpaca_resource(org=ORG_A)).allowed


# ── Sysadmin ──────────────────────────────────────────────────────────


def test_sysadmin_reads_system_config() -> None:
    assert can(sysadmin(), Action.READ, system_config()).allowed


def test_sysadmin_writes_system_config() -> None:
    assert can(sysadmin(), Action.UPDATE, system_config()).allowed


def test_sysadmin_reads_cost_and_infra_telemetry() -> None:
    assert can(sysadmin(), Action.READ, cost_data()).allowed
    infra = Resource(ResourceType.INFRA_TELEMETRY, org_id=None)
    assert can(sysadmin(), Action.READ, infra).allowed


def test_sysadmin_blocked_from_org_data_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The critical privacy invariant: by default, sysadmin cannot read
    strategies/positions/pnl in customer orgs."""

    monkeypatch.delenv("SYSADMIN_CAN_READ_ORG_DATA", raising=False)
    assert not can(sysadmin(), Action.READ, strategy(org=ORG_A)).allowed
    assert not can(sysadmin(), Action.LIST, strategy(org=ORG_A)).allowed


def test_sysadmin_can_read_org_data_when_deploy_flag_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYSADMIN_CAN_READ_ORG_DATA", "true")
    assert can(sysadmin(), Action.READ, strategy(org=ORG_A)).allowed


def test_sysadmin_cannot_read_alpaca_secrets_even_with_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with the override flag, customer API keys remain off-limits
    to sysadmin. This is a hard rule — ops shouldn't see customer creds."""

    monkeypatch.setenv("SYSADMIN_CAN_READ_ORG_DATA", "true")
    assert not can(sysadmin(), Action.READ, alpaca_resource(org=ORG_A)).allowed
    assert not can(sysadmin(), Action.UPDATE, alpaca_resource(org=ORG_A)).allowed


def test_sysadmin_manages_users() -> None:
    assert can(sysadmin(), Action.CREATE, user_resource()).allowed
    assert can(sysadmin(), Action.DELETE, user_resource()).allowed


def test_sysadmin_cannot_execute_trades_for_a_customer_org() -> None:
    """Sysadmin cannot create strategies in customer orgs. Mutation of
    customer operational state is not something ops should do."""

    assert not can(sysadmin(), Action.CREATE, strategy(org=ORG_A)).allowed
    assert not can(sysadmin(), Action.UPDATE, strategy(org=ORG_A, author=USER_BOB)).allowed


def test_only_sysadmin_creates_orgs() -> None:
    assert can(sysadmin(), Action.CREATE, org_resource()).allowed
    assert not can(orgadmin(), Action.CREATE, org_resource()).allowed
    assert not can(operator(), Action.CREATE, org_resource()).allowed


# ── Org metadata ──────────────────────────────────────────────────────


def test_any_org_member_reads_org_metadata() -> None:
    assert can(viewer(), Action.READ, org_resource()).allowed
    assert can(operator(), Action.READ, org_resource()).allowed
    assert can(auditor(), Action.READ, org_resource()).allowed


def test_non_member_cannot_read_org_metadata() -> None:
    outsider = viewer(USER_ALICE, ORG_A)
    assert not can(outsider, Action.READ, org_resource(org=ORG_B)).allowed


def test_orgadmin_updates_own_org() -> None:
    admin_a = orgadmin(USER_ALICE, ORG_A)
    assert can(admin_a, Action.UPDATE, org_resource(org=ORG_A)).allowed
    assert can(admin_a, Action.DELETE, org_resource(org=ORG_A)).allowed


def test_non_admin_cannot_mutate_org() -> None:
    assert not can(viewer(), Action.UPDATE, org_resource()).allowed
    assert not can(operator(), Action.UPDATE, org_resource()).allowed
    assert not can(auditor(), Action.DELETE, org_resource()).allowed


# ── Many-to-many membership ──────────────────────────────────────────


def test_user_with_membership_in_multiple_orgs_enforces_per_org_role() -> None:
    """Alice is operator in A and orgadmin in B. Her capabilities reflect
    that: she can mutate Bob's strategies in B (orgadmin) but not in A."""

    alice = Principal(
        USER_ALICE, "alice@x",
        memberships={ORG_A: Role.OPERATOR, ORG_B: Role.ORGADMIN},
    )
    bob_strategy_in_a = strategy(org=ORG_A, author=USER_BOB)
    bob_strategy_in_b = strategy(org=ORG_B, author=USER_BOB)

    assert not can(alice, Action.UPDATE, bob_strategy_in_a).allowed
    assert can(alice, Action.UPDATE, bob_strategy_in_b).allowed


# ── Market data ──────────────────────────────────────────────────────


def test_any_authenticated_user_reads_market_data() -> None:
    assert can(viewer(), Action.READ, market_data()).allowed
    assert can(operator(), Action.LIST, market_data()).allowed
    # Even a user with no memberships — they're authenticated, that's enough
    authd = Principal("u", "u@x", memberships={})
    assert can(authd, Action.READ, market_data()).allowed


def test_market_data_is_not_mutable_by_regular_users() -> None:
    assert not can(orgadmin(), Action.UPDATE, market_data()).allowed
    assert not can(operator(), Action.DELETE, market_data()).allowed


# ── require() raises ──────────────────────────────────────────────────


def test_require_raises_unauthorized_on_denial() -> None:
    with pytest.raises(Unauthorized) as exc:
        require(viewer(), Action.CREATE, strategy())
    assert exc.value.reason == "no rule matched — denied by default"


def test_require_silent_on_allow() -> None:
    require(operator(), Action.CREATE, strategy())  # should not raise
