"""React PWA host: serves frontend/dist under /app, per-screen rollout flags, and a JSON bridge for form routes.

Rollout: ``UI_REACT_SCREENS`` lists the screens that have reached parity (search, viewer, review,
meetings, briefing, admin, or "all"). For those screens a browser navigation to the Jinja page is
redirected to /app; ``?classic=1`` always keeps the Jinja page. Unlisted screens stay on Jinja,
and /app still works for anyone who opens it directly.

Bridge: the existing workflow routes are HTML form posts that answer with a 303 redirect. When the
React client sends ``X-BCP-Client: spa`` the redirect is returned as JSON instead, so both UIs run
the same server code during the migration.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlsplit

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

ROOT = Path(__file__).resolve().parents[2]
SPA_HEADER = "x-bcp-client"
SCREENS = ("search", "viewer", "review", "meetings", "briefing", "admin")
FAILURE_SUFFIXES = ("_locked", "_four_eyes", "_input", "_input_required", "_failed", "_denied", "_invalid")

CLASSIC_ROUTES: List[Tuple[re.Pattern, str, str]] = [
    (re.compile(r"^/search/?$"), "search", "/app/search"),
    (re.compile(r"^/ask/?$"), "search", "/app/ask"),
    (re.compile(r"^/view/(?P<id>[^/]+)/?$"), "viewer", "/app/view/{id}"),
    (re.compile(r"^/review/?$"), "review", "/app/review"),
    (re.compile(r"^/review/(?P<id>[^/]+)/?$"), "review", "/app/review/{id}"),
    (re.compile(r"^/(board/)?meetings/?$"), "meetings", "/app/meetings"),
    (re.compile(r"^/meetings/(?P<id>\d+)/?$"), "meetings", "/app/meetings/{id}"),
    (re.compile(r"^/documents/(?P<id>[^/]+)/briefing/?$"), "briefing", "/app/briefing/{id}"),
    (re.compile(r"^/admin/(queues|ai)/?$"), "admin", "/app/admin"),
]


def dist_dir() -> Path:
    return Path(os.getenv("FRONTEND_DIST") or ROOT / "frontend" / "dist")


def enabled_screens() -> Set[str]:
    raw = (os.getenv("UI_REACT_SCREENS") or "").strip().lower()
    if raw in {"all", "*"}:
        return set(SCREENS)
    return {s.strip() for s in raw.split(",") if s.strip() in SCREENS}


def spa_built() -> bool:
    return (dist_dir() / "index.html").is_file()


def classic_redirect(request: Request) -> Optional[str]:
    """/app URL for a browser navigation to a Jinja page whose screen is switched on, else None."""
    if request.method != "GET" or request.query_params.get("classic") == "1":
        return None
    if "text/html" not in (request.headers.get("accept") or "") or request.headers.get(SPA_HEADER):
        return None
    screens = enabled_screens()
    if not screens:
        return None
    for pattern, screen, target in CLASSIC_ROUTES:
        match = pattern.match(request.url.path)
        if match and screen in screens and spa_built():
            url = target.format(**match.groupdict())
            query = request.url.query
            return f"{url}?{query}" if query else url
    return None


def bridge_redirect(response: Response) -> JSONResponse:
    """Turn a form route's 303 into ``{ok, status, error, redirect}`` for the React client."""
    location = response.headers.get("location") or ""
    parts = urlsplit(location)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    code = query.get("status") or ""
    error = query.get("error")
    failed = bool(error) or code.endswith(FAILURE_SUFFIXES)
    payload: Dict[str, object] = {"ok": not failed, "status": code or None, "error": error, "redirect": location}
    return JSONResponse(payload, status_code=409 if failed else 200)


async def spa_middleware(request: Request, call_next):
    target = classic_redirect(request)
    if target:
        if request.headers.get("x-requested-with") == "BCPNav":
            # The Jinja shell fetches pages with redirect: "manual"; tell it to leave for /app instead.
            return Response(status_code=204, headers={"X-BCP-Hard-Nav": target, "Cache-Control": "no-store"})
        return RedirectResponse(target, status_code=307)
    response = await call_next(request)
    if (
        request.method == "POST"
        and request.headers.get(SPA_HEADER) == "spa"
        and response.status_code in (302, 303)
        and not request.url.path.startswith(("/login", "/logout", "/auth/"))
    ):
        return bridge_redirect(response)
    return response


def _asset_headers(rel: str) -> Dict[str, str]:
    if rel.startswith("assets/"):
        return {"Cache-Control": "public, max-age=31536000, immutable"}
    return {"Cache-Control": "no-cache"}


def serve_spa(path: str) -> Response:
    """A built file under dist (hashed assets, sw.js, manifest, icons) or index.html for client routes."""
    root = dist_dir().resolve()
    if not (root / "index.html").is_file():
        return JSONResponse({"detail": "The React app is not built. Run: cd frontend && npm ci && npm run build"}, status_code=404)
    rel = (path or "").lstrip("/")
    if rel:
        candidate = (root / rel).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            headers = _asset_headers(rel)
            if rel == "sw.js":
                headers["Service-Worker-Allowed"] = "/app/"
            return FileResponse(candidate, headers=headers)
        if "." in rel.rsplit("/", 1)[-1]:
            return JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(root / "index.html", headers={"Cache-Control": "no-store"})


def safe_next(value: Optional[str]) -> Optional[str]:
    """Same-site path to return to after sign-in; rejects absolute and protocol-relative URLs."""
    value = (value or "").strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value or any(ord(c) < 32 for c in value):
        return None
    return value[:500]
