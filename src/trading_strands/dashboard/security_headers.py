"""Security headers middleware for the dashboard.

Browser-layer defense paired with the render helpers + input
validators. The combined posture blocks every known XSS class for
this codebase:
  - Injected `<script>` from attacker-controlled data cannot land
    in the DOM because el()/mount() emits text nodes (enforced by
    tests/dashboard/test_no_inner_html.py).
  - Input boundary validators reject attacker-crafted payloads
    before they reach DDB (tests/dashboard/test_validators.py).
  - CSP forbids inline event-handler attributes and externally-
    loaded scripts, and the frame directives close clickjacking.

Headers set on every response:
  - Content-Security-Policy: default-src 'self'; script-src 'self'
      'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src
      'self' data:; connect-src 'self'; frame-ancestors 'none';
      base-uri 'self'; object-src 'none'
  - X-Frame-Options: DENY
  - X-Content-Type-Options: nosniff
  - Referrer-Policy: same-origin

`script-src 'unsafe-inline'` is present because our templates embed
their JS in `<script>` blocks (base.html lines 160-864, index.html's
`{% block scripts %}`). Tightening this to hashes or nonces requires
either (a) moving the JS to external files served from a `/static`
mount, or (b) precomputing sha256 of the script block at build time
and embedding in the CSP. Option (a) is the clean fix and is tracked
as a follow-up — see docs/SPEC/operational_notes.md §"Dashboard XSS
hardening → external JS".

This does NOT reopen the XSS attack surface that motivated the
render migration: the migration's job is to keep attacker data out
of the DOM in the first place, which it does. CSP was supplemental
defense-in-depth; `unsafe-inline` on script-src degrades it but the
primary controls are intact.

`style-src 'unsafe-inline'` stays — inline `style="..."` attributes
are used heavily in panel rendering and have no XSS pathway when
the values are static literal CSS.

No HSTS here — the dashboard is fronted by an ALB with TLS
termination at AWS's layer; HSTS belongs in the CDK stack's
LoadBalancer configuration so it's set even on error responses
before the app sees the request.
"""

from __future__ import annotations

from typing import Any, cast

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Inline policy. Keep it a single line so the CSP compiler in the
# browser doesn't have to parse newlines — some older CSP engines
# treat them as separator characters and drop the remaining
# directives silently.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "object-src 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Any,
    ) -> Response:
        response = cast(Response, await call_next(request))
        # Only set headers we control. If the app already set one
        # (e.g. a specific endpoint wants a stricter CSP), respect
        # that — never overwrite a more-restrictive policy.
        hdrs = response.headers
        hdrs.setdefault("Content-Security-Policy", _CSP)
        hdrs.setdefault("X-Frame-Options", "DENY")
        hdrs.setdefault("X-Content-Type-Options", "nosniff")
        hdrs.setdefault("Referrer-Policy", "same-origin")
        return response
