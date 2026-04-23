"""Tests for the authentication module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def _mock_boto3():
    with patch("trading_strands.dashboard.api.boto3") as mock:
        table = MagicMock()
        table.get_item.return_value = {}
        table.scan.return_value = {"Items": []}
        mock.resource.return_value.Table.return_value = table
        yield mock


@pytest.fixture()
def _mock_cognito():
    with patch("trading_strands.dashboard.auth.boto3") as mock:
        yield mock


@pytest.fixture()
def _mock_auth_config():
    with patch.dict("os.environ", {
        "COGNITO_USER_POOL_ID": "us-west-2_testpool",
        "COGNITO_CLIENT_ID": "testclientid",
        "COGNITO_CLIENT_SECRET": "testclientsecret",
    }):
        yield


# ── Public routes don't require auth ──────────────────────────────────


def test_health_no_auth_required(_mock_boto3: None, _mock_auth_config: None) -> None:
    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_login_page_no_auth_required(
    _mock_boto3: None, _mock_auth_config: None,
) -> None:
    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ── Protected routes redirect to login ────────────────────────────────


def test_dashboard_redirects_without_auth(
    _mock_boto3: None, _mock_auth_config: None,
) -> None:
    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert "/login" in resp.headers["location"]


def test_api_returns_401_without_auth(
    _mock_boto3: None, _mock_auth_config: None,
) -> None:
    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.get("/api/snapshot")
    assert resp.status_code == 401


def test_sse_returns_401_without_auth(
    _mock_boto3: None, _mock_auth_config: None,
) -> None:
    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.get("/api/stream")
    assert resp.status_code == 401


# ── Login flow ────────────────────────────────────────────────────────


def test_login_success(
    _mock_boto3: None, _mock_cognito: None, _mock_auth_config: None,
) -> None:
    import trading_strands.dashboard.auth as auth_mod

    auth_mod._cognito_client = _mock_cognito.client.return_value
    cognito = _mock_cognito.client.return_value
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {
            "IdToken": "fake.id.token",
            "AccessToken": "fake.access.token",
            "RefreshToken": "fake.refresh.token",
        },
    }
    cognito.get_user.return_value = {
        "Username": "testuser",
        "UserAttributes": [
            {"Name": "email", "Value": "test@example.com"},
            {"Name": "custom:role", "Value": "operator"},
        ],
    }

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.post("/auth/login", data={
        "email": "test@example.com",
        "password": "TestPass123!",
    }, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "session" in resp.cookies


def test_login_failure(
    _mock_boto3: None, _mock_cognito: None, _mock_auth_config: None,
) -> None:
    import trading_strands.dashboard.auth as auth_mod

    auth_mod._cognito_client = _mock_cognito.client.return_value
    cognito = _mock_cognito.client.return_value
    cognito.initiate_auth.side_effect = Exception("NotAuthorizedException")

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.post("/auth/login", data={
        "email": "test@example.com",
        "password": "wrong",
    }, follow_redirects=False)

    assert resp.status_code == 303
    assert "error" in resp.headers["location"]


def test_logout_clears_session(
    _mock_boto3: None, _mock_auth_config: None,
) -> None:
    from trading_strands.dashboard.api import app

    client = TestClient(app)
    resp = client.post("/auth/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    # Session cookie should be deleted (set to empty or max-age=0)
    assert "session" in resp.headers.get("set-cookie", "")


# ── Role-based access ────────────────────────────────────────────────


def test_viewer_cannot_halt(
    _mock_boto3: None, _mock_cognito: None, _mock_auth_config: None,
) -> None:
    """Viewer role should be rejected from write endpoints."""
    import trading_strands.dashboard.auth as auth_mod

    auth_mod._cognito_client = _mock_cognito.client.return_value
    cognito = _mock_cognito.client.return_value

    # Login as viewer
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {
            "IdToken": "fake.id.token",
            "AccessToken": "viewer.access.token",
            "RefreshToken": "fake.refresh.token",
        },
    }
    cognito.get_user.return_value = {
        "Username": "viewer",
        "UserAttributes": [
            {"Name": "email", "Value": "viewer@example.com"},
            {"Name": "custom:role", "Value": "viewer"},
        ],
    }

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    # Login
    login_resp = client.post("/auth/login", data={
        "email": "viewer@example.com",
        "password": "ViewerPass123!",
    }, follow_redirects=False)
    session_cookie = login_resp.cookies.get("session")

    # Try to halt — should be forbidden
    client.cookies.set("session", session_cookie)
    resp = client.post("/api/halt")
    assert resp.status_code == 403


def test_viewer_can_read_snapshot(
    _mock_boto3: None, _mock_cognito: None, _mock_auth_config: None,
) -> None:
    """Viewer role should be able to read endpoints."""
    import trading_strands.dashboard.auth as auth_mod

    auth_mod._cognito_client = _mock_cognito.client.return_value
    cognito = _mock_cognito.client.return_value

    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {
            "IdToken": "fake.id.token",
            "AccessToken": "viewer.access.token",
            "RefreshToken": "fake.refresh.token",
        },
    }
    cognito.get_user.return_value = {
        "Username": "viewer",
        "UserAttributes": [
            {"Name": "email", "Value": "viewer@example.com"},
            {"Name": "custom:role", "Value": "viewer"},
        ],
    }

    from trading_strands.dashboard.api import app

    client = TestClient(app)
    login_resp = client.post("/auth/login", data={
        "email": "viewer@example.com",
        "password": "ViewerPass123!",
    }, follow_redirects=False)
    session_cookie = login_resp.cookies.get("session")

    client.cookies.set("session", session_cookie)
    resp = client.get("/api/snapshot")
    assert resp.status_code == 200
