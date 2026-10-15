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
async def test_search_activity_page_is_admin_only_audited_and_exports(maker):
    from sqlalchemy import select

    from bcp_project.models import AuditLog, Role, User
    from bcp_project.routers import admin_search_activity
    from bcp_project.services.search_log import AiTrace, record_search

    async with maker() as db:
        await _seed(db)
        await record_search(
            db,
            channel="web_search",
            username="member1",
            role=Role.board_member,
            query="ঋণ নীতি ২০২৬",
            results=[{"doc_id": "SB-OK", "can_view": True, "page": 1}],
        )
        await record_search(
            db,
            channel="ask",
            username="member1",
            role=Role.board_member,
            query="What was approved?",
            results=[{"doc_id": "SB-OK", "can_view": True, "page": 2}],
            ai_trace=AiTrace(
                model="qwen3",
                source_docs=[{"doc_id": "SB-OK", "page": 2}],
                cited_docs=[{"doc_id": "SB-OK", "page": 2}],
                withheld_docs=[{"doc_id": "SB-SECRET", "page": 1}],
            ),
        )

    admin_user = User(id=99, username="admin1", hashed_password="x", role=Role.admin, is_active=True)
    app = _app(maker, admin_user, admin_search_activity.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        page = await client.get("/admin/search-activity", headers={"User-Agent": "Mobile 360"})
        assert page.status_code == 200
        assert "ঋণ নীতি ২০২৬" in page.text and "Ask Sonali" in page.text
        assert "Withheld from the AI" in page.text and "SB-SECRET" in page.text

        only_secret = await client.get("/api/admin/search-activity", params={"doc_id": "SB-SECRET"})
        assert only_secret.status_code == 200
        body = only_secret.json()
        assert body["total"] == 1 and body["entries"][0]["channel"] == "ask"
        assert body["entries"][0]["intact"] is True
        assert [d["doc_id"] for d in body["entries"][0]["ai"]["cited_docs"]] == ["SB-OK"]

        export = await client.get("/admin/search-activity.csv", params={"channel": "web_search"})
        assert export.status_code == 200 and export.headers["content-type"].startswith("text/csv")
        lines = export.text.strip().splitlines()
        assert lines[0].startswith("id,created_at,username") and len(lines) == 2

    async with maker() as db:
        actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert actions.count("search_logged") == 2
    assert "search_log_view" in actions and "search_log_export" in actions

    member = User(id=5, username="member1", hashed_password="x", role=Role.board_member, is_active=True)
    app = _app(maker, member, admin_search_activity.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.get("/admin/search-activity")).status_code == 403
        assert (await client.get("/admin/search-activity.csv")).status_code == 403


@pytest.mark.asyncio
async def test_audit_log_page_filters_flags_tampering_and_exports(maker):
    from datetime import timedelta

    from sqlalchemy import select, update

    from bcp_project.access_control import write_audit
    from bcp_project.models import AuditLog, Role, User
    from bcp_project.routers import admin_audit
    from bcp_project.services.audit_chain import seal_pending

    async with maker() as db:
        await write_audit(db, username="up1", action="upload", resource_type="document", resource_id="SB-1", correlation_id="req-1")
        await write_audit(db, username="up1", action="view", resource_type="document", resource_id="SB-1", correlation_id="req-1")
        await write_audit(db, username="member1", action="download", resource_type="document", resource_id="SB-2")
        await write_audit(db, username="member1", action="login", resource_type="session", detail="=HYPERLINK(\"http://x\")")
        await db.commit()
        assert await seal_pending(db, now=datetime.utcnow() + timedelta(minutes=1)) == 4
        await write_audit(db, username="admin1", action="user_update", resource_type="user", resource_id="member1", commit=True)
        await db.execute(update(AuditLog).where(AuditLog.action == "download").values(resource_id="SB-OTHER"))
        await db.commit()

    admin_user = User(id=99, username="admin1", hashed_password="x", role=Role.admin, is_active=True)
    app = _app(maker, admin_user, admin_audit.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        page = await client.get("/admin/audit", headers={"User-Agent": "Mobile 360"})
        assert page.status_code == 200
        assert "Audit log" in page.text and "has not been checked yet" in page.text
        assert "Changed after sealing" in page.text and "Not sealed yet" in page.text
        assert 'href="/view/SB-1"' in page.text

        one_request = (await client.get("/api/admin/audit", params={"correlation_id": "req-1"})).json()
        assert one_request["total"] == 2 and {e["action"] for e in one_request["entries"]} == {"upload", "view"}
        assert all(e["sealed"] and e["intact"] for e in one_request["entries"])

        tampered = (await client.get("/api/admin/audit", params={"action": "download"})).json()["entries"][0]
        assert tampered["intact"] is False and tampered["resource_id"] == "SB-OTHER"
        pending = (await client.get("/api/admin/audit", params={"username": "ADMIN1", "action": "user_update"})).json()
        assert pending["total"] == 1 and pending["entries"][0]["sealed"] is False
        assert (await client.get("/api/admin/audit", params={"date_from": "bad"})).status_code == 400

        export = await client.get("/admin/audit.csv", params={"resource_type": "session"})
        assert export.status_code == 200 and export.headers["content-type"].startswith("text/csv")
        lines = export.text.strip().splitlines()
        assert lines[0].startswith("id,created_at,username") and len(lines) == 2
        assert "'=HYPERLINK" in lines[1]

        verify = await client.post("/admin/audit/verify")
        assert verify.status_code == 303 and verify.headers["location"] == "/admin/audit"
        after = await client.get("/admin/audit")
        assert "has been tampered with" in after.text and "row content changed" in after.text

    async with maker() as db:
        actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert "audit_log_view" in actions and "audit_log_export" in actions and "audit_chain_broken" in actions

    member = User(id=5, username="member1", hashed_password="x", role=Role.board_member, is_active=True)
    app = _app(maker, member, admin_audit.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.get("/admin/audit")).status_code == 403
        assert (await client.get("/admin/audit.csv")).status_code == 403
        assert (await client.post("/admin/audit/verify")).status_code == 403


@pytest.mark.asyncio
async def test_ai_filled_details_wait_for_a_person_to_confirm(maker, monkeypatch):
    from unittest.mock import AsyncMock

    from sqlalchemy import and_, select, update

    from bcp_project.models import AuditLog, DocumentRecord, Role, User
    from bcp_project.routers import review
    from bcp_project.search_acl import SearchFilters

    monkeypatch.setenv("OCR_REVIEW_FOUR_EYES", "1")
    monkeypatch.setattr(review, "refresh_search_payload", AsyncMock())
    monkeypatch.setattr("bcp_project.cache.bump_search_cache_version", AsyncMock())
    async with maker() as db:
        await _seed(db)
        await db.execute(
            update(DocumentRecord)
            .where(DocumentRecord.doc_id == "SB-1")
            .values(
                department="Finance",
                metadata_check_pending=True,
                suggested_metadata={"_applied": ["title", "department"], "_source": {"kind": "ai"}},
                metadata_confidence={"title": 0.9, "department": 0.6},
            )
        )
        await db.commit()

        async def unchecked_ids(value):
            conditions = SearchFilters.from_params(metadata_check=value).sql_conditions()
            return (await db.execute(select(DocumentRecord.doc_id).where(and_(*conditions)))).scalars().all()

        assert await unchecked_ids("unchecked") == ["SB-1"]
        assert "SB-1" not in await unchecked_ids("checked")
    with pytest.raises(ValueError):
        SearchFilters.from_params(metadata_check="maybe")

    user = User(username="sec1", hashed_password="x", role=Role.board_secretary, is_active=True)
    app = _app(maker, user, review.router)
    form = {"doc_type": "Board Minutes", "doc_date": "2026-03-01", "title": "March board minutes", "department": "Treasury"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        queue = await client.get("/review")
        assert "Details to confirm (1)" in queue.text
        details = await client.get("/review?list=details")
        assert "Filled in by the AI: Title, Department" in details.text and "Lower confidence: Department" in details.text
        page = await client.get("/documents/SB-1/metadata")
        assert "Details not yet checked" in page.text and "Confirm details" in page.text and "Filled in by AI" in page.text

        user.username, user.role = "up1", Role.admin
        own = await client.post("/documents/SB-1/metadata", data=form)
        assert own.status_code == 303 and own.headers["location"].endswith("status=own_upload")
        async with maker() as db:
            doc = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == "SB-1"))).scalar_one()
            assert doc.metadata_check_pending is True and doc.department == "Treasury"
            assert doc.metadata_confidence["department"] == 1.0 and doc.metadata_confirmed_by is None

        user.username, user.role = "sec1", Role.board_secretary
        done = await client.post("/documents/SB-1/metadata", data=form)
        assert done.headers["location"].endswith("status=confirmed")
        assert "All details are checked." in (await client.get("/review?list=details")).text

        user.username, user.role = "up1", Role.uploader
        assert "Details to confirm" not in (await client.get("/review")).text

    async with maker() as db:
        doc = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == "SB-1"))).scalar_one()
        assert doc.metadata_check_pending is False and doc.metadata_confirmed_by == "sec1"
        details = (await db.execute(select(AuditLog.detail).where(AuditLog.action == "metadata_confirm"))).scalars().all()
    assert details == ["changed=none ai_filled=title,department"]


@pytest.mark.asyncio
async def test_package_and_metadata_pages_render_bangla_on_mobile(maker, monkeypatch):
    from datetime import timedelta

    from bcp_project.models import (
        AccessMode,
        AccessRequestStatus,
        BoardBriefing,
        BoardMeeting,
        DocumentAccessRequest,
        DocumentRecord,
        DocumentVersion,
        MeetingInvitation,
        Role,
        User,
    )
    from bcp_project.routers import board, briefings, meetings, review
    from bcp_project.services.briefing import BoardPaperSummary, BriefingPoint, BriefingSection

    async def no_refs(*a, **k):
        return []

    monkeypatch.setattr(briefings, "historical_references", no_refs)
    mobile = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; 360x800) Mobile"}
    async with maker() as db:
        await _seed(db)
        sb1 = await db.get(DocumentRecord, 1)
        sb1.subject = "মূলধন সংযোজন"
        sb1.language = "bn"
        sb1.metadata_confidence = {"subject": 0.4, "title": 0.95}
        db.add(
            DocumentRecord(
                doc_id="SB-4", doc_date=date(2026, 3, 1), doc_type="Annexure", keywords=[], file_location="pdfs/SB-4.pdf",
                uploaded_by="up1", ingest_status="indexed", title="নিরীক্ষিত হিসাব", page_count=2,
            )
        )
        db.add(DocumentVersion(doc_id="SB-4", version=1, sha256="c" * 64, file_location="pdfs/SB-4.pdf", created_by="up1"))
        db.add(BoardMeeting(id=7, title="412th Board Meeting", scheduled_at=datetime(2026, 4, 1, 10, 0), created_by="sec1"))
        db.add(MeetingInvitation(meeting_id=7, username="m1"))
        for doc_id in ("SB-1", "SB-4"):
            db.add(
                DocumentAccessRequest(
                    doc_id=doc_id, requester_username="m1", purpose="board work", requested_mode=AccessMode.view_only,
                    status=AccessRequestStatus.approved, expires_at=datetime.utcnow() + timedelta(days=7),
                )
            )
        await db.commit()

    sec = User(id=2, username="sec1", hashed_password="x", role=Role.board_secretary, is_active=True)
    app = _app(maker, sec, briefings.router, meetings.router, review.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=mobile) as client:
        listing = await client.get("/board/packages")
        assert listing.status_code == 200 and "No packages yet" in listing.text
        form = await client.get("/board/packages/new", params={"meeting_id": 7})
        assert form.status_code == 200 and 'value="7" selected' in form.text
        created = await client.post("/board/packages", data={"title": "মূলধন প্যাকেজ", "meeting_id": "7", "memo_doc_id": "SB-1"})
        assert created.status_code == 303 and created.headers["location"] == "/board/packages/1?status=created"
        added = await client.post("/board/packages/1/members", data={"doc_id": "SB-4", "role": "annexure"})
        assert added.status_code == 303 and "status=saved" in added.headers["location"]
        detail = await client.get("/board/packages/1")
        assert detail.status_code == 200
        assert "মূলধন প্যাকেজ" in detail.text and "নিরীক্ষিত হিসাব" in detail.text and ">A1<" in detail.text
        empty = await client.get("/board/packages/1/briefing")
        assert empty.status_code == 200 and "/board/packages/1/briefing/generate" in empty.text

        summary = BoardPaperSummary(
            proposal=BriefingSection(points=[BriefingPoint(text="মূলধন সংযোজনের প্রস্তাব", page=1, doc_id="SB-1")], not_found=False),
            risks=BriefingSection(points=[BriefingPoint(text="ঋণ ঝুঁকি বেড়েছে", page=2, doc_id="SB-4")], not_found=False),
        )
        async with maker() as db:
            db.add(
                BoardBriefing(
                    doc_id="SB-1", version=1, package_id=1, package_revision=2, state="approved", status="ready",
                    content=summary.model_dump(), requested_by="sec1", approved_by="sec2", sha256="0" * 64,
                    members=[
                        {"doc_id": "SB-1", "version": 1, "role": "memo", "label": "M", "seq": 0, "page_count": 1, "title": "March board minutes"},
                        {"doc_id": "SB-4", "version": 1, "role": "annexure", "label": "A1", "seq": 1, "page_count": 2, "title": "নিরীক্ষিত হিসাব"},
                    ],
                )
            )
            await db.commit()
        meeting = await client.get("/meetings/7")
        assert meeting.status_code == 200 and "/board/packages/1/briefing" in meeting.text

        metadata = await client.get("/documents/SB-1/metadata")
        assert metadata.status_code == 200
        assert "মূলধন সংযোজন" in metadata.text and "is-low-confidence" in metadata.text

    member = User(id=5, username="m1", hashed_password="x", role=Role.board_member, is_active=True)
    app = _app(maker, member, briefings.router, board.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=mobile) as client:
        meeting = await client.get("/board/meetings/7")
        assert meeting.status_code == 200 and "/board/packages/1/briefing" in meeting.text
        page = await client.get("/board/packages/1/briefing")
        assert page.status_code == 200
        text = page.text
        assert "Key risks for the Board" in text and "briefing-section--attention" in text
        assert "ঋণ ঝুঁকি বেড়েছে" in text and 'class="paper-chip"' in text
        assert "Required Board decision" in text and "Papers covered" in text
        assert (await client.get("/board/packages")).status_code == 403


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
