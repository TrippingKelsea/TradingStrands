"""Authentication module — Cognito-backed auth with session cookies."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

import boto3
import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

logger = structlog.get_logger()

# Session cookie settings
SESSION_COOKIE = "session"
SESSION_MAX_AGE = 86400 * 7  # 7 days

# Paths that don't require authentication
PUBLIC_PATHS = frozenset({"/health", "/login", "/auth/login", "/auth/logout"})

# Write operations that require the operator role
WRITE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})
READ_ONLY_API_PATHS = frozenset({
    "/api/snapshot", "/api/events", "/api/stream",
    "/api/strategies", "/api/telemetry",
})

# Module-level Cognito client (set during startup or mocked in tests)
_cognito_client: Any = None
_serializer: URLSafeTimedSerializer | None = None


def _get_cognito_client() -> Any:
    global _cognito_client
    if _cognito_client is None:
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
        _cognito_client = boto3.client("cognito-idp", region_name=region)
    return _cognito_client


def _get_serializer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        secret = os.environ.get("SESSION_SECRET", os.environ.get("COGNITO_CLIENT_SECRET", "dev"))
        _serializer = URLSafeTimedSerializer(secret)
    return _serializer


def _compute_secret_hash(username: str) -> str:
    """Compute Cognito SECRET_HASH for app clients with a client secret."""
    client_id = os.environ.get("COGNITO_CLIENT_ID", "")
    client_secret = os.environ.get("COGNITO_CLIENT_SECRET", "")
    if not client_secret:
        return ""
    msg = username + client_id
    dig = hmac.new(
        client_secret.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    import base64

    return base64.b64encode(dig).decode("utf-8")


def authenticate(email: str, password: str) -> dict[str, Any] | None:
    """Authenticate a user against Cognito and return user info + tokens."""
    client_id = os.environ.get("COGNITO_CLIENT_ID", "")
    cognito = _get_cognito_client()

    auth_params: dict[str, str] = {
        "USERNAME": email,
        "PASSWORD": password,
    }
    secret_hash = _compute_secret_hash(email)
    if secret_hash:
        auth_params["SECRET_HASH"] = secret_hash

    try:
        auth_result = cognito.initiate_auth(
            ClientId=client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters=auth_params,
        )
    except Exception:
        logger.exception("auth.login_failed", email=email)
        return None

    tokens = auth_result.get("AuthenticationResult", {})
    access_token = tokens.get("AccessToken", "")

    # Get user attributes
    try:
        user_resp = cognito.get_user(AccessToken=access_token)
        attrs = {a["Name"]: a["Value"] for a in user_resp.get("UserAttributes", [])}
    except Exception:
        logger.exception("auth.get_user_failed")
        attrs = {}

    return {
        "email": attrs.get("email", email),
        "role": attrs.get("custom:role", "viewer"),
        "access_token": access_token,
        "refresh_token": tokens.get("RefreshToken", ""),
        "login_at": int(time.time()),
    }


def create_session_cookie(user_info: dict[str, Any]) -> str:
    """Create a signed session cookie value."""
    serializer = _get_serializer()
    return serializer.dumps({
        "email": user_info["email"],
        "role": user_info["role"],
        "access_token": user_info["access_token"],
        "login_at": user_info["login_at"],
    })


def validate_session(cookie_value: str) -> dict[str, Any] | None:
    """Validate and decode a session cookie. Returns user info or None."""
    serializer = _get_serializer()
    try:
        data = serializer.loads(cookie_value, max_age=SESSION_MAX_AGE)
        return dict(data)
    except BadSignature:
        return None


def _is_read_only_request(request: Request) -> bool:
    """Check if this is a read-only request that a viewer can make."""
    if request.method == "GET":
        # GET on API strategy detail like /api/strategies/abc is also read-only
        path = request.url.path
        if path in READ_ONLY_API_PATHS:
            return True
        # /api/strategies/{id} is read-only for GET
        if path.startswith("/api/strategies/") and "/" not in path[17:]:
            return True
        # Root page and non-API GETs are read-only
        if not path.startswith("/api/") or path == "/":
            return True
    return False


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces authentication via session cookies."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        path = request.url.path

        # Skip auth for public paths
        if path in PUBLIC_PATHS:
            return await call_next(request)

        # Check for valid session
        session_cookie = request.cookies.get(SESSION_COOKIE)
        if not session_cookie:
            return self._unauthorized(request)

        user = validate_session(session_cookie)
        if user is None:
            return self._unauthorized(request)

        # Role-based access: viewers can only read
        if user.get("role") == "viewer" and not _is_read_only_request(request):
            return JSONResponse(
                status_code=403,
                content={"detail": "Viewer role cannot perform write operations"},
            )

        # Attach user info to request state for handlers to use
        request.state.user = user
        return await call_next(request)

    def _unauthorized(self, request: Request) -> Response:
        """Return 401 for API requests, redirect to login for browser requests."""
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
            )
        return RedirectResponse(url="/login", status_code=307)
