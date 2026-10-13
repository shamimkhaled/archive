"""Redesigned pages render for each role and show plain wording instead of internal codes."""
from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI


@pytest_asyncio.fixture
async def maker(tmp_path):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from bcp_project.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ui.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed(db):
    from bcp_project.models import DocumentPage, DocumentRecord, DocumentVersion

    def doc(doc_id, status, **extra):
        return DocumentRecord(
            doc_id=doc_id,
            doc_date=date(2026, 3, 1),
            doc_type="Board Minutes",
            keywords=[],
            file_location=f"pdfs/{doc_id}.pdf",
            uploaded_by="up1",
            ingest_status=status,
            created_at=datetime(2026, 3, 2, 4, 30),
            **extra,
        )

    db.add_all(
        [
            doc("SB-1", "indexed", title="March board minutes"),
            doc(
                "SB-2",
                "needs_review",
                title="Audit memo",
                suggested_metadata={"_review_reasons": ["low_ocr_confidence", "missing_title"], "title": "Audit committee memo"},
            ),
            doc("SB-3", "failed", ingest_error="PdfReadError: EOF marker not found"),
        ]
    )
    db.add_all(
        [
            DocumentVersion(doc_id="SB-1", version=1, sha256="a" * 64, file_location="pdfs/SB-1.pdf", created_by="up1"),
            DocumentVersion(doc_id="SB-2", version=1, sha256="b" * 64, file_location="pdfs/SB-2.pdf", created_by="up1", status="pending_review"),
            DocumentPage(doc_id="SB-2", version=1, page_number=1, text="Page one text", ocr_confidence=41.0),
        ]
    )
    await db.commit()


def _app(maker, user, *routers):
    from bcp_project.deps import get_current_user, get_db

    async def _db():
        async with maker() as session:
            yield session

    app = FastAPI()
    for router in routers:
        app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _db
    return app


@pytest.mark.asyncio
async def test_upload_and_my_uploads_pages(maker):
    from bcp_project.models import Role, User
    from bcp_project.routers import documents

    async with maker() as db:
        await _seed(db)
    user = User(username="up1", hashed_password="x", role=Role.uploader, is_active=True)
    app = _app(maker, user, documents.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        page = await client.get("/upload")
        assert page.status_code == 200
        assert "Choose the file" in page.text and "More details (optional)" in page.text
        assert 'name="doc_type"' in page.text and 'name="file"' in page.text

        mine = await client.get("/uploads/mine")
        assert mine.status_code == 200
        text = mine.text
        assert "In the archive" in text and "Waiting for a check" in text
        assert "Some text may have been misread from the scan" in text
        assert "PdfReadError" not in text and "could not be read" in text
        assert "/view/SB-1" not in text  # uploaders cannot open the viewer

        only_check = await client.get("/uploads/mine?show=check")
        assert "SB-2" in only_check.text and "March board minutes" not in only_check.text

        user.role = Role.board_member
        assert (await client.get("/uploads/mine")).status_code == 403


@pytest.mark.asyncio
async def test_review_pages_use_plain_reasons_and_lock_fields_for_uploader(maker, monkeypatch):
    from bcp_project.models import Role, User
    from bcp_project.routers import review

    monkeypatch.setenv("OCR_REVIEW_FOUR_EYES", "1")
    async with maker() as db:
        await _seed(db)
    user = User(username="sec1", hashed_password="x", role=Role.board_secretary, is_active=True)
    app = _app(maker, user, review.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        queue = await client.get("/review")
        assert queue.status_code == 200
        assert "Some text may have been misread from the scan" in queue.text
        assert "Check this document" in queue.text

        detail = await client.get("/review/SB-2")
        assert detail.status_code == 200
        assert "Confirm and publish" in detail.text and "Send back" in detail.text
        assert "Add a title so people can find it in Search." in detail.text
        assert "disabled" not in detail.text.split('id="meta_title"')[1].split(">")[0]

        user.username = "up1"
        user.role = Role.uploader
        own = await client.get("/review/SB-2")
        assert "someone else must check it" in own.text
        assert "Confirm and publish" not in own.text
        assert "disabled" in own.text.split('id="meta_title"')[1].split(">")[0]

        versions = await client.get("/documents/SB-2/versions")
        assert versions.status_code == 200
        assert "Waiting for a check" in versions.text and "pending_review" not in versions.text.split("<main")[1].split("<script")[0]


@pytest.mark.asyncio
async def test_map_lists_years_from_data_and_viewer_uses_local_pdfjs(maker):
    from pathlib import Path

    from bcp_project.models import Role, User
    from bcp_project.routers import documents

    async with maker() as db:
        await _seed(db)
    user = User(username="sec1", hashed_password="x", role=Role.board_secretary, is_active=True)
    app = _app(maker, user, documents.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        page = await client.get("/archive/map")
        assert page.status_code == 200
        assert '<option value="2026">2026</option>' in page.text and 'value="2024"' not in page.text
        assert "Map of connected documents" in page.text and "Semantic" not in page.text

    templates = Path(documents.__file__).resolve().parents[1] / "templates"
    viewer = (templates / "viewer.html").read_text()
    assert "cdnjs" not in viewer and "/static/vendor/pdfjs/" in viewer
    assert (templates.parent / "static/vendor/pdfjs/pdf.min.mjs").exists()
    briefing = (templates / "briefing.html").read_text()
    assert "location.reload" not in briefing and "data-poll-url" in briefing


@pytest.mark.asyncio
async def test_unread_count_and_nav_for_each_role(maker):
    from bcp_project.models import Role, User
    from bcp_project.ui import nav

    for role, expected in (
        (Role.board_member, ["home", "my_meetings", "decisions", "search"]),
        (Role.uploader, ["home", "upload", "my_uploads", "my_meetings"]),
        (Role.board_secretary, ["home", "manage_meetings", "review", "search"]),
        (Role.admin, ["home", "search", "review", "manage_meetings"]),
    ):
        user = User(username="u", hashed_password="x", role=role, is_active=True)
        shell = nav.shell(user, "/")
        assert [i["key"] for i in shell["bottom"]] == expected
        sidebar_keys = {i["key"] for g in shell["sidebar"] for i in g["items"]}
        more_keys = {i["key"] for g in shell["more"] for i in g["items"]}
        assert sidebar_keys == more_keys | set(expected)
    member = User(username="m", hashed_password="x", role=Role.board_member, is_active=True)
    keys = {i["key"] for g in nav.shell(member, "/")["sidebar"] for i in g["items"]}
    assert "upload" not in keys and "people" not in keys and "decisions" in keys
    assert nav.page_title("/view/SB-1") == "Search"
    assert nav.contextual("/meetings/12")["back"] == "/meetings"


@pytest.mark.asyncio
async def test_board_member_meeting_page_and_friendly_sign_in_errors(maker):
    from bcp_project.models import BoardMeeting, MeetingInvitation, Role, User
    from bcp_project.routers import board

    async with maker() as db:
        db.add(BoardMeeting(id=7, title="412th Board Meeting", scheduled_at=datetime(2026, 4, 1, 10, 0), location="https://meet.example/abc", created_by="sec1"))
        db.add(MeetingInvitation(meeting_id=7, username="m1"))
        await db.commit()
    user = User(username="m1", hashed_password="x", role=Role.board_member, is_active=True)
    app = _app(maker, user, board.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        listing = await client.get("/board/meetings")
        assert "My meetings" in listing.text and "412th Board Meeting" in listing.text

        page = await client.get("/board/meetings/7")
        assert page.status_code == 200
        assert "01 Apr 2026, 10:00" in page.text and "Join online" in page.text
        assert "Sign-in hasn't opened yet" in page.text and 'id="sig-pad"' not in page.text

        closed = await client.post("/board/meetings/7/attendance", data={"signature": "data:image/png;base64,AAAA"})
        assert closed.status_code == 303 and "status=attendance_locked" in closed.headers["location"]

        async with maker() as db:
            meeting = await db.get(BoardMeeting, 7)
            meeting.attendance_open = True
            await db.commit()
        bad = await client.post("/board/meetings/7/attendance", data={"signature": "data:image/png;base64,not-an-image"})
        assert bad.status_code == 303 and "status=signature_invalid" in bad.headers["location"]

        page = await client.get("/board/meetings/7?status=signature_invalid")
        assert "We couldn't read your signature" in page.text and 'id="sig-pad"' in page.text


@pytest.mark.asyncio
async def test_meeting_form_offers_select_all(maker):
    from bcp_project.models import Role, User
    from bcp_project.routers import meetings

    async with maker() as db:
        db.add_all([User(username=f"m{i}", hashed_password="x", role=Role.board_member, is_active=True) for i in range(3)])
        await db.commit()
    user = User(username="sec1", hashed_password="x", role=Role.board_secretary, is_active=True)
    app = _app(maker, user, meetings.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        page = await client.get("/meetings/new")
        assert page.status_code == 200
        assert "data-select-all" in page.text and 'name="invitees"' in page.text and 'name="scheduled_at"' in page.text


@pytest.mark.asyncio
async def test_home_login_notifications_access_and_appearance_pages(maker):
    from datetime import timedelta

    from bcp_project.models import (
        AccessMode,
        AccessRequestStatus,
        BoardMeeting,
        DocumentAccessRequest,
        MeetingInvitation,
        NotificationEvent,
        Role,
        User,
    )
    from bcp_project.deps import get_optional_current_user
    from bcp_project.routers import access, notifications, pages

    async with maker() as db:
        await _seed(db)
        db.add(BoardMeeting(id=3, title="Budget sitting", scheduled_at=datetime.now() + timedelta(days=2), created_by="sec1"))
        db.add(MeetingInvitation(meeting_id=3, username="m1"))
        db.add(NotificationEvent(username="m1", kind="invitation", title="Invitation to the budget sitting", body="Budget sitting", meeting_id=3))
        db.add(DocumentAccessRequest(
            doc_id="SB-1", requester_username="m1", purpose="Prepare", requested_mode=AccessMode.view_only,
            status=AccessRequestStatus.approved, expires_at=datetime.utcnow() - timedelta(days=1),
        ))
        db.add(DocumentAccessRequest(
            doc_id="SB-2", requester_username="m1", purpose="Follow-up", requested_mode=AccessMode.view_only,
            status=AccessRequestStatus.pending,
        ))
        await db.commit()

    member = User(username="m1", hashed_password="x", role=Role.board_member, is_active=True, notifications_enabled=True)
    app = _app(maker, member, pages.router, notifications.router, access.router)
    app.dependency_overrides[get_optional_current_user] = lambda: member
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        home = await client.get("/")
        assert "Budget sitting" in home.text and "/board/meetings/3" in home.text
        assert "of your access requests still waiting" in home.text

        notes = await client.get("/notifications")
        assert "Invitation to the budget sitting" in notes.text and "Meeting invitation" in notes.text and "data-notif-open" in notes.text

        mine = await client.get("/access-requests")
        assert "March board minutes" in mine.text and "Expired" in mine.text and "Ask again" in mine.text
        assert "Waiting for approval" in mine.text

        look = await client.get("/appearance")
        for name in ("theme", "mode", "text"):
            assert f'name="{name}"' in look.text and f'data-appearance="{name}"' in look.text

        member.role = Role.board_secretary
        queue = await client.get("/admin/access-requests")
        assert "Audit memo" in queue.text and "data-confirm" in queue.text and "onchange" not in queue.text
        home = await client.get("/")
        assert "access requests to answer" in home.text and "/meetings/3" in home.text

    login_app = _app(maker, None, pages.router)
    login_app.dependency_overrides[get_optional_current_user] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=login_app), base_url="http://t") as client:
        page = await client.get("/")
        assert 'autocapitalize="none"' in page.text and "Forgot your password" in page.text


def test_mfa_setup_page_shows_qr_and_accepts_backup_codes():
    from pathlib import Path

    from bcp_project import mfa

    uri = mfa.provisioning_uri(mfa.generate_secret(), "sec1")
    qr = mfa.qr_data_uri(uri)
    try:
        import segno  # noqa: F401
    except ImportError:
        assert qr is None
    else:
        assert qr.startswith("data:image/svg+xml")
    template = (Path(mfa.__file__).parent / "templates" / "mfa.html").read_text()
    assert "qr_data_uri" in template and "useBackup" in template and "printCodes" in template


@pytest.mark.asyncio
async def test_admin_pages_use_plain_words_and_confirmations(maker, monkeypatch):
    from bcp_project.models import Role, User
    from bcp_project.routers import admin, admin_ai, admin_queues

    async with maker() as db:
        await _seed(db)
        db.add(User(username="clerk1", hashed_password="x", role=Role.uploader, is_active=True))
        await db.commit()

    async def _streams():
        return {"bcp:stream:ingest": {"length": 2, "pending": 1, "consumers": 1}, "bcp:stream:ingest:dead": {"length": 1}}

    async def _dead(limit=50):
        return [
            {
                "entry_id": "1-0",
                "job_id": "j1",
                "type": "document_ingest",
                "attempts": 5,
                "reason": "max_attempts",
                "error": "httpx.ConnectError: connection refused",
                "source_stream": "bcp:stream:ingest",
                "dead_at": "2026-03-02T04:30:00",
            }
        ]

    async def _noop():
        return None

    monkeypatch.setattr(admin_queues, "stream_stats", _streams)
    monkeypatch.setattr(admin_queues, "list_dead_letters", _dead)
    monkeypatch.setattr(admin_ai.routing, "refresh_registry", _noop)

    user = User(id=99, username="admin1", hashed_password="x", role=Role.admin, is_active=True)
    app = _app(maker, user, admin.router, admin_queues.router, admin_ai.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        people = await client.get("/admin/users")
        assert people.status_code == 200
        assert 'data-confirm-type="clerk1"' in people.text and "peopleSearch" in people.text

        tasks = await client.get("/admin/queues")
        assert tasks.status_code == 200
        assert "Background tasks" in tasks.text and "Reading an uploaded document" in tasks.text
        assert "stopped after several tries" in tasks.text and "/admin/queues/dead/1-0/retry" in tasks.text
        assert "bcp:stream:ingest:dead" not in tasks.text

        settings = await client.get("/admin/ai")
        assert settings.status_code == 200
        assert "AI settings" in settings.text and "Answering questions in Ask Sonali" in settings.text
        assert 'id="aiRouteForm"' in settings.text and "data-confirm" in settings.text


@pytest.mark.asyncio
async def test_pages_are_not_cached_and_logout_clears_the_browser_cache(maker):
    from fastapi.responses import HTMLResponse, PlainTextResponse
    from fastapi.staticfiles import StaticFiles

    from bcp_project.deps import STATIC_DIR, get_db, get_optional_current_user
    from bcp_project.routers import auth
    from bcp_project.security import SecurityHeadersMiddleware

    async def _db():
        async with maker() as session:
            yield session

    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(auth.router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_optional_current_user] = lambda: None

    @app.get("/page", response_class=HTMLResponse)
    async def _page():
        return "<p>secret</p>"

    @app.get("/api/thing")
    async def _api():
        return {"ok": True}

    @app.get("/cached")
    async def _cached():
        return PlainTextResponse("x", headers={"Cache-Control": "no-cache"})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        page = await client.get("/page")
        assert page.headers["cache-control"] == "no-store"
        csp = page.headers["content-security-policy"]
        assert "cdnjs" not in csp and "googleapis" not in csp and "gstatic" not in csp
        assert "'wasm-unsafe-eval'" in csp
        assert (await client.get("/api/thing")).headers["cache-control"] == "no-store"
        assert (await client.get("/cached")).headers["cache-control"] == "no-cache"
        offline = await client.get("/static/offline.html")
        assert offline.status_code == 200 and "no-store" not in offline.headers.get("cache-control", "")
        assert "googleapis" not in offline.text and 'lang="bn"' in offline.text

        bye = await client.get("/logout")
        assert bye.status_code == 303 and bye.headers["clear-site-data"] == '"cache"'
