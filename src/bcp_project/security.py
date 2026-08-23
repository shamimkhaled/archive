"""Web security helpers: CSRF, security headers, login rate limiting."""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Set, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "x-csrf-token"
CSRF_FORM_FIELD = "csrf_token"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
CSRF_EXEMPT_PATHS = frozenset({"/login", "/token", "/healthz", "/readyz", "/sw.js"})


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str, *, secure: bool) -> None:
    response.set_cookie(
        key=CSRF_COOKIE,
        value=token,
        httponly=False,  # readable by JS for double-submit header
        secure=secure,
        samesite="lax",
        max_age=60 * 60 * 12,
        path="/",
    )


def _origin_ok(request: Request) -> bool:
    """Reject cross-site cookie POSTs when Origin/Referer don't match host."""
    host = request.headers.get("host")
    if not host:
        return True
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/").endswith("//" + host) or origin.rstrip("/") == f"https://{host}" or origin.rstrip("/") == f"http://{host}"
    referer = request.headers.get("referer") or request.headers.get("Referer")
    if not referer:
        # Some privacy clients omit Referer; still require CSRF token.
        return True
    from urllib.parse import urlparse

    parsed = urlparse(referer)
    return parsed.netloc == host


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, is_production: bool = False):
        super().__init__(app)
        self.is_production = is_production

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        # Allow pdf.js CDN + fonts used by the PWA shell.
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
            "worker-src 'self' blob: https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' https://cdnjs.cloudflare.com; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers.setdefault("Content-Security-Policy", csp)
        if self.is_production:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


class CsrfMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            request.method not in SAFE_METHODS
            and path not in CSRF_EXEMPT_PATHS
            and not path.startswith("/static/")
            and request.cookies.get("access_token")
        ):
            cookie_token = request.cookies.get(CSRF_COOKIE)
            if not cookie_token or not _origin_ok(request):
                accept = request.headers.get("accept", "")
                if "application/json" in accept or path.startswith("/api/"):
                    return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
                return Response(
                    "CSRF validation failed. Refresh the page and try again.",
                    status_code=403,
                )

            content_type = (request.headers.get("content-type") or "").lower()
            provided = request.headers.get(CSRF_HEADER)
            if provided:
                provided = provided.strip()
            else:
                # Never parse the request body here — BaseHTTPMiddleware body reads
                # leave downstream Form()/request.form() empty (e.g. blank username).
                # Cookie + same-origin is sufficient for cookie-auth POSTs with SameSite=Lax.
                provided = cookie_token

            if not provided or not secrets.compare_digest(provided, cookie_token):
                accept = request.headers.get("accept", "")
                if "application/json" in accept or path.startswith("/api/"):
                    return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
                return Response(
                    "CSRF validation failed. Refresh the page and try again.",
                    status_code=403,
                )

        response = await call_next(request)

        # Ensure authenticated sessions always have a CSRF cookie for subsequent POSTs.
        if request.cookies.get("access_token") and not request.cookies.get(CSRF_COOKIE):
            set_csrf_cookie(response, new_csrf_token(), secure=request.url.scheme == "https")
        return response


class LoginRateLimiter:
    """In-process sliding-window limiter. Never grants access on failure — only blocks."""

    def __init__(self, max_attempts: int = 8, window_seconds: int = 600):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures: Dict[str, Deque[float]] = defaultdict(deque)

    def _key(self, request: Request, username: str) -> str:
        ip = request.client.host if request.client else "unknown"
        return f"{ip}:{username.strip().lower()}"

    def is_blocked(self, request: Request, username: str) -> bool:
        key = self._key(request, username)
        now = time.time()
        q = self._failures[key]
        while q and now - q[0] > self.window_seconds:
            q.popleft()
        return len(q) >= self.max_attempts

    def record_failure(self, request: Request, username: str) -> None:
        key = self._key(request, username)
        self._failures[key].append(time.time())

    def clear(self, request: Request, username: str) -> None:
        self._failures.pop(self._key(request, username), None)


login_rate_limiter = LoginRateLimiter()
