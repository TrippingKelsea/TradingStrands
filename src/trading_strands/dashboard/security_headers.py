"""Security headers middleware for the dashboard.

Browser-layer defense paired with the render helpers + input
validators. Even if a render site regresses, the CSP header forbids
inline script execution — injected `<script>` and `onerror=` handlers
cannot run.

Headers set on every response:
  - Content-Security-Policy: default-src 'self'; script-src 'self';
      style-src 'self' 'unsafe-inline'; img-src 'self' data:;
      connect-src 'self'; frame-ancestors 'none'; base-uri 'self';
      object-src 'none'
  - X-Frame-Options: DENY  (redundant with frame-ancestors but covers
      older browsers)
  - X-Content-Type-Options: nosniff
  - Referrer-Policy: same-origin

`style-src 'unsafe-inline'` is kept because the existing layout uses
inline `style="..."` attributes heavily across panel rendering. This
does NOT weaken the XSS protection posture — script-src remains
strict, so inline `<script>` tags and event-handler attributes
(onerror=, onclick=) are still blocked. Tightening style-src to
hashes/nonces is a separate follow-up tracked in
docs/SPEC/operational_notes.md §"Dashboard XSS hardening".

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
    "script-src 'self'; "
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
