"""Authentication — Cognito login, session cookies, and request gating.

Responsibilities:
  - Validate email/password against Cognito.
  - Find or create the corresponding `USER#` record in DynamoDB so every
    authenticated user has a first-class identity (survives Cognito rebuilds).
  - Issue a signed session cookie carrying `user_id` + `active_org_id`.
  - Enforce 'you must be logged in' at the middleware boundary. Per-endpoint
    authorization (viewer vs operator vs orgadmin etc.) is handled by the
    authz module via the Principal on request.state — this file only checks
    authentication, not authorization.

Session cookie shape (v2 — introduced with the privacy refactor):
    {
        "user_id":        <our USER# id>,            # REQUIRED; v1 sessions lack this
        "active_org_id":  <org_id or None>,           # scope of current requests
        "email":          <cognito email>,            # cached for display
        "access_token":   <cognito access token>,     # kept for lower-level calls
        "login_at":       <unix ts>,
    }

v1 sessions (those without `user_id`) are invalidated on load — users
must re-login. Acceptable because v1 was a pre-alpha shape.
"""

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
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from trading_strands.tenancy.store import TenancyStore

logger = structlog.get_logger()

# Session cookie name
SESSION_COOKIE = "session"

# Default session max age: 1 year for dev (controlled per-org via ORG item)
SESSION_MAX_AGE_DEFAULT = 86400 * 365  # 1 year

# Signed URL token max age — short-lived, just survives one redirect
URL_TOKEN_MAX_AGE = 60  # 60 seconds

# Paths that don't require authentication
PUBLIC_PATHS = frozenset({"/health", "/login", "/auth/login", "/auth/logout"})

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


# ── Signed URL tokens ─────────────────────────────────────────────────


def create_url_token(data: dict[str, Any]) -> str:
    """Create a signed, time-limited token for URL parameters.

    Uses a separate salt from session cookies so tokens are not
    interchangeable. Expires after URL_TOKEN_MAX_AGE seconds.
    """
    serializer = _get_serializer()
    return serializer.dumps(data, salt="url-token")


def decode_url_token(token: str) -> dict[str, Any] | None:
    """Decode a signed URL token. Returns None if expired or tampered."""
    serializer = _get_serializer()
    try:
        data = serializer.loads(token, salt="url-token", max_age=URL_TOKEN_MAX_AGE)
        return dict(data)
    except (BadSignature, SignatureExpired):
        return None


# ── Authentication ─────────────────────────────────────────────────────


def _cognito_login(email: str, password: str) -> dict[str, Any] | None:
    """Raw Cognito auth. Returns the access token + email, or None on failure.

    Pulled out of `authenticate()` so the tenancy-store lookup can be tested
    independently of Cognito.
    """

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

    try:
        user_resp = cognito.get_user(AccessToken=access_token)
        attrs = {a["Name"]: a["Value"] for a in user_resp.get("UserAttributes", [])}
        cognito_sub = attrs.get("sub") or user_resp.get("Username", "")
    except Exception:
        logger.exception("auth.get_user_failed")
        attrs = {}
        cognito_sub = ""

    return {
        "email": attrs.get("email", email),
        "cognito_sub": cognito_sub,
        "access_token": access_token,
    }


def authenticate(
    email: str, password: str, tenancy: TenancyStore,
) -> dict[str, Any] | None:
    """Full login: Cognito verify + find-or-create local USER# + build session dict.

    The `tenancy` argument is injected so tests can pass a store backed by
    moto without having to mock DynamoDB globally. On a legitimate successful
    login we guarantee a `USER#` record exists for this email; the caller
    then calls `create_session_cookie(user_info)` with the returned dict.
    """

    cognito_result = _cognito_login(email, password)
    if cognito_result is None:
        return None

    normalized_email = cognito_result["email"]

    existing = tenancy.find_user_by_email(normalized_email)
    if existing is not None:
        user = existing
    else:
        # Cognito said yes but we have no local record. Provision one.
        user = tenancy.create_user(
            email=normalized_email,
            cognito_sub=cognito_result["cognito_sub"] or None,
        )
        logger.info(
            "auth.user_provisioned", email=normalized_email, user_id=user.user_id,
        )

    # Resolve active org: prefer last-active (if still valid), else
    # auto-select if user has exactly one membership, else None.
    memberships = tenancy.memberships_for_user(user.user_id)
    member_org_ids = {m.org_id for m in memberships}
    active_org_id: str | None
    if user.last_active_org_id and user.last_active_org_id in member_org_ids:
        active_org_id = user.last_active_org_id
    elif len(memberships) == 1:
        active_org_id = memberships[0].org_id
    else:
        active_org_id = None

    return {
        "user_id": user.user_id,
        "email": normalized_email,
        "active_org_id": active_org_id,
        "access_token": cognito_result["access_token"],
        "login_at": int(time.time()),
    }


def create_session_cookie(user_info: dict[str, Any]) -> str:
    """Sign and return the session cookie value for the given session dict.

    Accepts any subset of the session fields plus a required `user_id` —
    callers who want to update a single field (e.g., switch active org)
    can pass an updated dict without re-running authenticate().
    """

    if not user_info.get("user_id"):
        msg = "create_session_cookie requires user_id"
        raise ValueError(msg)
    serializer = _get_serializer()
    return serializer.dumps({
        "user_id": user_info["user_id"],
        "email": user_info.get("email", ""),
        "active_org_id": user_info.get("active_org_id"),
        "access_token": user_info.get("access_token", ""),
        "login_at": user_info.get("login_at", int(time.time())),
    }, salt="session")


def validate_session(
    cookie_value: str,
    max_age: int = SESSION_MAX_AGE_DEFAULT,
) -> dict[str, Any] | None:
    """Decode + verify a session cookie. Returns the session dict, or None
    if the signature is bad, the cookie is expired, or the cookie is in
    the pre-refactor v1 shape (no `user_id`)."""

    serializer = _get_serializer()
    try:
        data = serializer.loads(cookie_value, salt="session", max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None

    if not isinstance(data, dict) or not data.get("user_id"):
        # v1 cookie or tampering — force re-login.
        return None
    return dict(data)


class AuthMiddleware(BaseHTTPMiddleware):
    """Authenticate only. Authorization is done per-endpoint via authz.can().

    On a valid session cookie we attach the decoded session dict to
    `request.state.session`. Endpoint handlers then call a dependency that
    builds a Principal from that session + a fresh DynamoDB read and passes
    the result to authz. Role-string checks at the middleware level were
    removed — they were coarse, wrong in places, and violated deny-by-default.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        path = request.url.path

        if path in PUBLIC_PATHS:
            return await call_next(request)

        session_cookie = request.cookies.get(SESSION_COOKIE)
        if not session_cookie:
            return self._unauthorized(request)

        session = validate_session(session_cookie)
        if session is None:
            return self._unauthorized(request)

        request.state.session = session
        return await call_next(request)

    def _unauthorized(self, request: Request) -> Response:
        """Return 401 for API requests, redirect to login for browser requests."""
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
            )
        return RedirectResponse(url="/login", status_code=307)
