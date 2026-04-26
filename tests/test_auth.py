"""Tests for authentication, login flow, and v1-session invalidation.

Uses moto for the DDB layer (same as dashboard tests) so the end-to-end
login → tenancy provisioning → session cookie path exercises real code.
Cognito is mocked via a patch on `trading_strands.dashboard.auth.boto3`.
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

os.environ.setdefault("COGNITO_USER_POOL_ID", "us-west-2_testpool")
os.environ.setdefault("COGNITO_CLIENT_ID", "testclientid")
os.environ.setdefault("COGNITO_CLIENT_SECRET", "testclientsecret")
os.environ.setdefault("DYNAMODB_TABLE", "trading-strands-state")
os.environ.setdefault("SESSION_SECRET", "test-secret")
# moto fixtures need a default region for boto3 to resolve.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture(autouse=True)
def _reset_clients() -> Iterator[None]:
    """Reset lazy module singletons between tests."""

    import trading_strands.dashboard.auth as auth_mod
    auth_mod._cognito_client = None
    auth_mod._serializer = None
    yield
    auth_mod._cognito_client = None
    auth_mod._serializer = None


def _create_table() -> Any:
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ddb.create_table(
        TableName="trading-strands-state",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table("trading-strands-state")


# ── Public routes ─────────────────────────────────────────────────────


def test_health_no_auth_required() -> None:
    with mock_aws():
        _create_table()
        from trading_strands.dashboard.api import app

        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200


def test_login_page_no_auth_required() -> None:
    with mock_aws():
        _create_table()
        from trading_strands.dashboard.api import app

        client = TestClient(app)
        resp = client.get("/login", follow_redirects=False)
        assert resp.status_code == 200


# ── Unauthenticated access ────────────────────────────────────────────


def test_dashboard_redirects_without_auth() -> None:
    with mock_aws():
        _create_table()
        from trading_strands.dashboard.api import app

        client = TestClient(app)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert "/login" in resp.headers["location"]


def test_api_returns_401_without_auth() -> None:
    with mock_aws():
        _create_table()
        from trading_strands.dashboard.api import app

        client = TestClient(app)
        resp = client.get("/api/snapshot")
        assert resp.status_code == 401


def test_sse_returns_401_without_auth() -> None:
    with mock_aws():
        _create_table()
        from trading_strands.dashboard.api import app

        client = TestClient(app)
        resp = client.get("/api/stream")
        assert resp.status_code == 401


# ── Login flow ────────────────────────────────────────────────────────


def _mock_cognito_success(email: str = "test@example.com") -> MagicMock:
    """Produce a Cognito client mock that returns a successful auth."""

    cognito = MagicMock()
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {
            "IdToken": "fake.id.token",
            "AccessToken": "fake.access.token",
            "RefreshToken": "fake.refresh.token",
        },
    }
    cognito.get_user.return_value = {
        "Username": f"{email}-sub",
        "UserAttributes": [
            {"Name": "email", "Value": email},
            {"Name": "sub", "Value": f"{email}-sub"},
        ],
    }
    return cognito


def test_login_success_creates_user_and_sets_session() -> None:
    cognito = _mock_cognito_success("test@example.com")

    with mock_aws(), patch(
        "trading_strands.dashboard.auth.boto3"
    ) as mock_auth_boto3:
        _create_table()
        mock_auth_boto3.client.return_value = cognito

        from trading_strands.dashboard.api import app
        from trading_strands.tenancy.store import TenancyStore

        client = TestClient(app)
        resp = client.post(
            "/auth/login",
            data={"email": "test@example.com", "password": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        assert "session" in resp.cookies

        # USER# record should exist in DynamoDB.
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        store = TenancyStore(ddb.Table("trading-strands-state"))
        user = store.find_user_by_email("test@example.com")
        assert user is not None


def test_login_idempotent_for_existing_user() -> None:
    """Second login should not create a second USER# record."""

    cognito = _mock_cognito_success("test@example.com")

    with mock_aws(), patch(
        "trading_strands.dashboard.auth.boto3"
    ) as mock_auth_boto3:
        _create_table()
        mock_auth_boto3.client.return_value = cognito

        from trading_strands.dashboard.api import app
        from trading_strands.tenancy.store import TenancyStore

        client = TestClient(app)
        client.post(
            "/auth/login",
            data={"email": "test@example.com", "password": "x"},
            follow_redirects=False,
        )
        # Drop cookies so the second call goes through login again.
        client2 = TestClient(app)
        client2.post(
            "/auth/login",
            data={"email": "test@example.com", "password": "x"},
            follow_redirects=False,
        )

        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        store = TenancyStore(ddb.Table("trading-strands-state"))
        users = [u for u in store.list_users() if u.email == "test@example.com"]
        assert len(users) == 1


def test_login_failure_redirects_with_signed_error_token() -> None:
    cognito = MagicMock()
    cognito.initiate_auth.side_effect = Exception("NotAuthorizedException")

    with mock_aws(), patch(
        "trading_strands.dashboard.auth.boto3"
    ) as mock_auth_boto3:
        _create_table()
        mock_auth_boto3.client.return_value = cognito

        from trading_strands.dashboard.api import app
        from trading_strands.dashboard.auth import decode_url_token

        client = TestClient(app)
        resp = client.post(
            "/auth/login",
            data={"email": "test@example.com", "password": "wrong"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "?t=" in location
        token = location.split("?t=")[1]
        decoded = decode_url_token(token)
        assert decoded is not None
        assert decoded["error"] == "Invalid email or password"


def test_logout_clears_session() -> None:
    with mock_aws():
        _create_table()
        from trading_strands.dashboard.api import app

        client = TestClient(app)
        resp = client.post("/auth/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
        assert "session" in resp.headers.get("set-cookie", "")


# ── Authenticated access ─────────────────────────────────────────────


def test_login_then_read_snapshot() -> None:
    """End-to-end: login, then the resulting cookie works on /api/snapshot."""

    cognito = _mock_cognito_success("viewer@example.com")

    with mock_aws(), patch(
        "trading_strands.dashboard.auth.boto3"
    ) as mock_auth_boto3:
        _create_table()
        mock_auth_boto3.client.return_value = cognito

        from trading_strands.dashboard.api import app

        client = TestClient(app)
        login_resp = client.post(
            "/auth/login",
            data={"email": "viewer@example.com", "password": "x"},
            follow_redirects=False,
        )
        session = login_resp.cookies.get("session")
        assert session is not None

        client.cookies.set("session", session)
        resp = client.get("/api/snapshot")
        assert resp.status_code == 200
