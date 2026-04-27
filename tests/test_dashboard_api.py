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


# ── Session / org switcher ───────────────────────────────────────────


def test_session_returns_memberships_and_active_org() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/session")
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == uid
        assert data["active_org_id"] == oid
        assert data["sysadmin"] is False
        assert len(data["memberships"]) == 1
        assert data["memberships"][0]["role"] == "operator"
        assert data["memberships"][0]["org_name"] == "Test Org"


def test_session_auto_selects_single_membership_when_no_claim() -> None:
    """Session with no active_org_id but a single membership auto-resolves."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="viewer")

        from trading_strands.dashboard.api import app

        # Cookie without active_org_id.
        client = TestClient(app, cookies=_session_cookie(uid, active_org_id=None))
        resp = client.get("/api/session")
        assert resp.status_code == 200
        assert resp.json()["active_org_id"] == oid


def test_session_null_active_org_when_multiple_orgs_and_no_claim() -> None:
    """Multi-org user with no session claim lands without an active org;
    frontend is expected to render the org picker."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        a = tenancy.create_org("A")
        b = tenancy.create_org("B")
        tenancy.add_membership(alice.user_id, a.org_id, Role.OPERATOR)
        tenancy.add_membership(alice.user_id, b.org_id, Role.VIEWER)

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(alice.user_id, active_org_id=None))
        resp = client.get("/api/session")
        assert resp.status_code == 200
        assert resp.json()["active_org_id"] is None
        assert len(resp.json()["memberships"]) == 2


def test_session_ignores_stale_active_org_claim() -> None:
    """If a session claims membership in an org the user is no longer in,
    the response reports active_org_id=None (or auto-selects a valid org).
    Privacy guard: we never honor a claimed org without verifying."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        real = tenancy.create_org("Real")
        tenancy.create_org("Phantom")  # user NOT a member
        tenancy.add_membership(alice.user_id, real.org_id, Role.VIEWER)

        from trading_strands.dashboard.api import app

        # Session claims "phantom" org the user isn't in.
        client = TestClient(
            app, cookies=_session_cookie(alice.user_id, active_org_id="phantom-fake"),
        )
        resp = client.get("/api/session")
        assert resp.status_code == 200
        # Falls back to the real single membership rather than honoring the claim.
        assert resp.json()["active_org_id"] == real.org_id


def test_set_active_org_persists_and_updates_cookie() -> None:
    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        a = tenancy.create_org("A")
        b = tenancy.create_org("B")
        tenancy.add_membership(alice.user_id, a.org_id, Role.OPERATOR)
        tenancy.add_membership(alice.user_id, b.org_id, Role.VIEWER)

        from trading_strands.dashboard.api import app

        client = TestClient(
            app, cookies=_session_cookie(alice.user_id, active_org_id=a.org_id),
        )
        resp = client.put(
            "/api/session/active-org",
            json={"org_id": b.org_id},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        # The response sets a new session cookie.
        assert "session" in resp.cookies or "session" in resp.headers.get(
            "set-cookie", "",
        )
        # last_active_org_id persisted on the user.
        refreshed = tenancy.get_user(alice.user_id)
        assert refreshed.last_active_org_id == b.org_id


def test_set_active_org_rejects_non_member_org() -> None:
    """Final guard: even if the frontend sends a non-member org_id, server
    refuses. Tampered cookies / frontends cannot leak cross-org access."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, active_org_id=oid))
        resp = client.put(
            "/api/session/active-org",
            json={"org_id": "not-my-org"},
            follow_redirects=False,
        )
        assert resp.status_code == 403


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


def test_create_strategy_with_model_id() -> None:
    """Allowlisted model_id round-trips through the store."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post("/api/strategies", json={
            "name": "Opus test",
            "markdown": "# rules",
            "symbols": ["AAPL"],
            "capital": "1000",
            "model_id": "us.anthropic.claude-opus-4-7",
        })
        assert resp.status_code == 201, resp.text
        assert resp.json()["model_id"] == "us.anthropic.claude-opus-4-7"


def test_create_strategy_rejects_unknown_model_id() -> None:
    """Typo in model_id must 400 at save time, not silently accept."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post("/api/strategies", json={
            "name": "Broken",
            "markdown": "# rules",
            "model_id": "us.anthropic.claude-wrong",
        })
        assert resp.status_code == 400
        assert "unknown model_id" in resp.json()["detail"]


def test_update_strategy_rejects_unknown_model_id() -> None:
    """PUT with a bad model_id surfaces as 400 from the store validator."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")

        from trading_strands.strategies_store.store import StrategyStore

        store = StrategyStore(table)
        strat = store.create(
            org_id=oid, author_user_id=uid, name="s", markdown="# m",
        )

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.put(
            f"/api/strategies/{strat.strategy_id}",
            json={"model_id": "nope-not-real"},
        )
        assert resp.status_code == 400


def test_list_models_returns_allowlist() -> None:
    """GET /api/models returns the registry entries for the UI dropdown."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="viewer")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/models")
        assert resp.status_code == 200
        models = resp.json()
        assert len(models) > 0
        ids = [m["id"] for m in models]
        assert "us.anthropic.claude-sonnet-4-6" in ids
        # Exactly one default is flagged.
        defaults = [m for m in models if m["is_default"] == "true"]
        assert len(defaults) == 1


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
def test_list_users_for_orgadmin_shows_real_memberships(
    mock_cognito_fn: MagicMock,
) -> None:
    """List-users reads from DynamoDB now, so memberships + sysadmin flag
    reflect real state (not the old Cognito custom:role attr)."""

    mock_cognito = MagicMock()
    mock_cognito_fn.return_value = mock_cognito
    mock_cognito.admin_get_user.return_value = {
        "UserStatus": "CONFIRMED", "Enabled": True,
    }

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/admin/users")
        assert resp.status_code == 200
        users = resp.json()
        assert len(users) == 1
        assert users[0]["email"] == "test@example.com"
        assert users[0]["memberships"] == [
            {"org_id": oid, "role": "orgadmin"},
        ]
        assert users[0]["sysadmin"] is False


@patch("trading_strands.dashboard.api._get_cognito_client")
def test_list_users_for_sysadmin_shows_sysadmin_flag(
    mock_cognito_fn: MagicMock,
) -> None:
    mock_cognito = MagicMock()
    mock_cognito_fn.return_value = mock_cognito
    mock_cognito.admin_get_user.return_value = {
        "UserStatus": "CONFIRMED", "Enabled": True,
    }

    with mock_aws():
        table = _make_table()
        uid, _oid = _make_user(table, role_name="viewer", sysadmin=True)

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid))
        resp = client.get("/api/admin/users")
        assert resp.status_code == 200
        users = resp.json()
        assert len(users) == 1
        assert users[0]["sysadmin"] is True


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


@patch("trading_strands.dashboard.api._get_cognito_client")
def test_create_user_accepts_all_four_per_org_roles(
    mock_cognito_fn: MagicMock,
) -> None:
    """Every role in VALID_ROLES must be settable via user-create."""

    mock_cognito = MagicMock()
    mock_cognito_fn.return_value = mock_cognito

    for role in ("viewer", "operator", "auditor", "orgadmin"):
        with mock_aws():
            table = _make_table()
            uid, oid = _make_user(table, role_name="orgadmin")
            from trading_strands.dashboard.api import app

            client = TestClient(app, cookies=_session_cookie(uid, oid))
            resp = client.post("/api/admin/users", json={
                "email": f"{role}@x.com", "role": role, "org_id": oid,
            })
            assert resp.status_code == 201, f"role {role} should be accepted"
            assert resp.json()["role"] == role


@patch("trading_strands.dashboard.api._get_cognito_client")
def test_create_user_rejects_sysadmin_as_role(
    mock_cognito_fn: MagicMock,
) -> None:
    """Sysadmin is a global flag, not a role. Setting it here must 400."""

    mock_cognito = MagicMock()
    mock_cognito_fn.return_value = mock_cognito

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")
        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post("/api/admin/users", json={
            "email": "god@x.com", "role": "sysadmin", "org_id": oid,
        })
        assert resp.status_code == 400


@patch("trading_strands.dashboard.api._get_cognito_client")
def test_update_role_requires_org_id_and_writes_membership(
    mock_cognito_fn: MagicMock,
) -> None:
    """New role-update takes user_id + (body.org_id, body.role) and writes
    a membership via the tenancy store. The old Cognito-attribute path is
    gone."""

    mock_cognito_fn.return_value = MagicMock()

    with mock_aws():
        from trading_strands.authz.model import Role as AuthzRole
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        admin_uid, oid = _make_user(table, role_name="orgadmin")
        bob = TenancyStore(table).create_user(email="bob@x.com")

        from trading_strands.dashboard.api import app

        client = TestClient(
            app, cookies=_session_cookie(admin_uid, oid),
        )
        resp = client.put(
            f"/api/admin/users/{bob.user_id}/role",
            json={"role": "auditor", "org_id": oid},
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "auditor"
        role = TenancyStore(table).role_of(bob.user_id, oid)
        assert role == AuthzRole.AUDITOR


@patch("trading_strands.dashboard.api._get_cognito_client")
def test_sysadmin_endpoint_grants(mock_cognito_fn: MagicMock) -> None:
    mock_cognito_fn.return_value = MagicMock()

    with mock_aws():
        from trading_strands.authz.model import Role as AuthzRole
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)

        # Caller: a sysadmin who's a member of the system org.
        from trading_strands.tenancy.models import OrgType as _OrgType
        system_org = tenancy.create_org(
            "Women with Super Powers", _OrgType.SYSTEM,
        )
        caller = tenancy.create_user(email="caller@x.com")
        tenancy.add_membership(caller.user_id, system_org.org_id, AuthzRole.ORGADMIN)
        tenancy.grant_sysadmin(caller.user_id)

        # Target: a user who's already a member of the system org (so
        # they're eligible for sysadmin).
        target = tenancy.create_user(email="target@x.com")
        tenancy.add_membership(
            target.user_id, system_org.org_id, AuthzRole.VIEWER,
        )

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(caller.user_id))
        resp = client.put(
            f"/api/admin/users/{target.user_id}/sysadmin",
            json={"sysadmin": True},
        )
        assert resp.status_code == 200
        assert resp.json()["sysadmin"] is True
        assert tenancy.is_sysadmin(target.user_id) is True


@patch("trading_strands.dashboard.api._get_cognito_client")
def test_sysadmin_endpoint_refuses_non_system_org_member(
    mock_cognito_fn: MagicMock,
) -> None:
    """Cannot grant sysadmin to a user who's not in the system org."""

    mock_cognito_fn.return_value = MagicMock()

    with mock_aws():
        from trading_strands.authz.model import Role as AuthzRole
        from trading_strands.tenancy.models import OrgType as _OrgType
        from trading_strands.tenancy.store import TenancyStore
        table = _make_table()
        tenancy = TenancyStore(table)
        tenancy.create_org("Women with Super Powers", _OrgType.SYSTEM)
        customer_org = tenancy.create_org("Customer")

        caller = tenancy.create_user(email="caller@x.com")
        tenancy.grant_sysadmin(caller.user_id)

        target = tenancy.create_user(email="target@x.com")
        # Only in the customer org, not the system org.
        tenancy.add_membership(
            target.user_id, customer_org.org_id, AuthzRole.OPERATOR,
        )

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(caller.user_id))
        resp = client.put(
            f"/api/admin/users/{target.user_id}/sysadmin",
            json={"sysadmin": True},
        )
        assert resp.status_code == 400


@patch("trading_strands.dashboard.api._get_cognito_client")
def test_sysadmin_endpoint_cannot_revoke_last_sysadmin(
    mock_cognito_fn: MagicMock,
) -> None:
    mock_cognito_fn.return_value = MagicMock()

    with mock_aws():
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        uid, _oid = _make_user(
            table, role_name="orgadmin", sysadmin=True,
        )
        tenancy = TenancyStore(table)
        assert tenancy.is_sysadmin(uid) is True
        assert sum(1 for u in tenancy.list_users() if tenancy.is_sysadmin(u.user_id)) == 1

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid))
        resp = client.put(
            f"/api/admin/users/{uid}/sysadmin",
            json={"sysadmin": False},
        )
        assert resp.status_code == 400
        # Still sysadmin — the revocation was refused.
        assert tenancy.is_sysadmin(uid) is True


@patch("trading_strands.dashboard.api._get_cognito_client")
def test_sysadmin_endpoint_forbidden_for_non_sysadmin(
    mock_cognito_fn: MagicMock,
) -> None:
    mock_cognito_fn.return_value = MagicMock()

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")
        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.put(
            f"/api/admin/users/{uid}/sysadmin",
            json={"sysadmin": True},
        )
        assert resp.status_code == 403


# ── Per-org Alpaca credentials ───────────────────────────────────────


def test_alpaca_status_requires_orgadmin() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get(f"/api/admin/orgs/{oid}/alpaca")
        assert resp.status_code == 403


def test_alpaca_status_reports_not_configured_for_orgadmin() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get(f"/api/admin/orgs/{oid}/alpaca")
        assert resp.status_code == 200
        assert resp.json()["configured"] is False


def test_alpaca_upsert_and_status_roundtrip() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        put = client.put(
            f"/api/admin/orgs/{oid}/alpaca",
            json={"api_key": "K", "secret_key": "S", "paper": True},
        )
        assert put.status_code == 200
        assert put.json()["configured"] is True

        status = client.get(f"/api/admin/orgs/{oid}/alpaca")
        assert status.status_code == 200
        assert status.json()["configured"] is True
        assert status.json()["paper"] is True


def test_alpaca_creds_never_returned_by_any_endpoint() -> None:
    """Hard invariant: the raw API key must never surface to the dashboard."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid, oid))
        client.put(
            f"/api/admin/orgs/{oid}/alpaca",
            json={"api_key": "supersecret", "secret_key": "alsosecret", "paper": True},
        )
        status = client.get(f"/api/admin/orgs/{oid}/alpaca")
        body = status.json()
        assert "supersecret" not in str(body)
        assert "alsosecret" not in str(body)
        assert "api_key" not in body
        assert "secret_key" not in body


def test_alpaca_creds_foreign_org_forbidden() -> None:
    """Orgadmin in org A cannot write creds for org B."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org_a = tenancy.create_org("A")
        org_b = tenancy.create_org("B")
        tenancy.add_membership(alice.user_id, org_a.org_id, Role.ORGADMIN)

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(alice.user_id, org_a.org_id))
        resp = client.put(
            f"/api/admin/orgs/{org_b.org_id}/alpaca",
            json={"api_key": "X", "secret_key": "Y", "paper": True},
        )
        assert resp.status_code == 403


def test_alpaca_sysadmin_cannot_read_or_write() -> None:
    """Hard invariant: sysadmin is blocked from Alpaca secrets."""

    with mock_aws():
        table = _make_table()
        uid, _oid = _make_user(table, role_name="viewer", sysadmin=True)

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(uid))
        # Create a different org the sysadmin isn't a member of.
        from trading_strands.tenancy.store import TenancyStore
        customer_org = TenancyStore(table).create_org("Customer")

        resp = client.get(f"/api/admin/orgs/{customer_org.org_id}/alpaca")
        assert resp.status_code == 403

        put = client.put(
            f"/api/admin/orgs/{customer_org.org_id}/alpaca",
            json={"api_key": "X", "secret_key": "Y", "paper": True},
        )
        assert put.status_code == 403


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


# ── Self-critique lessons endpoint ──────────────────────────────────


def _put_lesson_in_s3(
    bucket: str, org_id: str, bot_id: str, content: str,
) -> None:
    """Seed a lessons.md under the canonical per-agent prefix.

    Mirrors AgentMemoryStore's path convention so the dashboard reads
    what the bot would have written in real life.
    """

    s3 = boto3.client("s3", region_name="us-west-2")
    s3.create_bucket(
        Bucket=bucket,
        CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
    )
    s3.put_object(
        Bucket=bucket,
        Key=f"{org_id}/strategy/{bot_id}/lessons.md",
        Body=content.encode("utf-8"),
    )


def test_critique_lessons_returns_file_for_author() -> None:
    """Author reads their own strategy's lessons — happy path."""

    os.environ["AGENT_MEMORY_BUCKET"] = "test-agent-memory"
    try:
        with mock_aws():
            from trading_strands.authz.model import Role
            from trading_strands.strategies_store.store import StrategyStore
            from trading_strands.tenancy.store import TenancyStore

            table = _make_table()
            tenancy = TenancyStore(table)
            alice = tenancy.create_user(email="alice@x.com")
            org = tenancy.create_org("org-a")
            tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)

            store = StrategyStore(table)
            strat = store.create(org.org_id, alice.user_id, "S", "# rules")
            bot_id = f"strategy-{strat.strategy_id}"

            _put_lesson_in_s3(
                "test-agent-memory", org.org_id, bot_id,
                "## 2026-04-26 — weekend self-critique\n\nFollowed rules.",
            )

            from trading_strands.dashboard.api import app

            client = TestClient(app, cookies=_session_cookie(
                alice.user_id, org.org_id,
            ))
            resp = client.get(f"/api/strategies/{strat.strategy_id}/lessons")
            assert resp.status_code == 200
            body = resp.json()
            assert "Followed rules" in body["lessons"]
            assert body["strategy_id"] == strat.strategy_id
    finally:
        del os.environ["AGENT_MEMORY_BUCKET"]


def test_critique_lessons_forbidden_for_other_org() -> None:
    """Lessons contain strategy reasoning — privacy boundary is the same
    as the strategy row itself. Cross-org reads must 403."""

    os.environ["AGENT_MEMORY_BUCKET"] = "test-agent-memory"
    try:
        with mock_aws():
            from trading_strands.authz.model import Role
            from trading_strands.strategies_store.store import StrategyStore
            from trading_strands.tenancy.store import TenancyStore

            table = _make_table()
            tenancy = TenancyStore(table)
            alice = tenancy.create_user(email="alice@x.com")
            bob = tenancy.create_user(email="bob@x.com")
            org_a = tenancy.create_org("A")
            org_b = tenancy.create_org("B")
            tenancy.add_membership(alice.user_id, org_a.org_id, Role.OPERATOR)
            tenancy.add_membership(bob.user_id, org_b.org_id, Role.OPERATOR)

            store = StrategyStore(table)
            strat = store.create(org_b.org_id, bob.user_id, "S", "# rules")

            from trading_strands.dashboard.api import app

            client = TestClient(app, cookies=_session_cookie(
                alice.user_id, org_a.org_id,
            ))
            resp = client.get(f"/api/strategies/{strat.strategy_id}/lessons")
            assert resp.status_code == 403
    finally:
        del os.environ["AGENT_MEMORY_BUCKET"]


def test_critique_lessons_404_when_missing() -> None:
    """No lessons.md yet — return 200 with empty body rather than 404.
    A new strategy with zero self-critiques is the default state; the
    dashboard wants to render an empty panel, not an error."""

    os.environ["AGENT_MEMORY_BUCKET"] = "test-agent-memory"
    try:
        with mock_aws():
            from trading_strands.authz.model import Role
            from trading_strands.strategies_store.store import StrategyStore
            from trading_strands.tenancy.store import TenancyStore

            table = _make_table()
            tenancy = TenancyStore(table)
            alice = tenancy.create_user(email="alice@x.com")
            org = tenancy.create_org("A")
            tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)

            store = StrategyStore(table)
            strat = store.create(org.org_id, alice.user_id, "S", "# rules")

            # Make the bucket exist but don't put anything in it.
            boto3.client("s3", region_name="us-west-2").create_bucket(
                Bucket="test-agent-memory",
                CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
            )

            from trading_strands.dashboard.api import app

            client = TestClient(app, cookies=_session_cookie(
                alice.user_id, org.org_id,
            ))
            resp = client.get(f"/api/strategies/{strat.strategy_id}/lessons")
            assert resp.status_code == 200
            assert resp.json()["lessons"] == ""
    finally:
        del os.environ["AGENT_MEMORY_BUCKET"]


def test_critique_lessons_requires_auth() -> None:
    """No session cookie -> 401."""

    with mock_aws():
        _make_table()
        from trading_strands.dashboard.api import app

        client = TestClient(app)
        resp = client.get("/api/strategies/abc/lessons")
        assert resp.status_code == 401


def test_critique_lessons_503_when_bucket_unconfigured() -> None:
    """If AGENT_MEMORY_BUCKET isn't set, the dashboard should tell the
    operator rather than crash — this is a config issue they can fix."""

    # Explicitly clear the env var in case a prior test leaked it.
    os.environ.pop("AGENT_MEMORY_BUCKET", None)
    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.strategies_store.store import StrategyStore
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org = tenancy.create_org("A")
        tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)

        store = StrategyStore(table)
        strat = store.create(org.org_id, alice.user_id, "S", "# rules")

        from trading_strands.dashboard.api import app

        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, org.org_id,
        ))
        resp = client.get(f"/api/strategies/{strat.strategy_id}/lessons")
        assert resp.status_code == 503
        assert "AGENT_MEMORY_BUCKET" in resp.json().get("detail", "")


# ── Per-bot Fargate service state ───────────────────────────────────


def _ecs_client_mock(
    service_name: str, service: dict[str, Any] | None,
) -> MagicMock:
    """Build an ecs client mock whose describe_services returns either
    the service or the standard 'MISSING' sentinel ECS uses."""

    client = MagicMock()
    if service is None:
        client.describe_services.return_value = {
            "services": [{"status": "MISSING", "serviceName": service_name}],
        }
    else:
        client.describe_services.return_value = {"services": [service]}
    return client


def test_service_state_reports_running() -> None:
    """Operator sees a healthy per-bot service."""

    os.environ["ECS_CLUSTER"] = "ts-cluster"
    try:
        with mock_aws():
            from trading_strands.authz.model import Role
            from trading_strands.strategies_store.store import StrategyStore
            from trading_strands.tenancy.store import TenancyStore

            table = _make_table()
            tenancy = TenancyStore(table)
            alice = tenancy.create_user(email="alice@x.com")
            org = tenancy.create_org("A")
            tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)
            store = StrategyStore(table)
            strat = store.create(org.org_id, alice.user_id, "S", "# rules")

            svc_name = f"ts-strategy-{strat.strategy_id}"
            ecs_mock = _ecs_client_mock(svc_name, {
                "serviceName": svc_name,
                "status": "ACTIVE",
                "desiredCount": 1,
                "runningCount": 1,
                "pendingCount": 0,
                "taskDefinition": (
                    "arn:aws:ecs:us-west-2:0:task-definition/ts-bot-"
                    + strat.strategy_id + ":3"
                ),
            })
            with patch(
                "trading_strands.dashboard.api._get_ecs_client",
                return_value=ecs_mock,
            ):
                from trading_strands.dashboard.api import app
                client = TestClient(app, cookies=_session_cookie(
                    alice.user_id, org.org_id,
                ))
                resp = client.get(
                    f"/api/strategies/{strat.strategy_id}/service",
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["exists"] is True
                assert body["status"] == "ACTIVE"
                assert body["desired_count"] == 1
                assert body["running_count"] == 1
                assert body["task_definition_revision"] == 3
    finally:
        del os.environ["ECS_CLUSTER"]


def test_service_state_reports_missing_when_not_created() -> None:
    """A strategy created but never transitioned to per-bot — supervisor
    hasn't made a service yet. Dashboard must distinguish this from
    'service exists, scaled to zero'."""

    os.environ["ECS_CLUSTER"] = "ts-cluster"
    try:
        with mock_aws():
            from trading_strands.authz.model import Role
            from trading_strands.strategies_store.store import StrategyStore
            from trading_strands.tenancy.store import TenancyStore

            table = _make_table()
            tenancy = TenancyStore(table)
            alice = tenancy.create_user(email="alice@x.com")
            org = tenancy.create_org("A")
            tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)
            store = StrategyStore(table)
            strat = store.create(org.org_id, alice.user_id, "S", "# rules")

            svc_name = f"ts-strategy-{strat.strategy_id}"
            ecs_mock = _ecs_client_mock(svc_name, None)
            with patch(
                "trading_strands.dashboard.api._get_ecs_client",
                return_value=ecs_mock,
            ):
                from trading_strands.dashboard.api import app
                client = TestClient(app, cookies=_session_cookie(
                    alice.user_id, org.org_id,
                ))
                resp = client.get(
                    f"/api/strategies/{strat.strategy_id}/service",
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["exists"] is False
                assert body["status"] is None
    finally:
        del os.environ["ECS_CLUSTER"]


def test_service_state_reports_paused_when_scaled_to_zero() -> None:
    """Supervisor scales to 0 on PAUSE. Dashboard must not confuse
    this with 'missing'."""

    os.environ["ECS_CLUSTER"] = "ts-cluster"
    try:
        with mock_aws():
            from trading_strands.authz.model import Role
            from trading_strands.strategies_store.store import StrategyStore
            from trading_strands.tenancy.store import TenancyStore

            table = _make_table()
            tenancy = TenancyStore(table)
            alice = tenancy.create_user(email="alice@x.com")
            org = tenancy.create_org("A")
            tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)
            store = StrategyStore(table)
            strat = store.create(org.org_id, alice.user_id, "S", "# rules")

            svc_name = f"ts-strategy-{strat.strategy_id}"
            ecs_mock = _ecs_client_mock(svc_name, {
                "serviceName": svc_name,
                "status": "ACTIVE",
                "desiredCount": 0,
                "runningCount": 0,
                "pendingCount": 0,
                "taskDefinition": (
                    "arn:aws:ecs:us-west-2:0:task-definition/ts-bot-"
                    + strat.strategy_id + ":1"
                ),
            })
            with patch(
                "trading_strands.dashboard.api._get_ecs_client",
                return_value=ecs_mock,
            ):
                from trading_strands.dashboard.api import app
                client = TestClient(app, cookies=_session_cookie(
                    alice.user_id, org.org_id,
                ))
                resp = client.get(
                    f"/api/strategies/{strat.strategy_id}/service",
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["exists"] is True
                assert body["desired_count"] == 0
    finally:
        del os.environ["ECS_CLUSTER"]


def test_service_state_forbidden_for_other_org() -> None:
    """Cross-org — supervisor runs no service-state fishing."""

    os.environ["ECS_CLUSTER"] = "ts-cluster"
    try:
        with mock_aws():
            from trading_strands.authz.model import Role
            from trading_strands.strategies_store.store import StrategyStore
            from trading_strands.tenancy.store import TenancyStore

            table = _make_table()
            tenancy = TenancyStore(table)
            alice = tenancy.create_user(email="alice@x.com")
            bob = tenancy.create_user(email="bob@x.com")
            org_a = tenancy.create_org("A")
            org_b = tenancy.create_org("B")
            tenancy.add_membership(alice.user_id, org_a.org_id, Role.OPERATOR)
            tenancy.add_membership(bob.user_id, org_b.org_id, Role.OPERATOR)
            store = StrategyStore(table)
            strat = store.create(org_b.org_id, bob.user_id, "S", "# r")

            from trading_strands.dashboard.api import app
            client = TestClient(app, cookies=_session_cookie(
                alice.user_id, org_a.org_id,
            ))
            resp = client.get(
                f"/api/strategies/{strat.strategy_id}/service",
            )
            assert resp.status_code == 403
    finally:
        del os.environ["ECS_CLUSTER"]


def test_service_state_503_when_cluster_unconfigured() -> None:
    """Dev instance or a misdeployed stack: tell the operator."""

    os.environ.pop("ECS_CLUSTER", None)
    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.strategies_store.store import StrategyStore
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org = tenancy.create_org("A")
        tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)
        store = StrategyStore(table)
        strat = store.create(org.org_id, alice.user_id, "S", "# r")

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, org.org_id,
        ))
        resp = client.get(f"/api/strategies/{strat.strategy_id}/service")
        assert resp.status_code == 503
        assert "ECS_CLUSTER" in resp.json().get("detail", "")


def test_service_state_requires_auth() -> None:
    with mock_aws():
        _make_table()
        from trading_strands.dashboard.api import app
        client = TestClient(app)
        resp = client.get("/api/strategies/abc/service")
        assert resp.status_code == 401


# ── Org-level review-agent recommendations ─────────────────────────


def _put_recs_in_s3(
    bucket: str, org_id: str, agent_type: str, content: str,
) -> None:
    """Seed a recommendations.md under the review-agent's prefix.

    The review agents (Risk/Compliance/Auditor) use agent_id == org_id
    by convention — one memory bucket per (org, agent_type)."""

    import contextlib as _contextlib
    s3 = boto3.client("s3", region_name="us-west-2")
    with _contextlib.suppress(s3.exceptions.BucketAlreadyOwnedByYou):
        s3.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
    s3.put_object(
        Bucket=bucket,
        Key=f"{org_id}/{agent_type}/{org_id}/recommendations.md",
        Body=content.encode("utf-8"),
    )


def test_org_recommendations_returns_risk_file_for_member() -> None:
    os.environ["AGENT_MEMORY_BUCKET"] = "test-agent-memory"
    try:
        with mock_aws():
            from trading_strands.authz.model import Role
            from trading_strands.tenancy.store import TenancyStore

            table = _make_table()
            tenancy = TenancyStore(table)
            alice = tenancy.create_user(email="alice@x.com")
            org = tenancy.create_org("Ops")
            tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)

            _put_recs_in_s3(
                "test-agent-memory", org.org_id, "risk",
                "## 2026-04-26 — risk review\n\nNVDA concentration 40%.",
            )

            from trading_strands.dashboard.api import app
            client = TestClient(app, cookies=_session_cookie(
                alice.user_id, org.org_id,
            ))
            resp = client.get(
                f"/api/orgs/{org.org_id}/recommendations/risk",
            )
            assert resp.status_code == 200
            body = resp.json()
            assert "NVDA concentration" in body["recommendations"]
            assert body["agent_type"] == "risk"
            assert body["org_id"] == org.org_id
    finally:
        del os.environ["AGENT_MEMORY_BUCKET"]


def test_org_recommendations_empty_when_agent_has_not_run() -> None:
    """Fresh org, no review cycle has run yet. Return 200 with an
    empty string rather than 404 — the UI renders an empty panel,
    not an error."""

    os.environ["AGENT_MEMORY_BUCKET"] = "test-agent-memory"
    try:
        with mock_aws():
            from trading_strands.authz.model import Role
            from trading_strands.tenancy.store import TenancyStore

            table = _make_table()
            tenancy = TenancyStore(table)
            alice = tenancy.create_user(email="alice@x.com")
            org = tenancy.create_org("Ops")
            tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)

            # Bucket exists but no file.
            boto3.client("s3", region_name="us-west-2").create_bucket(
                Bucket="test-agent-memory",
                CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
            )

            from trading_strands.dashboard.api import app
            client = TestClient(app, cookies=_session_cookie(
                alice.user_id, org.org_id,
            ))
            resp = client.get(
                f"/api/orgs/{org.org_id}/recommendations/compliance",
            )
            assert resp.status_code == 200
            assert resp.json()["recommendations"] == ""
    finally:
        del os.environ["AGENT_MEMORY_BUCKET"]


def test_org_recommendations_forbidden_for_non_member() -> None:
    """Cross-org reads must 403 — recommendations are org-sensitive."""

    os.environ["AGENT_MEMORY_BUCKET"] = "test-agent-memory"
    try:
        with mock_aws():
            from trading_strands.authz.model import Role
            from trading_strands.tenancy.store import TenancyStore

            table = _make_table()
            tenancy = TenancyStore(table)
            alice = tenancy.create_user(email="alice@x.com")
            bob = tenancy.create_user(email="bob@x.com")
            org_a = tenancy.create_org("A")
            org_b = tenancy.create_org("B")
            tenancy.add_membership(alice.user_id, org_a.org_id, Role.OPERATOR)
            tenancy.add_membership(bob.user_id, org_b.org_id, Role.OPERATOR)

            from trading_strands.dashboard.api import app
            client = TestClient(app, cookies=_session_cookie(
                alice.user_id, org_a.org_id,
            ))
            resp = client.get(
                f"/api/orgs/{org_b.org_id}/recommendations/risk",
            )
            assert resp.status_code == 403
    finally:
        del os.environ["AGENT_MEMORY_BUCKET"]


def test_org_recommendations_rejects_unknown_agent_type() -> None:
    """Only risk, compliance, auditor are valid. A typo'd path must
    not trick the dashboard into reading arbitrary S3 keys — this is
    the injection surface for 'recommendations.md but under a
    different prefix'."""

    os.environ["AGENT_MEMORY_BUCKET"] = "test-agent-memory"
    try:
        with mock_aws():
            from trading_strands.authz.model import Role
            from trading_strands.tenancy.store import TenancyStore

            table = _make_table()
            tenancy = TenancyStore(table)
            alice = tenancy.create_user(email="alice@x.com")
            org = tenancy.create_org("Ops")
            tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)

            from trading_strands.dashboard.api import app
            client = TestClient(app, cookies=_session_cookie(
                alice.user_id, org.org_id,
            ))
            resp = client.get(
                f"/api/orgs/{org.org_id}/recommendations/strategy",
            )
            assert resp.status_code == 400
            # And something obviously hostile like a path traversal.
            resp = client.get(
                f"/api/orgs/{org.org_id}/recommendations/..%2Fsomething",
            )
            assert resp.status_code in (400, 404)
    finally:
        del os.environ["AGENT_MEMORY_BUCKET"]


def test_org_recommendations_503_when_bucket_unconfigured() -> None:
    os.environ.pop("AGENT_MEMORY_BUCKET", None)
    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org = tenancy.create_org("Ops")
        tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, org.org_id,
        ))
        resp = client.get(
            f"/api/orgs/{org.org_id}/recommendations/risk",
        )
        assert resp.status_code == 503


def test_org_recommendations_requires_auth() -> None:
    with mock_aws():
        _make_table()
        from trading_strands.dashboard.api import app
        client = TestClient(app)
        resp = client.get("/api/orgs/abc/recommendations/risk")
        assert resp.status_code == 401


# ── GET /api/halt (scoped halt status) ──────────────────────────────


def test_get_halt_returns_both_scopes_for_member() -> None:
    """Any authenticated user can read halt state — they need to know
    whether trading is running. Per-org data only for orgs they belong
    to (system state is coarse + universally observable)."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.halt.store import HaltStore
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org = tenancy.create_org("Ops")
        tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)

        hs = HaltStore(table)
        hs.set_org_halt(org.org_id, True, reason="auditor: drift")

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, org.org_id,
        ))
        resp = client.get("/api/halt")
        assert resp.status_code == 200
        body = resp.json()
        assert body["system"]["halted"] is False
        assert body["org"]["halted"] is True
        assert body["effective_halted"] is True
        assert "auditor" in (body["org"]["reason"] or "").lower()


def test_get_halt_shows_system_halt_for_anyone() -> None:
    """A sysadmin emergency stop is visible to every user — they need
    to know trading is frozen."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.halt.store import HaltStore
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org = tenancy.create_org("Ops")
        tenancy.add_membership(alice.user_id, org.org_id, Role.VIEWER)

        HaltStore(table).set_system_halt(True, reason="sysadmin emergency")

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, org.org_id,
        ))
        resp = client.get("/api/halt")
        assert resp.status_code == 200
        body = resp.json()
        assert body["system"]["halted"] is True
        assert body["effective_halted"] is True


def test_get_halt_without_active_org_returns_system_only() -> None:
    """A sysadmin who hasn't picked an active org still gets system
    state — empty org block rather than a 400."""

    with mock_aws():
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        tenancy.grant_sysadmin(alice.user_id)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, None,
        ))
        resp = client.get("/api/halt")
        assert resp.status_code == 200
        body = resp.json()
        assert body["system"]["halted"] is False
        assert body["org"] is None
        assert body["effective_halted"] is False


def test_get_halt_requires_auth() -> None:
    with mock_aws():
        _make_table()
        from trading_strands.dashboard.api import app
        client = TestClient(app)
        resp = client.get("/api/halt")
        assert resp.status_code == 401


def test_get_halt_surfaces_permissions_for_ui() -> None:
    """UI uses these flags to enable/disable the system-halt option in
    the scoped-halt dialog."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)

        admin = tenancy.create_user(email="admin@x.com")
        viewer = tenancy.create_user(email="viewer@x.com")
        org = tenancy.create_org("Ops")
        tenancy.add_membership(admin.user_id, org.org_id, Role.ORGADMIN)
        tenancy.add_membership(viewer.user_id, org.org_id, Role.VIEWER)

        from trading_strands.dashboard.api import app

        resp_admin = TestClient(app, cookies=_session_cookie(
            admin.user_id, org.org_id,
        )).get("/api/halt")
        assert resp_admin.json()["can_halt_org"] is True
        assert resp_admin.json()["can_halt_system"] is False

        resp_viewer = TestClient(app, cookies=_session_cookie(
            viewer.user_id, org.org_id,
        )).get("/api/halt")
        assert resp_viewer.json()["can_halt_org"] is False
        assert resp_viewer.json()["can_halt_system"] is False


def test_get_halt_sysadmin_can_halt_system() -> None:
    with mock_aws():
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        root = tenancy.create_user(email="root@x.com")
        tenancy.grant_sysadmin(root.user_id)

        from trading_strands.dashboard.api import app
        resp = TestClient(app, cookies=_session_cookie(
            root.user_id, None,
        )).get("/api/halt")
        assert resp.status_code == 200
        body = resp.json()
        assert body["can_halt_system"] is True


# ── Review-agent heartbeat timestamps ───────────────────────────────


def test_review_heartbeats_returns_all_three_timestamps() -> None:
    """When all three review agents have beat, the endpoint returns
    each timestamp keyed by agent type."""

    import time as _time

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.heartbeat.store import HeartbeatStore
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org = tenancy.create_org("Ops")
        tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)

        hs = HeartbeatStore(table)
        hs.beat("risk", org.org_id)
        hs.beat("compliance", org.org_id)
        hs.beat("auditor", org.org_id)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, org.org_id,
        ))
        resp = client.get(
            f"/api/orgs/{org.org_id}/heartbeats/review",
        )
        assert resp.status_code == 200
        body = resp.json()
        now = int(_time.time())
        # Each timestamp should be near-now (agent just beat).
        for agent_type in ("risk", "compliance", "auditor"):
            assert body[agent_type] is not None
            assert abs(body[agent_type] - now) < 5


def test_review_heartbeats_returns_null_when_never_run() -> None:
    """A fresh org with no review runs yet — each agent is null."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org = tenancy.create_org("Ops")
        tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, org.org_id,
        ))
        resp = client.get(
            f"/api/orgs/{org.org_id}/heartbeats/review",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"risk": None, "compliance": None, "auditor": None}


def test_review_heartbeats_forbidden_for_non_member() -> None:
    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        bob = tenancy.create_user(email="bob@x.com")
        org_a = tenancy.create_org("A")
        org_b = tenancy.create_org("B")
        tenancy.add_membership(alice.user_id, org_a.org_id, Role.OPERATOR)
        tenancy.add_membership(bob.user_id, org_b.org_id, Role.OPERATOR)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, org_a.org_id,
        ))
        resp = client.get(
            f"/api/orgs/{org_b.org_id}/heartbeats/review",
        )
        assert resp.status_code == 403


def test_review_heartbeats_requires_auth() -> None:
    with mock_aws():
        _make_table()
        from trading_strands.dashboard.api import app
        client = TestClient(app)
        resp = client.get("/api/orgs/abc/heartbeats/review")
        assert resp.status_code == 401


def test_review_heartbeats_partial_coverage() -> None:
    """Realistic case — Risk ran but Compliance + Auditor are new
    and haven't run yet. Endpoint returns mixed populated/null."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.heartbeat.store import HeartbeatStore
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org = tenancy.create_org("Ops")
        tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)

        HeartbeatStore(table).beat("risk", org.org_id)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, org.org_id,
        ))
        resp = client.get(
            f"/api/orgs/{org.org_id}/heartbeats/review",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["risk"] is not None
        assert body["compliance"] is None
        assert body["auditor"] is None


# ── Platform Supervisor view ────────────────────────────────────────


def test_supervisor_agents_returns_heartbeat_classification() -> None:
    """Authenticated user sees the supervisor's classification: one
    entry per heartbeat row with status=healthy/stale/missing/untracked."""

    import time as _time

    with mock_aws():
        from trading_strands.heartbeat.store import HeartbeatStore

        table = _make_table()
        uid, oid = _make_user(table)
        hb = HeartbeatStore(table)
        hb.beat("strategy", "strategy-abc")
        # Manually write a stale fast-cadence row.
        table.put_item(Item={
            "pk": "HEARTBEAT#strategy#strategy-old",
            "agent_type": "strategy",
            "agent_id": "strategy-old",
            "last_beat_ts": int(_time.time() - 3600),
            "ttl": int(_time.time() + 3600),
        })
        # And a review-agent untracked beat.
        hb.beat("risk", oid)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/supervisor/agents")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False  # one missing fast-cadence agent
        assert body["total"] == 3
        statuses = {a["agent_id"]: a["status"] for a in body["agents"]}
        assert statuses["strategy-abc"] == "healthy"
        assert statuses["strategy-old"] == "missing"
        assert statuses[oid] == "untracked"


def test_supervisor_agents_empty_table_is_ok() -> None:
    """Fresh deploy: zero heartbeats, the supervisor reports ok."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/supervisor/agents")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["ok"] is True
        assert body["agents"] == []


def test_supervisor_agents_requires_auth() -> None:
    with mock_aws():
        _make_table()
        from trading_strands.dashboard.api import app
        client = TestClient(app)
        resp = client.get("/api/supervisor/agents")
        assert resp.status_code == 401


def test_supervisor_agents_uses_env_thresholds() -> None:
    """The endpoint honors the same thresholds as the Lambda, read
    from env — so operators see the same classification in the UI
    that CW alarms fire on."""

    import time as _time

    os.environ["SUPERVISOR_STALE_AFTER_SECONDS"] = "10"
    os.environ["SUPERVISOR_MISSING_AFTER_SECONDS"] = "30"
    try:
        with mock_aws():
            table = _make_table()
            uid, oid = _make_user(table)
            # 20s ago — stale under tight thresholds, healthy under defaults.
            table.put_item(Item={
                "pk": "HEARTBEAT#strategy#bot-1",
                "agent_type": "strategy",
                "agent_id": "bot-1",
                "last_beat_ts": int(_time.time() - 20),
                "ttl": int(_time.time() + 3600),
            })
            from trading_strands.dashboard.api import app
            client = TestClient(app, cookies=_session_cookie(uid, oid))
            resp = client.get("/api/supervisor/agents")
            body = resp.json()
            assert body["agents"][0]["status"] == "stale"
    finally:
        del os.environ["SUPERVISOR_STALE_AFTER_SECONDS"]
        del os.environ["SUPERVISOR_MISSING_AFTER_SECONDS"]


# ── Login page expired-session hint ─────────────────────────────────


def test_login_page_shows_expired_notice() -> None:
    """The dashboard's fetch interceptor redirects to /login?expired=1
    when an API call returns 401. The login page must render a visible
    notice for that query param so users understand why they landed
    here."""

    with mock_aws():
        _make_table()
        from trading_strands.dashboard.api import app

        client = TestClient(app)
        resp = client.get("/login?expired=1")
        assert resp.status_code == 200
        assert "Your session expired" in resp.text


def test_login_page_no_notice_when_fresh() -> None:
    """No expired param → no notice. Rules out an always-present hint
    that would be confusing on a normal login."""

    with mock_aws():
        _make_table()
        from trading_strands.dashboard.api import app

        client = TestClient(app)
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "Your session expired" not in resp.text


# ── Supervisor alarms endpoint ──────────────────────────────────────


def _alarm_item(
    name: str, state: str = "OK",
    reason: str = "", actions_enabled: bool = False,
    last_change: str = "2026-04-27T00:00:00+00:00",
) -> dict[str, Any]:
    """Match the shape cloudwatch.describe_alarms returns."""

    return {
        "AlarmName": name,
        "StateValue": state,
        "StateReason": reason,
        "ActionsEnabled": actions_enabled,
        "StateUpdatedTimestamp": last_change,
    }


def test_supervisor_alarms_returns_three_expected() -> None:
    """Happy path: the three alarms we created in CDK show up in the
    response, states pulled through verbatim."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        cw_mock = MagicMock()
        cw_mock.describe_alarms.return_value = {
            "MetricAlarms": [
                _alarm_item("trading-strands-system-halt", state="OK"),
                _alarm_item(
                    "trading-strands-org-halt", state="ALARM",
                    reason="Threshold crossed: 1 out of 1 > 1.0",
                ),
                _alarm_item(
                    "trading-strands-missing-agents",
                    state="INSUFFICIENT_DATA",
                ),
            ],
        }

        with patch(
            "trading_strands.dashboard.api._get_cloudwatch_client",
            return_value=cw_mock,
        ):
            from trading_strands.dashboard.api import app
            client = TestClient(app, cookies=_session_cookie(uid, oid))
            resp = client.get("/api/supervisor/alarms")
            assert resp.status_code == 200
            body = resp.json()

        assert len(body["alarms"]) == 3
        states = {a["name"]: a["state"] for a in body["alarms"]}
        assert states == {
            "trading-strands-system-halt": "OK",
            "trading-strands-org-halt": "ALARM",
            "trading-strands-missing-agents": "INSUFFICIENT_DATA",
        }
        # All three have actions_enabled=False per current CDK — the
        # endpoint should carry that through so UI can warn.
        assert all(a["actions_enabled"] is False for a in body["alarms"])


def test_supervisor_alarms_ok_flag_reflects_worst_state() -> None:
    """Any ALARM = ok:False. OK alarms alone = ok:True. Helps the UI
    render a single summary badge without re-walking the list."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        all_ok = MagicMock()
        all_ok.describe_alarms.return_value = {
            "MetricAlarms": [
                _alarm_item("trading-strands-system-halt"),
                _alarm_item("trading-strands-org-halt"),
            ],
        }

        with patch(
            "trading_strands.dashboard.api._get_cloudwatch_client",
            return_value=all_ok,
        ):
            from trading_strands.dashboard.api import app
            client = TestClient(app, cookies=_session_cookie(uid, oid))
            assert client.get("/api/supervisor/alarms").json()["ok"] is True

        one_alarm = MagicMock()
        one_alarm.describe_alarms.return_value = {
            "MetricAlarms": [
                _alarm_item("trading-strands-system-halt"),
                _alarm_item("trading-strands-org-halt", state="ALARM"),
            ],
        }

        with patch(
            "trading_strands.dashboard.api._get_cloudwatch_client",
            return_value=one_alarm,
        ):
            from trading_strands.dashboard.api import app
            client = TestClient(app, cookies=_session_cookie(uid, oid))
            assert client.get("/api/supervisor/alarms").json()["ok"] is False


def test_supervisor_alarms_empty_when_none_configured() -> None:
    """Fresh environment — alarms not yet deployed. Return empty list,
    not an error. UI shows 'no alarms configured'."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        cw = MagicMock()
        cw.describe_alarms.return_value = {"MetricAlarms": []}

        with patch(
            "trading_strands.dashboard.api._get_cloudwatch_client",
            return_value=cw,
        ):
            from trading_strands.dashboard.api import app
            client = TestClient(app, cookies=_session_cookie(uid, oid))
            resp = client.get("/api/supervisor/alarms")
            assert resp.status_code == 200
            body = resp.json()
            assert body["alarms"] == []
            assert body["ok"] is True


def test_supervisor_alarms_cw_failure_returns_503() -> None:
    """CW API error shouldn't crash the dashboard — return 503 so the
    UI can render 'alarm state unavailable' rather than the generic
    500 and a blank panel."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        cw = MagicMock()
        cw.describe_alarms.side_effect = RuntimeError("AccessDenied")

        with patch(
            "trading_strands.dashboard.api._get_cloudwatch_client",
            return_value=cw,
        ):
            from trading_strands.dashboard.api import app
            client = TestClient(app, cookies=_session_cookie(uid, oid))
            resp = client.get("/api/supervisor/alarms")
            assert resp.status_code == 503
            assert "alarm" in resp.json().get("detail", "").lower()


def test_supervisor_alarms_requires_auth() -> None:
    with mock_aws():
        _make_table()
        from trading_strands.dashboard.api import app
        client = TestClient(app)
        resp = client.get("/api/supervisor/alarms")
        assert resp.status_code == 401


def test_supervisor_alarms_filters_to_our_alarms_only() -> None:
    """If the AWS account has other CW alarms unrelated to this stack,
    they must not leak into the response. Filter on the name prefix."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        cw = MagicMock()
        cw.describe_alarms.return_value = {
            "MetricAlarms": [
                _alarm_item("trading-strands-system-halt"),
                _alarm_item("some-other-teams-alarm"),
                _alarm_item("trading-strands-missing-agents"),
            ],
        }

        with patch(
            "trading_strands.dashboard.api._get_cloudwatch_client",
            return_value=cw,
        ):
            from trading_strands.dashboard.api import app
            client = TestClient(app, cookies=_session_cookie(uid, oid))
            body = client.get("/api/supervisor/alarms").json()

        names = {a["name"] for a in body["alarms"]}
        assert "some-other-teams-alarm" not in names
        assert names == {
            "trading-strands-system-halt",
            "trading-strands-missing-agents",
        }


# ── Halt events endpoint ────────────────────────────────────────────


def test_halt_events_returns_newest_first() -> None:
    import time as _time

    with mock_aws():
        from trading_strands.halt.store import HaltStore

        table = _make_table()
        uid, oid = _make_user(table)

        hs = HaltStore(table)
        hs.set_system_halt(True, reason="first")
        _time.sleep(1.05)
        hs.set_org_halt(oid, True, reason="second")

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/halt/events")
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 2
        # Newest first.
        assert events[0]["reason"] == "second"
        assert events[0]["scope"] == "org"
        assert events[1]["reason"] == "first"
        assert events[1]["scope"] == "system"


def test_halt_events_empty_when_none_ever() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/halt/events")
        assert resp.status_code == 200
        assert resp.json()["events"] == []


def test_halt_events_rejects_bad_limit() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        assert client.get("/api/halt/events?limit=0").status_code == 400
        assert client.get("/api/halt/events?limit=1000").status_code == 400


def test_halt_events_requires_auth() -> None:
    with mock_aws():
        _make_table()
        from trading_strands.dashboard.api import app
        client = TestClient(app)
        resp = client.get("/api/halt/events")
        assert resp.status_code == 401


# ── Halt scope resolution edge cases ────────────────────────────────


def test_halt_explicit_system_scope_by_non_sysadmin_is_403() -> None:
    """An orgadmin (no sysadmin) trying scope=system is asking for a
    power they don't have. 403, not silently downgraded to org."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post("/api/halt", json={"scope": "system"})
        assert resp.status_code == 403
        assert "sysadmin" in resp.json().get("detail", "").lower()


def test_halt_explicit_org_by_non_member_is_403() -> None:
    """Even an orgadmin can't halt an org they're not in."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org_a = tenancy.create_org("A")
        org_b = tenancy.create_org("B")
        tenancy.add_membership(alice.user_id, org_a.org_id, Role.ORGADMIN)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, org_a.org_id,
        ))
        resp = client.post(
            "/api/halt", json={"scope": "org", "org_id": org_b.org_id},
        )
        assert resp.status_code == 403


def test_halt_explicit_org_no_org_id_no_active_is_400() -> None:
    """scope=org with neither body.org_id nor an active org = 400."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org = tenancy.create_org("A")
        tenancy.add_membership(alice.user_id, org.org_id, Role.ORGADMIN)

        from trading_strands.dashboard.api import app
        # Cookie with active_org_id=None so the lenient active lookup
        # returns empty.
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, None,
        ))
        resp = client.post("/api/halt", json={"scope": "org"})
        assert resp.status_code == 400
        assert "org_id" in resp.json().get("detail", "").lower()


def test_halt_no_scope_viewer_only_is_403() -> None:
    """A user who's only a viewer — no orgadmin anywhere, no
    sysadmin — can't halt. The resolution path must reject rather
    than silently pick 'system'."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org = tenancy.create_org("A")
        tenancy.add_membership(alice.user_id, org.org_id, Role.VIEWER)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, org.org_id,
        ))
        resp = client.post("/api/halt")
        assert resp.status_code == 403
        assert "orgadmin" in resp.json().get("detail", "").lower()


def test_halt_no_scope_multi_orgadmin_no_active_requires_org_id() -> None:
    """Orgadmin of multiple orgs with no active org can't implicitly
    halt — they must specify which org."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        a = tenancy.create_org("A")
        b = tenancy.create_org("B")
        tenancy.add_membership(alice.user_id, a.org_id, Role.ORGADMIN)
        tenancy.add_membership(alice.user_id, b.org_id, Role.ORGADMIN)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, None,
        ))
        resp = client.post("/api/halt")
        assert resp.status_code == 400
        assert "multiple" in resp.json().get("detail", "").lower()


def test_halt_no_scope_single_orgadmin_no_active_falls_back_to_that_org() -> None:
    """Orgadmin of exactly one org, no active, no scope — resolve
    to that org. Avoids requiring explicit org_id when there's no
    ambiguity."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.halt.store import HaltStore
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org = tenancy.create_org("A")
        tenancy.add_membership(alice.user_id, org.org_id, Role.ORGADMIN)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, None,
        ))
        resp = client.post("/api/halt", json={"reason": "single-org test"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["scope"] == "org"
        assert body["org_id"] == org.org_id

        # And the HaltStore row actually landed on that org, not
        # system-wide or a sibling.
        assert HaltStore(table).is_org_halted(org.org_id) is True
        assert HaltStore(table).is_system_halted() is False


def test_halt_sysadmin_no_scope_defaults_to_system() -> None:
    """Sysadmin with no explicit scope gets the broadest power they
    have — system. Documented in _halt_scope_and_org."""

    with mock_aws():
        from trading_strands.halt.store import HaltStore
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        root = tenancy.create_user(email="root@x.com")
        tenancy.grant_sysadmin(root.user_id)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            root.user_id, None,
        ))
        resp = client.post("/api/halt", json={"reason": "sysadmin test"})
        assert resp.status_code == 200
        assert resp.json()["scope"] == "system"
        assert HaltStore(table).is_system_halted() is True


def test_halt_sysadmin_can_halt_any_org_by_id() -> None:
    """Sysadmin explicit scope=org halts that org without needing
    membership. Complements the org-halt=orgadmin rule; sysadmin is
    a super-right across orgs."""

    with mock_aws():
        from trading_strands.halt.store import HaltStore
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        root = tenancy.create_user(email="root@x.com")
        tenancy.grant_sysadmin(root.user_id)
        # Sysadmin is NOT in any org.
        target_org = tenancy.create_org("target")

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            root.user_id, None,
        ))
        resp = client.post("/api/halt", json={
            "scope": "org", "org_id": target_org.org_id,
            "reason": "sysadmin halting on behalf",
        })
        assert resp.status_code == 200
        assert resp.json()["org_id"] == target_org.org_id
        assert HaltStore(table).is_org_halted(target_org.org_id) is True


# ── Per-org tools endpoints ─────────────────────────────────────────


def test_list_org_tools_requires_membership() -> None:
    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        bob = tenancy.create_user(email="bob@x.com")
        org_a = tenancy.create_org("A")
        org_b = tenancy.create_org("B")
        tenancy.add_membership(alice.user_id, org_a.org_id, Role.OPERATOR)
        tenancy.add_membership(bob.user_id, org_b.org_id, Role.OPERATOR)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, org_a.org_id,
        ))
        # Alice listing org_b → 403.
        resp = client.get(f"/api/orgs/{org_b.org_id}/tools")
        assert resp.status_code == 403


def test_set_org_tool_requires_orgadmin() -> None:
    """Operators can't flip tool availability — only orgadmins."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org = tenancy.create_org("A")
        tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, org.org_id,
        ))
        resp = client.put(
            f"/api/orgs/{org.org_id}/tools/news",
            json={"enabled": True},
        )
        assert resp.status_code == 403


def test_orgadmin_can_enable_tool_and_list_it() -> None:
    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        admin = tenancy.create_user(email="admin@x.com")
        org = tenancy.create_org("A")
        tenancy.add_membership(admin.user_id, org.org_id, Role.ORGADMIN)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            admin.user_id, org.org_id,
        ))
        resp = client.put(
            f"/api/orgs/{org.org_id}/tools/news",
            json={"enabled": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tool_name"] == "news"
        assert body["enabled"] is True

        # Now list returns it.
        resp = client.get(f"/api/orgs/{org.org_id}/tools")
        assert resp.status_code == 200
        tools = resp.json()
        assert len(tools) == 1
        assert tools[0]["tool_name"] == "news"


# ── Per-org skills endpoints ────────────────────────────────────────


def test_put_skill_requires_orgadmin() -> None:
    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org = tenancy.create_org("A")
        tenancy.add_membership(alice.user_id, org.org_id, Role.OPERATOR)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, org.org_id,
        ))
        resp = client.put(
            f"/api/orgs/{org.org_id}/skills/morning_prep",
            json={"markdown": "## Morning prep\n\nCheck calendar."},
        )
        assert resp.status_code == 403


def test_orgadmin_skill_crud_full_cycle() -> None:
    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        admin = tenancy.create_user(email="admin@x.com")
        org = tenancy.create_org("A")
        tenancy.add_membership(admin.user_id, org.org_id, Role.ORGADMIN)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            admin.user_id, org.org_id,
        ))
        # Create
        resp = client.put(
            f"/api/orgs/{org.org_id}/skills/morning",
            json={"markdown": "body v1"},
        )
        assert resp.status_code == 200
        assert resp.json()["markdown"] == "body v1"

        # List
        resp = client.get(f"/api/orgs/{org.org_id}/skills")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        # Get
        resp = client.get(f"/api/orgs/{org.org_id}/skills/morning")
        assert resp.status_code == 200
        assert resp.json()["skill_name"] == "morning"

        # Update
        resp = client.put(
            f"/api/orgs/{org.org_id}/skills/morning",
            json={"markdown": "body v2"},
        )
        assert resp.json()["markdown"] == "body v2"

        # Delete
        resp = client.delete(f"/api/orgs/{org.org_id}/skills/morning")
        assert resp.status_code == 204

        # Now 404.
        resp = client.get(f"/api/orgs/{org.org_id}/skills/morning")
        assert resp.status_code == 404


def test_skill_cross_org_list_forbidden() -> None:
    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        bob = tenancy.create_user(email="bob@x.com")
        org_a = tenancy.create_org("A")
        org_b = tenancy.create_org("B")
        tenancy.add_membership(alice.user_id, org_a.org_id, Role.OPERATOR)
        tenancy.add_membership(bob.user_id, org_b.org_id, Role.OPERATOR)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            alice.user_id, org_a.org_id,
        ))
        resp = client.get(f"/api/orgs/{org_b.org_id}/skills")
        assert resp.status_code == 403


def test_skill_too_large_returns_400() -> None:
    """32 KB cap at the store layer → API surfaces as 400 with detail."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        admin = tenancy.create_user(email="admin@x.com")
        org = tenancy.create_org("A")
        tenancy.add_membership(admin.user_id, org.org_id, Role.ORGADMIN)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(
            admin.user_id, org.org_id,
        ))
        resp = client.put(
            f"/api/orgs/{org.org_id}/skills/big",
            json={"markdown": "x" * (32 * 1024 + 1)},
        )
        assert resp.status_code == 400


# ── Strategy edit accepts tools + skills ────────────────────────────


def test_create_strategy_with_tools_and_skills() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post("/api/strategies", json={
            "name": "test",
            "markdown": "# strat\nbuy",
            "symbols": ["AAPL"],
            "capital": "1000",
            "tools": {
                "news": {"enabled": True, "daily_quota": 20},
            },
            "skills": ["morning_prep"],
        })
        assert resp.status_code == 201
        body = resp.json()
        assert body["tools"]["news"]["enabled"] is True
        assert body["tools"]["news"]["daily_quota"] == 20
        assert body["skills"] == ["morning_prep"]


# ── Strategy proposals (self-critique prompt edits) ───────────────────


def test_list_strategy_proposals_returns_store_entries() -> None:
    """Any org member with READ on the strategy can list its proposals."""

    with mock_aws():
        from trading_strands.strategies_store.store import StrategyStore
        from trading_strands.strategy_proposals.store import (
            StrategyProposalsStore,
        )
        table = _make_table()
        uid, oid = _make_user(table, role_name="viewer")

        strat = StrategyStore(table).create(
            org_id=oid, author_user_id="author-1",
            name="S", markdown="# old",
        )
        proposals = StrategyProposalsStore(table)
        proposals.create(
            strategy_id=strat.strategy_id, org_id=oid,
            proposer_agent="self_critique",
            proposer_agent_id="sc-1",
            rationale="tighten exit", proposed_markdown="# new",
        )

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get(
            f"/api/strategies/{strat.strategy_id}/proposals",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["status"] == "pending"
        assert body[0]["proposed_markdown"] == "# new"


def test_apply_strategy_proposal_rewrites_prompt_and_marks_applied() -> None:
    """Author applies a pending proposal — strategy.markdown gets the
    proposed text, proposal transitions to APPLIED."""

    with mock_aws():
        from trading_strands.strategies_store.store import StrategyStore
        from trading_strands.strategy_proposals.store import (
            StrategyProposalsStore,
        )
        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")

        store = StrategyStore(table)
        strat = store.create(
            org_id=oid, author_user_id=uid, name="S",
            markdown="# original rules",
        )
        proposals = StrategyProposalsStore(table)
        p = proposals.create(
            strategy_id=strat.strategy_id, org_id=oid,
            proposer_agent="self_critique",
            proposer_agent_id="sc-1",
            rationale="add exit", proposed_markdown="# refined rules",
        )

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post(
            f"/api/strategies/{strat.strategy_id}"
            f"/proposals/{p.proposal_id}/apply",
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "applied"
        assert resp.json()["decided_by"] == uid

        # Strategy's markdown was actually rewritten.
        after = store.get(strat.strategy_id)
        assert after.markdown == "# refined rules"


def test_reject_proposal_marks_rejected_and_leaves_strategy_unchanged() -> None:
    with mock_aws():
        from trading_strands.strategies_store.store import StrategyStore
        from trading_strands.strategy_proposals.store import (
            StrategyProposalsStore,
        )
        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")

        store = StrategyStore(table)
        strat = store.create(
            org_id=oid, author_user_id=uid, name="S",
            markdown="# original",
        )
        proposals = StrategyProposalsStore(table)
        p = proposals.create(
            strategy_id=strat.strategy_id, org_id=oid,
            proposer_agent="self_critique",
            proposer_agent_id="sc-1",
            rationale="ignore this", proposed_markdown="# replacement",
        )

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post(
            f"/api/strategies/{strat.strategy_id}"
            f"/proposals/{p.proposal_id}/reject",
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"
        assert store.get(strat.strategy_id).markdown == "# original"


def test_apply_already_decided_proposal_409s() -> None:
    """Single-transition invariant: once applied, you can't re-apply
    or switch to rejected via the endpoint."""

    with mock_aws():
        from trading_strands.strategies_store.store import StrategyStore
        from trading_strands.strategy_proposals.store import (
            ProposalStatus,
            StrategyProposalsStore,
        )
        table = _make_table()
        uid, oid = _make_user(table, role_name="operator")

        store = StrategyStore(table)
        strat = store.create(
            org_id=oid, author_user_id=uid, name="S", markdown="# m",
        )
        proposals = StrategyProposalsStore(table)
        p = proposals.create(
            strategy_id=strat.strategy_id, org_id=oid,
            proposer_agent="self_critique", proposer_agent_id="sc",
            rationale="r", proposed_markdown="# m2",
        )
        proposals.decide(
            strategy_id=strat.strategy_id, proposal_id=p.proposal_id,
            status=ProposalStatus.APPLIED, decided_by=uid,
        )

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post(
            f"/api/strategies/{strat.strategy_id}"
            f"/proposals/{p.proposal_id}/apply",
        )
        assert resp.status_code == 409


def test_viewer_cannot_apply_proposal() -> None:
    """A viewer can READ proposals but cannot UPDATE — apply is gated
    at the authz layer same as direct strategy edits."""

    with mock_aws():
        from trading_strands.strategies_store.store import StrategyStore
        from trading_strands.strategy_proposals.store import (
            StrategyProposalsStore,
        )
        table = _make_table()
        uid, oid = _make_user(table, role_name="viewer")

        strat = StrategyStore(table).create(
            org_id=oid, author_user_id="someone-else",
            name="S", markdown="# m",
        )
        proposals = StrategyProposalsStore(table)
        p = proposals.create(
            strategy_id=strat.strategy_id, org_id=oid,
            proposer_agent="self_critique", proposer_agent_id="sc",
            rationale="r", proposed_markdown="# m2",
        )

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post(
            f"/api/strategies/{strat.strategy_id}"
            f"/proposals/{p.proposal_id}/apply",
        )
        assert resp.status_code == 403


# ── /api/orgs/{org_id}/advisories ─────────────────────────────────────


def test_advisories_merges_cross_agent_output() -> None:
    """Risk + Compliance + Auditor all write to RecommendationsStore;
    the endpoint returns them merged, newest-first. Org Advisories
    is the single panel an orgadmin checks for advisories across all
    three review agents."""

    with mock_aws():
        from trading_strands.recommendations_store.store import (
            RecommendationsStore,
        )
        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")

        store = RecommendationsStore(table)
        store.append(
            org_id=oid, agent_type="risk", agent_id="r",
            severity="info", summary="concentration ok",
            created_at=1_700_000_100,
        )
        store.append(
            org_id=oid, agent_type="compliance", agent_id="c",
            severity="warn", summary="mandate drift",
            created_at=1_700_000_200,
        )
        store.append(
            org_id=oid, agent_type="auditor", agent_id="a",
            severity="critical", summary="[HALT] position drift",
            created_at=1_700_000_300,
        )

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get(f"/api/orgs/{oid}/advisories")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 3
        # Newest first — auditor (300) > compliance (200) > risk (100).
        assert body[0]["agent_type"] == "auditor"
        assert body[0]["severity"] == "critical"
        assert body[2]["agent_type"] == "risk"


def test_advisories_is_org_scoped() -> None:
    """A user in org A must not see org B's advisories."""

    with mock_aws():
        from trading_strands.authz.model import Role
        from trading_strands.recommendations_store.store import (
            RecommendationsStore,
        )
        from trading_strands.tenancy.store import TenancyStore

        table = _make_table()
        tenancy = TenancyStore(table)
        alice = tenancy.create_user(email="alice@x.com")
        org_a = tenancy.create_org("A")
        org_b = tenancy.create_org("B")
        tenancy.add_membership(alice.user_id, org_a.org_id, Role.ORGADMIN)

        rstore = RecommendationsStore(table)
        rstore.append(
            org_id=org_a.org_id, agent_type="risk", agent_id="r",
            severity="info", summary="for A", created_at=1,
        )
        rstore.append(
            org_id=org_b.org_id, agent_type="risk", agent_id="r",
            severity="info", summary="for B", created_at=2,
        )

        from trading_strands.dashboard.api import app
        client = TestClient(
            app, cookies=_session_cookie(alice.user_id, org_a.org_id),
        )
        # Can see own org.
        resp = client.get(f"/api/orgs/{org_a.org_id}/advisories")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        # Cannot see another org's advisories (403 on authz).
        resp = client.get(f"/api/orgs/{org_b.org_id}/advisories")
        assert resp.status_code == 403


# ── /api/metrics/query ─────────────────────────────────────────────────


def test_metrics_query_returns_datapoints() -> None:
    """Happy path: allowlisted metric + valid stat returns ts/value list."""

    import datetime

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        cw_mock = MagicMock()
        now = datetime.datetime(2026, 4, 26, 12, 0, 0)
        cw_mock.get_metric_data.return_value = {
            "MetricDataResults": [{
                "Id": "m1",
                "Label": "agent.decision.latency_ms",
                "Timestamps": [
                    now, now + datetime.timedelta(minutes=1),
                ],
                "Values": [150.0, 175.0],
            }],
        }
        with patch(
            "trading_strands.dashboard.api._get_cloudwatch_client",
            return_value=cw_mock,
        ):
            from trading_strands.dashboard.api import app
            client = TestClient(app, cookies=_session_cookie(uid, oid))
            resp = client.post("/api/metrics/query", json={
                "metric_name": "agent.decision.latency_ms",
                "dimensions": {"agent_type": "strategy"},
                "stat": "Average",
                "period_seconds": 60,
                "lookback_seconds": 3600,
            })

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["datapoints"]) == 2
        assert body["datapoints"][0]["value"] == 150.0


def test_metrics_query_rejects_non_allowlisted_metric() -> None:
    """Block arbitrary metric queries — the endpoint is a narrow proxy,
    not a general CloudWatch client."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post("/api/metrics/query", json={
            "metric_name": "AWS/EC2.CPUUtilization",
        })
        assert resp.status_code == 400
        assert "not allowlisted" in resp.json()["detail"]


def test_metrics_query_rejects_bad_stat() -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.post("/api/metrics/query", json={
            "metric_name": "agent.decision.count",
            "stat": "NotARealStat",
        })
        assert resp.status_code == 400


def test_metrics_query_cw_failure_returns_503() -> None:
    """CloudWatch call failing is 503, not 500 — consistent with the
    alarms endpoint, signals 'try again later' to the client."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        cw_mock = MagicMock()
        cw_mock.get_metric_data.side_effect = RuntimeError("boom")
        with patch(
            "trading_strands.dashboard.api._get_cloudwatch_client",
            return_value=cw_mock,
        ):
            from trading_strands.dashboard.api import app
            client = TestClient(app, cookies=_session_cookie(uid, oid))
            resp = client.post("/api/metrics/query", json={
                "metric_name": "agent.decision.count",
            })
        assert resp.status_code == 503


def test_metrics_query_requires_auth() -> None:
    from trading_strands.dashboard.api import app
    client = TestClient(app)
    resp = client.post("/api/metrics/query", json={
        "metric_name": "agent.decision.count",
    })
    assert resp.status_code in (401, 403)


# ── /api/deploys/recent ────────────────────────────────────────────────


def test_deploys_recent_returns_marker_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With DEPLOY_COMMIT + DEPLOY_TIMESTAMP set at container start,
    the endpoint returns one marker. The UI overlays it on metric
    charts so a regression lines up with the causing deploy."""

    import time as _time

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        monkeypatch.setenv("DEPLOY_COMMIT", "abc123def456" * 3)
        monkeypatch.setenv("DEPLOY_TIMESTAMP", str(int(_time.time())))

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/deploys/recent")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["deploys"]) == 1
        assert body["deploys"][0]["commit"] == "abc123d"


def test_deploys_recent_empty_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        monkeypatch.delenv("DEPLOY_COMMIT", raising=False)
        monkeypatch.delenv("DEPLOY_TIMESTAMP", raising=False)

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/deploys/recent")
        assert resp.status_code == 200
        assert resp.json() == {"deploys": []}


def test_deploys_recent_rejects_garbage_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed DEPLOY_TIMESTAMP must not 500 — render as 'no
    markers' and move on. CI misconfiguration shouldn't break the
    dashboard panel."""

    with mock_aws():
        table = _make_table()
        uid, oid = _make_user(table)

        monkeypatch.setenv("DEPLOY_COMMIT", "abc")
        monkeypatch.setenv("DEPLOY_TIMESTAMP", "not-a-number")

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.get("/api/deploys/recent")
        assert resp.status_code == 200
        assert resp.json()["deploys"] == []


def test_update_strategy_can_modify_tools_and_skills() -> None:
    with mock_aws():
        from trading_strands.strategies_store.store import StrategyStore

        table = _make_table()
        uid, oid = _make_user(table, role_name="orgadmin")

        store = StrategyStore(table)
        strat = store.create(
            org_id=oid, author_user_id=uid,
            name="n", markdown="# m",
        )

        from trading_strands.dashboard.api import app
        client = TestClient(app, cookies=_session_cookie(uid, oid))
        resp = client.put(f"/api/strategies/{strat.strategy_id}", json={
            "tools": {"news": {"enabled": True, "daily_quota": 10}},
            "skills": ["greeks", "morning_prep"],
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["tools"]["news"]["enabled"] is True
        assert body["skills"] == ["greeks", "morning_prep"]
