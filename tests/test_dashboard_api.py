"""Tests for the dashboard FastAPI service.

These tests use a moto-backed real DynamoDB table so the new code paths
(TenancyStore -> Principal -> authz) exercise as close to production as
possible. A couple of endpoints still hit Cognito, which is mocked with
unittest.mock where needed.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

# Set auth env vars before importing the app
os.environ.setdefault("COGNITO_USER_POOL_ID", "us-west-2_test")
os.environ.setdefault("COGNITO_CLIENT_ID", "testclient")
os.environ.setdefault("COGNITO_CLIENT_SECRET", "testsecret")
os.environ.setdefault("DYNAMODB_TABLE", "trading-strands-state")
os.environ.setdefault("SESSION_SECRET", "test-secret")
# moto fixtures need a default region so boto3.resource('dynamodb') resolves.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture(autouse=True)
def _reset_serializer() -> Iterator[None]:
    """Clear the lazy serializer so SESSION_SECRET is picked up per test."""

    import trading_strands.dashboard.auth as auth_mod
    auth_mod._serializer = None
    yield
    auth_mod._serializer = None


def _make_table() -> Any:
    """Create the production-shape DDB table in moto."""

    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="trading-strands-state",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("trading-strands-state")


def _make_user(
    table: Any, email: str = "test@example.com",
    role_name: str = "operator",
    sysadmin: bool = False,
) -> tuple[str, str]:
    """Create a user + org + membership. Returns (user_id, org_id)."""

    from trading_strands.authz.model import Role
    from trading_strands.tenancy.store import TenancyStore

    store = TenancyStore(table)
    user = store.create_user(email=email)
    org = store.create_org("Test Org")
    store.add_membership(user.user_id, org.org_id, Role(role_name))
    if sysadmin:
        store.grant_sysadmin(user.user_id)
    return user.user_id, org.org_id


def _session_cookie(
    user_id: str, active_org_id: str | None = None,
) -> dict[str, str]:
    """Build a v2 session cookie for the given user + active org."""

    from trading_strands.dashboard.auth import create_session_cookie

    return {
        "session": create_session_cookie({
            "user_id": user_id,
            "email": "test@example.com",
            "active_org_id": active_org_id,
            "access_token": "fake",
            "login_at": 1700000000,
        })
    }


# ── Public endpoints ──────────────────────────────────────────────────


def test_health() -> None:
    with mock_aws():
        _make_table()
        from trading_strands.dashboard.api import app

        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


# ── Snapshot / events / telemetry (authentication only) ──────────────


def test_snapshot() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)
        table.put_item(Item={
            "pk": "SNAPSHOT",
            "tick": 5,
            "timestamp": 1700000000,
            "prices": {"AAPL": "155.50"},
            "ledgers": {},
            "risk": {"desk_halted": False, "halted_bots": []},
        })

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/snapshot")
        assert resp.status_code == 200
        data = resp.json()
        assert int(data["tick"]) == 5
        assert data["prices"]["AAPL"] == "155.50"


def test_snapshot_empty() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)
        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/snapshot")
        assert resp.status_code == 200
        assert resp.json()["tick"] == 0


def test_snapshot_requires_auth() -> None:
    with mock_aws():
        _make_table()
        from trading_strands.dashboard.api import app

        client = TestClient(app)
        resp = client.get("/api/snapshot")
        assert resp.status_code == 401


def test_events() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)
        table.put_item(Item={
            "pk": "EVENT#123",
            "event_type": "trade.executed",
            "timestamp": 1700000000,
            "data": {"symbol": "AAPL"},
        })

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/events")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["event_type"] == "trade.executed"


def test_telemetry() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/telemetry")
        assert resp.status_code == 200
        data = resp.json()
        assert "dynamodb" in data
        assert "trading_service" in data


def test_index_serves_html() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/")
        assert resp.status_code == 200
        assert "TradingStrands" in resp.text


# ── Strategy CRUD ─────────────────────────────────────────────────────


def test_list_strategies_scopes_by_org() -> None:
    """Cross-org privacy: strategies in other orgs must NOT appear."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.strategies_store.store import StrategyStore
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)

        alice = tenancy.create_user(email="alice@x.com")
        org_a = tenancy.create_org("A")
        org_b = tenancy.create_org("B")
        tenancy.add_membership(alice.user_id, org_a.org_id, Role.OPERATOR)

        store = StrategyStore(table)
        store.create(org_a.org_id, alice.user_id, "Alpha", "# A")
        store.create(org_b.org_id, "bob", "Bravo", "# B")  # Alice is NOT in org_b

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(alice.user_id, org_a.org_id))
        resp = client.get("/api/strategies")
        assert resp.status_code == 200
        names = {s["name"] for s in resp.json()}
        assert names == {"Alpha"}  # Bravo must not appear


def test_create_strategy() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post("/api/strategies", json={
            "name": "Momentum",
            "markdown": "# rules",
            "symbols": ["AAPL"],
            "capital": "5000",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Momentum"
        assert data["org_id"] == oid
        assert data["author_user_id"] == uid
        assert data["status"] == "active"


def test_viewer_cannot_create_strategy() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="viewer")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post("/api/strategies", json={
            "name": "Banned", "markdown": "# nope",
        })
        assert resp.status_code == 403


def test_get_strategy_requires_membership() -> None:
    """An operator in org A cannot read org B's strategy."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.strategies_store.store import StrategyStore
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org_a = tenancy.create_org("A")
        org_b = tenancy.create_org("B")
        tenancy.add_membership(alice.user_id, org_a.org_id, Role.OPERATOR)

        store = StrategyStore(table)
        foreign = store.create(org_b.org_id, "bob", "Alpha", "# B")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(alice.user_id, org_a.org_id))
        resp = client.get(f"/api/strategies/{foreign.strategy_id}")
        assert resp.status_code == 403


def test_get_strategy_not_found() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/strategies/nonexistent")
        assert resp.status_code == 404


def test_update_strategy_only_by_author() -> None:
    """Operator-in-same-org but not author cannot edit."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.strategies_store.store import StrategyStore
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        bob = tenancy.create_user(email="bob@x.com")
        org = tenancy.create_org("shared")
        tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)
        tenancy.add_membership(bob.user_id, org.org_id, Role.OPERATOR)

        store = StrategyStore(table)
        strat = store.create(org.org_id, alice.user_id, "A", "# A")

        from trading_strands.dashboard.api import app

        # Bob tries to edit alice's strategy — should 403.
        client = TestClient(app, cookies=_session_cookie(bob.user_id, org.org_id))
        resp = client.put(
            f"/api/strategies/{strat.strategy_id}",
            json={"name": "Bob Was Here"},
        )
        assert resp.status_code == 403

        # Alice edits her own — should 200.
        client2 = TestClient(
            app, cookies=_session_cookie(alice.user_id, org.org_id),
        )
        resp2 = client2.put(
            f"/api/strategies/{strat.strategy_id}",
            json={"name": "Renamed"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["name"] == "Renamed"


def test_update_strategy_status() -> None:
    with mock_aws():
        from trading_strands.strategies_store.store import StrategyStore

        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")
        strat = StrategyStore(table).create(oid, uid, "S", "# s")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.put(
            f"/api/strategies/{strat.strategy_id}/status",
            json={"status": "paused"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "paused"


def test_update_strategy_status_invalid() -> None:
    with mock_aws():
        from trading_strands.strategies_store.store import StrategyStore

        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")
        strat = StrategyStore(table).create(oid, uid, "S", "# s")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.put(
            f"/api/strategies/{strat.strategy_id}/status",
            json={"status": "invalid"},
        )
        assert resp.status_code == 400


def test_delete_strategy_by_author() -> None:
    with mock_aws():
        from trading_strands.strategies_store.store import StrategyStore

        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")
        store = StrategyStore(table)
        strat = store.create(oid, uid, "S", "# s")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.delete(f"/api/strategies/{strat.strategy_id}")
        assert resp.status_code == 204


# ── Halt ─────────────────────────────────────────────────────────────


def test_halt_requires_orgadmin() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post("/api/halt")
        assert resp.status_code == 403


def test_halt_by_orgadmin() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post("/api/halt")
        assert resp.status_code == 200
        assert resp.json()["status"] == "halted"


def test_unhalt_by_orgadmin() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post("/api/unhalt")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"


# ── Cost (sysadmin only) ─────────────────────────────────────────────


def test_cost_requires_sysadmin() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/costs")
        assert resp.status_code == 403


def test_cost_allowed_for_sysadmin() -> None:
    with mock_aws():
        table = _make_table()
        uid, _oid = _make_user(table, role_name="viewer", sysadmin=True)

        from trading_strands.dashboard.api import app

        # Sysadmin — cost is allowed (may error at CE call but that's the
        # expected "try/except and return empty" path).
        client = TestClient(app, cookies=_session_cookie(uid))
        resp = client.get("/api/costs")
        # Either 200 with empty data (if boto CE fails gracefully) or real data.
        assert resp.status_code == 200


# ── Admin — orgs (authorized via policy) ─────────────────────────────


def test_list_orgs_for_orgadmin_sees_only_own() -> None:
    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org_a = tenancy.create_org("A")
        org_b = tenancy.create_org("B")
        tenancy.create_org("C")
        tenancy.add_membership(alice.user_id, org_a.org_id, Role.ORGADMIN)
        tenancy.add_membership(alice.user_id, org_b.org_id, Role.OPERATOR)

        from trading_strands.dashboard.api import app

        client = TestClient(
            app, cookies=_session_cookie(alice.user_id, org_a.org_id),
        )
        resp = client.get("/api/admin/orgs")
        assert resp.status_code == 200
        names = {o["name"] for o in resp.json()}
        assert names == {"A"}  # only the one she's orgadmin of


def test_list_orgs_for_sysadmin_sees_all() -> None:
    with mock_aws():
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        tenancy.create_org("A")
        tenancy.create_org("B")
        tenancy.create_org("C")
        tenancy.grant_sysadmin(alice.user_id)

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(alice.user_id))
        resp = client.get("/api/admin/orgs")
        assert resp.status_code == 200
        assert len(resp.json()) == 3


def test_list_orgs_forbidden_for_plain_user() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/admin/orgs")
        assert resp.status_code == 403


def test_create_org_requires_sysadmin() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post("/api/admin/orgs", json={"name": "new org"})
        assert resp.status_code == 403


def test_create_org_allowed_for_sysadmin() -> None:
    with mock_aws():
        table = _make_table()
        uid, _oid = _make_user(table, role_name="viewer", sysadmin=True)

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid))
        resp = client.post("/api/admin/orgs", json={"name": "new org"})
        assert resp.status_code == 201
        assert resp.json()["name"] == "new org"


# ── Admin — users (Cognito-backed) ────────────────────────────────────


def test_list_users_requires_orgadmin_or_sysadmin() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/admin/users")
        assert resp.status_code == 403


@patch("trading_strands.dashboard.api._get_cognito_client")
def test_list_users_for_orgadmin(mock_cognito_fn: MagicMock) -> None:
    mock_cognito = MagicMock()
    mock_cognito_fn.return_value = mock_cognito
    mock_cognito.list_users.return_value = {
        "Users": [
            {
                "Username": "abc",
                "Attributes": [
                    {"Name": "email", "Value": "admin@x.com"},
                    {"Name": "custom:role", "Value": "operator"},
                ],
                "UserStatus": "CONFIRMED",
                "Enabled": True,
                "UserCreateDate": "2026-01-01T00:00:00Z",
            },
        ],
    }

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/admin/users")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


@patch("trading_strands.dashboard.api._get_cognito_client")
def test_create_user_invalid_role(mock_cognito_fn: MagicMock) -> None:
    mock_cognito = MagicMock()
    mock_cognito_fn.return_value = mock_cognito

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post("/api/admin/users", json={
            "email": "new@x.com",
            "role": "superadmin",
            "org_id": oid,
        })
        assert resp.status_code == 400


@patch("trading_strands.dashboard.api._get_cognito_client")
def test_create_user_allowed_by_orgadmin(mock_cognito_fn: MagicMock) -> None:
    mock_cognito = MagicMock()
    mock_cognito_fn.return_value = mock_cognito

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post("/api/admin/users", json={
            "email": "new@x.com",
            "role": "viewer",
            "org_id": oid,
        })
        assert resp.status_code == 201
        assert resp.json()["email"] == "new@x.com"


# ── Signed URL tokens ─────────────────────────────────────────────────


def test_url_token_roundtrip() -> None:
    from trading_strands.dashboard.auth import create_url_token, decode_url_token

    data = {"error": "test message", "code": 42}
    token = create_url_token(data)
    assert "test message" not in token
    decoded = decode_url_token(token)
    assert decoded is not None
    assert decoded["error"] == "test message"


def test_url_token_tampered() -> None:
    from trading_strands.dashboard.auth import create_url_token, decode_url_token

    token = create_url_token({"error": "test"})
    tampered = token[:-4] + "XXXX"
    assert decode_url_token(tampered) is None


# ── v1 session invalidation ──────────────────────────────────────────


def test_v1_session_without_user_id_is_rejected() -> None:
    """Pre-refactor sessions (no user_id) must be rejected — users re-login."""

    import trading_strands.dashboard.auth as auth_mod
    from trading_strands.dashboard.auth import _get_serializer

    # Craft a v1-shaped session cookie directly.
    ser = _get_serializer()
    v1 = ser.dumps(
        {"email": "old@x.com", "role": "operator"},
        salt="session",
    )

    with mock_aws():
        _make_table()
        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies={"session": v1})
        resp = client.get("/api/snapshot")
        assert resp.status_code == 401
    # Touch the module to keep the import in scope.
    _ = auth_mod
