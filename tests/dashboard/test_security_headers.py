"""Tests for the security-headers middleware.

Every response the dashboard emits must carry CSP + companion headers
regardless of whether the request was authenticated, redirected, or
errored. This test locks that in so a future refactor can't
silently drop headers.
"""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

# Same env bootstrapping the main dashboard test file does so
# importing dashboard.api doesn't crash on missing secrets.
os.environ.setdefault("COGNITO_USER_POOL_ID", "us-west-2_test")
os.environ.setdefault("COGNITO_CLIENT_ID", "testclient")
os.environ.setdefault("COGNITO_CLIENT_SECRET", "testsecret")
os.environ.setdefault("DYNAMODB_TABLE", "trading-strands-state")
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

REQUIRED_HEADERS = (
    "content-security-policy",
    "x-frame-options",
    "x-content-type-options",
    "referrer-policy",
)


def _client() -> TestClient:
    # follow_redirects=False so auth middleware's 303 → /login response
    # is the one we're inspecting, not the /login page.
    from trading_strands.dashboard.api import app
    return TestClient(app, follow_redirects=False)


def test_health_endpoint_has_all_headers() -> None:
    """/health is the simplest baseline — unauthenticated, always 200."""

    resp = _client().get("/health")
    assert resp.status_code == 200
    for h in REQUIRED_HEADERS:
        assert h in {k.lower() for k in resp.headers}, f"missing {h}"


def test_csp_restricts_script_sources_to_self() -> None:
    """script-src must include 'self' so externally-loaded scripts
    are blocked. 'unsafe-inline' is allowed for now because the
    dashboard's JS lives in inline <script> blocks in the templates;
    moving those to external files is the tracked follow-up. The
    key invariant this test guards is: no third-party script
    origins, no `*`, no data:-URLs."""

    resp = _client().get("/health")
    csp = resp.headers.get("content-security-policy", "")
    src = _script_src(csp)
    assert src, "script-src directive missing"
    assert "'self'" in src
    # Reject any form of wildcard or 'unsafe-eval' — those would
    # re-open the exec pathway even with inline allowed.
    assert "*" not in src.replace("'self'", "").replace(" ", "")
    assert "'unsafe-eval'" not in src
    # External origins (http(s)://…) are forbidden.
    assert "http" not in src


def _script_src(csp: str) -> str:
    """Pull out just the script-src directive string for assertion."""

    parts = [p.strip() for p in csp.split(";")]
    for p in parts:
        if p.startswith("script-src"):
            return p
    return ""


def test_csp_blocks_framing() -> None:
    """frame-ancestors 'none' + X-Frame-Options DENY — every browser
    refuses to put the dashboard in an iframe, closing clickjacking."""

    resp = _client().get("/health")
    csp = resp.headers.get("content-security-policy", "")
    assert "frame-ancestors 'none'" in csp
    assert resp.headers.get("x-frame-options") == "DENY"


def test_content_type_options_nosniff() -> None:
    resp = _client().get("/health")
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_referrer_policy_same_origin() -> None:
    """Browser must not send the dashboard URL (which carries org +
    strategy ids on some paths) as Referer on cross-origin navigation."""

    resp = _client().get("/health")
    assert resp.headers.get("referrer-policy") == "same-origin"


def test_headers_present_on_redirect() -> None:
    """Auth middleware redirects unauthenticated requests to /login.
    The redirect response itself must still carry the headers — an
    attacker-crafted URL that triggers a redirect mustn't be the
    escape hatch from clickjacking protection."""

    # Any protected path that would 303 to /login.
    resp = _client().get("/")
    assert resp.status_code in (200, 303, 307)
    for h in REQUIRED_HEADERS:
        assert h in {k.lower() for k in resp.headers}, (
            f"missing {h} on redirect (status {resp.status_code})"
        )


def test_headers_present_on_login_page() -> None:
    resp = _client().get("/login")
    assert resp.status_code == 200
    for h in REQUIRED_HEADERS:
        assert h in {k.lower() for k in resp.headers}
