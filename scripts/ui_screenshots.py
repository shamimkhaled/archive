#!/usr/bin/env python3
"""Screenshots of the main screens for each role, at phone, tablet and desktop widths, light and dark.

By default this starts the app on a throw-away SQLite database filled with demo
records, so no real document is ever opened. Sessions are made directly from
JWT_SECRET_KEY; nothing touches a real login.

    pip install playwright && playwright install chromium
    python scripts/ui_screenshots.py                    # demo data, all screens
    python scripts/ui_screenshots.py --only home,login  # a few screens
    python scripts/ui_screenshots.py --base-url http://localhost:8000   # a server you started
    python scripts/ui_screenshots.py --serve 8765       # keep the demo running, e.g. for Lighthouse

Images go to build/ui-screenshots/<screen>-<width>-<mode>.png. With --manifest,
the phone and desktop shots of the home screen are also written to
src/bcp_project/static/img/screenshots/ for the web app manifest.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
WIDTHS = {360: 780, 768: 1024, 1280: 860}
MODES = ("light", "dark")

# (name, role, path). Role None means signed out.
SCREENS: List[Tuple[str, Optional[str], str]] = [
    ("login", None, "/login"),
    ("home-member", "board_member", "/"),
    ("meetings-member", "board_member", "/board/meetings"),
    ("meeting-member", "board_member", "/board/meetings/1"),
    ("decisions", "board_member", "/board/decisions"),
    ("notifications", "board_member", "/notifications"),
    ("search", "board_member", "/search?q=budget"),
    ("home-uploader", "uploader", "/"),
    ("upload", "uploader", "/upload"),
    ("my-uploads", "uploader", "/uploads/mine"),
    ("home-secretary", "board_secretary", "/"),
    ("review-queue", "board_secretary", "/review"),
    ("meetings-manage", "board_secretary", "/meetings"),
    ("meeting-manage", "board_secretary", "/meetings/1"),
    ("access-requests", "board_secretary", "/admin/access-requests"),
    ("people", "admin", "/admin/users"),
    ("background-tasks", "admin", "/admin/queues"),
    ("ai-settings", "admin", "/admin/ai"),
    ("keep-and-delete", "admin", "/admin/retention"),
    ("appearance", "admin", "/appearance"),
]

USERS = {
    "board_member": "demo.member",
    "uploader": "demo.uploader",
    "board_secretary": "demo.secretary",
    "admin": "demo.admin",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _demo_env(db_path: Path) -> None:
    # The demo must never read .env: no real database, storage, cache or AI keys.
    import dotenv

    dotenv.load_dotenv = lambda *args, **kwargs: False
    for key in list(os.environ):
        if key.startswith(("OPENROUTER_", "OPENAI_", "ANTHROPIC_", "UPSTASH_", "AWS_", "QDRANT_", "SMTP_", "OIDC_", "LDAP_")):
            del os.environ[key]
    os.environ.update(
        {
            "APP_ENV": "test",
            "UPLOAD_DIR": str(db_path.parent / "uploads"),
            "STORAGE_BACKEND": "local",
            "AI_EGRESS": "internal_only",
            "AI_CHAT_PROVIDER": "none",
            "AI_EMBED_PROVIDER": "none",
            "QDRANT_URL": "http://127.0.0.1:1",
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
            "AUTO_MIGRATE": "0",
            "ENABLE_JOB_QUEUE": "0",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "MFA_ENFORCE": "0",
            "JWT_SECRET_KEY": os.environ.get("JWT_SECRET_KEY") or "screenshots-only-secret-key-not-for-production",
            "COOKIE_SECURE": "0",
            "OIDC_ENABLED": "0",
        }
    )
    sys.path.insert(0, str(ROOT / "src"))


async def _seed() -> None:
    from bcp_project.db import engine, get_session
    from bcp_project.models import (
        AccessMode,
        AccessRequestStatus,
        Base,
        BoardMeeting,
        DocumentAccessRequest,
        DocumentRecord,
        DocumentVersion,
        MeetingInvitation,
        MeetingStatus,
        NotificationEvent,
        Role,
        User,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime.now()
    async with get_session() as db:
        db.add_all([User(username=name, email=f"{name}@example.org", hashed_password="!", role=Role(role), is_active=True) for role, name in USERS.items()])
        db.add_all([User(username=f"director{i}", hashed_password="!", role=Role.board_member, is_active=True) for i in range(1, 6)])

        def doc(doc_id: str, title: str, status: str, **extra) -> DocumentRecord:
            return DocumentRecord(
                doc_id=doc_id,
                title=title,
                doc_date=date(2026, 3, 1),
                doc_type="Board Minutes",
                keywords=["budget"],
                file_location=f"pdfs/{doc_id}.pdf",
                uploaded_by="demo.uploader",
                ingest_status=status,
                created_at=now - timedelta(days=2),
                **extra,
            )

        db.add_all(
            [
                doc("SB-2026-001", "Budget for 2026–27", "indexed"),
                doc("SB-2026-002", "Audit committee memo", "needs_review", suggested_metadata={"_review_reasons": ["low_ocr_confidence"]}),
                doc("SB-2026-003", "Branch expansion plan", "failed", ingest_error="PdfReadError: EOF marker not found"),
            ]
        )
        db.add_all(
            [
                DocumentVersion(doc_id="SB-2026-001", version=1, sha256="a" * 64, file_location="pdfs/SB-2026-001.pdf", created_by="demo.uploader"),
                DocumentVersion(doc_id="SB-2026-002", version=1, sha256="b" * 64, file_location="pdfs/SB-2026-002.pdf", created_by="demo.uploader", status="pending_review"),
            ]
        )
        db.add(BoardMeeting(id=1, title="412th Board Meeting", scheduled_at=now + timedelta(days=2), location="Head office, 8th floor", created_by="demo.secretary"))
        db.add(BoardMeeting(id=2, title="411th Board Meeting", scheduled_at=now - timedelta(days=28), location="Head office", created_by="demo.secretary", status=MeetingStatus.completed))
        db.add_all([MeetingInvitation(meeting_id=m, username=u) for m in (1, 2) for u in ("demo.member", "director1", "director2")])
        db.add(NotificationEvent(username="demo.member", kind="invitation", title="Invitation to the 412th Board Meeting", body="Head office, 8th floor", meeting_id=1))
        db.add(
            DocumentAccessRequest(
                doc_id="SB-2026-002",
                requester_username="demo.member",
                purpose="Preparing for the audit discussion",
                requested_mode=AccessMode.view_only,
                status=AccessRequestStatus.pending,
            )
        )
        await db.commit()


def _start_demo_server(port: int) -> None:
    import uvicorn

    from bcp_project.main_api import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise SystemExit("The demo server didn't start")


def _token(role: str) -> str:
    from bcp_project.auth import create_access_token

    return create_access_token(subject=USERS[role], role=role, mfa=True)


async def _shoot(base_url: str, out: Path, screens, executable: Optional[str], manifest: bool) -> List[Path]:
    from playwright.async_api import async_playwright

    out.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    host = base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=executable) if executable else await pw.chromium.launch()
        for mode in MODES:
            for width, height in WIDTHS.items():
                contexts: Dict[Optional[str], object] = {}
                for name, role, path in screens:
                    if role not in contexts:
                        context = await browser.new_context(
                            viewport={"width": width, "height": height},
                            device_scale_factor=1,
                            color_scheme=mode,
                            is_mobile=width < 768,
                            has_touch=width < 768,
                            service_workers="block",
                            reduced_motion="reduce",
                        )
                        await context.add_init_script(f"try {{ localStorage.setItem('sb-mode', '{mode}'); }} catch (e) {{}}")
                        if role:
                            await context.add_cookies([{"name": "access_token", "value": _token(role), "domain": host, "path": "/"}])
                        contexts[role] = context
                    page = await contexts[role].new_page()
                    try:
                        await page.goto(base_url + path, wait_until="networkidle", timeout=20_000)
                    except Exception as exc:  # a slow page still gets a picture
                        print(f"  {name}: {exc.__class__.__name__}, taking the picture anyway")
                    await page.wait_for_timeout(300)
                    target = out / f"{name}-{width}-{mode}.png"
                    await page.screenshot(path=str(target), full_page=True)
                    written.append(target)
                    await page.close()
                for context in contexts.values():
                    await context.close()
        if manifest:
            shots = ROOT / "src" / "bcp_project" / "static" / "img" / "screenshots"
            shots.mkdir(parents=True, exist_ok=True)
            for label, (w, h), mobile in (("narrow", (390, 844), True), ("wide", (1280, 800), False)):
                context = await browser.new_context(viewport={"width": w, "height": h}, is_mobile=mobile, service_workers="block", reduced_motion="reduce")
                await context.add_cookies([{"name": "access_token", "value": _token("board_member"), "domain": host, "path": "/"}])
                page = await context.new_page()
                await page.goto(base_url + "/", wait_until="networkidle")
                await page.screenshot(path=str(shots / f"home-{label}.png"))
                await context.close()
        await browser.close()
    return written


def _default_chromium() -> Optional[str]:
    cache = Path.home() / ".cache" / "ms-playwright"
    found = sorted(cache.glob("chromium-*/chrome-linux64/chrome"))
    return str(found[-1]) if found else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", help="Use a server that is already running instead of the demo server")
    parser.add_argument("--out", default=str(ROOT / "build" / "ui-screenshots"))
    parser.add_argument("--only", help="Comma-separated screen names")
    parser.add_argument("--chromium", default=os.getenv("PLAYWRIGHT_CHROMIUM") or None, help="Path to a Chromium binary")
    parser.add_argument("--manifest", action="store_true", help="Also refresh the web app manifest screenshots")
    parser.add_argument("--serve", type=int, metavar="PORT", help="Only run the demo server on PORT and print a session cookie per role")
    args = parser.parse_args()

    if args.serve:
        tmp = Path(tempfile.mkdtemp(prefix="sb-demo-"))
        _demo_env(tmp / "demo.db")
        asyncio.run(_seed())
        _start_demo_server(args.serve)
        print(f"Demo server on http://127.0.0.1:{args.serve}")
        for role in USERS:
            print(f"{role}: Cookie: access_token={_token(role)}", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    screens = SCREENS
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        screens = [s for s in SCREENS if s[0] in wanted]

    base_url = args.base_url
    if base_url:
        sys.path.insert(0, str(ROOT / "src"))
    else:
        tmp = Path(tempfile.mkdtemp(prefix="sb-shots-"))
        _demo_env(tmp / "demo.db")
        asyncio.run(_seed())
        port = _free_port()
        _start_demo_server(port)
        base_url = f"http://127.0.0.1:{port}"

    executable = args.chromium
    if executable is None:
        executable = _default_chromium()
    written = asyncio.run(_shoot(base_url.rstrip("/"), Path(args.out), screens, executable, args.manifest))
    print(f"Wrote {len(written)} screenshots to {args.out}")


if __name__ == "__main__":
    main()
