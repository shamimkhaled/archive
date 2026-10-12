"""Phase 9: React PWA hosting, per-screen rollout redirects, the form-route JSON bridge, and app payload endpoints."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient


@pytest.fixture
def dist(tmp_path, monkeypatch):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=root></div>")
    (root / "assets" / "index-abc.js").write_text("console.log(1)")
    (root / "sw.js").write_text("self.addEventListener('install',()=>{})")
    (tmp_path / "secret.txt").write_text("nope")
    monkeypatch.setenv("FRONTEND_DIST", str(root))
    return root


@pytest.fixture
def app(dist, monkeypatch):
    from bcp_project.routers.app_shell import router
    from bcp_project.spa import spa_middleware

    monkeypatch.setenv("UI_REACT_SCREENS", "search,meetings")
    app = FastAPI()
    app.middleware("http")(spa_middleware)
    app.include_router(router)

    @app.get("/search")
    async def search_page():
        return {"page": "jinja-search"}

    @app.get("/review")
    async def review_page():
        return {"page": "jinja-review"}

    @app.post("/meetings/{meeting_id}/minutes/approve")
    async def approve(meeting_id: int, outcome: str = "ok"):
        code = "minutes_approved" if outcome == "ok" else "minutes_four_eyes"
        return RedirectResponse(f"/meetings/{meeting_id}?status={code}#minutes", status_code=303)

    @app.post("/admin/ai/route")
    async def route():
        return RedirectResponse("/admin/ai?error=Unknown%20model", status_code=303)

    @app.post("/login")
    async def login():
        return RedirectResponse("/", status_code=303)

    return app


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


HTML = {"accept": "text/html,application/xhtml+xml"}
SPA = {"x-bcp-client": "spa"}


# --- rollout redirects -----------------------------------------------------------------------


def test_flagged_screen_redirects_browser_navigation(client):
    response = client.get("/search?q=loan&lang=bn", headers=HTML)
    assert response.status_code == 307
    assert response.headers["location"] == "/app/search?q=loan&lang=bn"


def test_classic_escape_hatch_and_unflagged_screens_stay_on_jinja(client):
    assert client.get("/search?classic=1", headers=HTML).json() == {"page": "jinja-search"}
    assert client.get("/review", headers=HTML).json() == {"page": "jinja-review"}


def test_json_and_spa_requests_are_not_redirected(client):
    assert client.get("/search", headers={"accept": "application/json"}).status_code == 200
    assert client.get("/search", headers={**HTML, **SPA}).status_code == 200


def test_jinja_shell_navigation_gets_hard_nav_hint(client):
    response = client.get("/search", headers={**HTML, "x-requested-with": "BCPNav"})
    assert response.status_code == 204
    assert response.headers["x-bcp-hard-nav"] == "/app/search"


def test_no_redirect_until_the_app_is_built(client, dist):
    (dist / "index.html").unlink()
    assert client.get("/search", headers=HTML).status_code == 200


def test_all_flag_and_unknown_names(monkeypatch):
    from bcp_project.spa import SCREENS, enabled_screens

    monkeypatch.setenv("UI_REACT_SCREENS", "all")
    assert enabled_screens() == set(SCREENS)
    monkeypatch.setenv("UI_REACT_SCREENS", "search, bogus ,admin")
    assert enabled_screens() == {"search", "admin"}
    monkeypatch.delenv("UI_REACT_SCREENS")
    assert enabled_screens() == set()


# --- form bridge ----------------------------------------------------------------------------------


def test_bridge_returns_json_for_spa_posts(client):
    ok = client.post("/meetings/4/minutes/approve", headers=SPA)
    assert ok.status_code == 200
    assert ok.json() == {"ok": True, "status": "minutes_approved", "error": None, "redirect": "/meetings/4?status=minutes_approved#minutes"}

    refused = client.post("/meetings/4/minutes/approve?outcome=no", headers=SPA)
    assert refused.status_code == 409
    assert refused.json()["ok"] is False and refused.json()["status"] == "minutes_four_eyes"


def test_bridge_reports_error_query(client):
    response = client.post("/admin/ai/route", headers=SPA)
    assert response.status_code == 409
    assert response.json()["error"] == "Unknown model"


def test_classic_posts_and_login_keep_redirects(client):
    assert client.post("/meetings/4/minutes/approve").status_code == 303
    assert client.post("/login", headers=SPA).status_code == 303


@pytest.mark.parametrize(
    "code,failed",
    [
        ("briefing_locked", True),
        ("briefing_input", True),
        ("minutes_input_required", True),
        ("minutes_archive_failed", True),
        ("briefing_approved", False),
        ("decision_saved", False),
    ],
)
def test_failure_codes(code, failed):
    from bcp_project.spa import bridge_redirect

    response = bridge_redirect(RedirectResponse(f"/x?status={code}", status_code=303))
    assert (response.status_code == 409) is failed


# --- static hosting ----------------------------------------------------------------------------


def test_serves_hashed_assets_immutable_and_shell_no_store(client):
    asset = client.get("/app/assets/index-abc.js")
    assert asset.status_code == 200
    assert "immutable" in asset.headers["cache-control"]

    shell = client.get("/app/meetings/12")
    assert shell.status_code == 200
    assert "id=root" in shell.text
    assert shell.headers["cache-control"] == "no-store"

    assert client.get("/app").status_code == 200


def test_service_worker_scope_header(client):
    response = client.get("/app/sw.js")
    assert response.headers["service-worker-allowed"] == "/app/"
    assert response.headers["cache-control"] == "no-cache"


def test_missing_files_and_traversal_are_404(client):
    from bcp_project.spa import serve_spa

    assert client.get("/app/assets/missing.js").status_code == 404
    assert serve_spa("../secret.txt").status_code == 404
    assert serve_spa("assets/../../secret.txt").status_code == 404


def test_not_built_explains_how_to_build(client, dist):
    (dist / "index.html").unlink()
    response = client.get("/app/search")
    assert response.status_code == 404
    assert "npm run build" in response.json()["detail"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("/app/view/BCP-1?page=3", "/app/view/BCP-1?page=3"),
        ("/search", "/search"),
        ("//evil.example/x", None),
        ("https://evil.example/", None),
        ("/\\evil.example", None),
        ("/app\n/x", None),
        ("", None),
        (None, None),
    ],
)
def test_safe_next(value, expected):
    from bcp_project.spa import safe_next

    assert safe_next(value) == expected


# --- payload endpoints -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_permissions_follow_role(monkeypatch):
    from bcp_project.models import Role
    from bcp_project.routers.app_shell import app_session

    monkeypatch.setenv("UI_REACT_SCREENS", "search,viewer")
    member = await app_session(current_user=SimpleNamespace(username="bm", role=Role.board_member))
    assert member["permissions"] == {"search": True, "review": False, "organize_meetings": False, "admin": False}
    assert member["screens"] == ["search", "viewer"]
    uploader = await app_session(current_user=SimpleNamespace(username="up", role=Role.uploader))
    assert uploader["permissions"]["review"] and not uploader["permissions"]["search"]
    admin = await app_session(current_user=SimpleNamespace(username="ad", role=Role.admin))
    assert all(admin["permissions"].values())


@pytest_asyncio.fixture
async def maker(tmp_path):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from bcp_project.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'p9.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _meeting(db, *, minutes_state="draft"):
    from bcp_project.models import BoardMeeting, MeetingDocument, MeetingInvitation

    meeting = BoardMeeting(
        title="Board 2026-10",
        scheduled_at=datetime(2026, 10, 1, 10, 0),
        created_by="sec",
        agenda="1. Budget\n2. Branch automation",
        minutes_draft="Secret draft text",
        minutes_state=minutes_state,
        minutes_doc_id="MIN-1" if minutes_state == "approved" else None,
    )
    db.add(meeting)
    await db.flush()
    db.add(MeetingInvitation(meeting_id=meeting.id, username="bm"))
    db.add(MeetingDocument(meeting_id=meeting.id, title="Budget paper", file_location="m/1.pdf", uploaded_by="sec"))
    await db.commit()
    return meeting


@pytest.mark.asyncio
async def test_meeting_payload_hides_draft_minutes_from_invitees(maker):
    from bcp_project.models import Role
    from bcp_project.routers.app_shell import app_meeting

    async with maker() as db:
        meeting = await _meeting(db)
        member = SimpleNamespace(username="bm", role=Role.board_member)
        payload = await app_meeting(meeting.id, current_user=member, db=db)
        assert payload["is_organizer"] is False
        assert payload["minutes"] == {"state": "draft", "doc_id": None}
        assert "Secret" not in str(payload)
        assert payload["documents"][0]["file_url"] == f"/board/meetings/{meeting.id}/documents/{payload['documents'][0]['id']}/file"
        assert payload["agenda_items"]

        secretary = SimpleNamespace(username="sec", role=Role.board_secretary)
        organizer_view = await app_meeting(meeting.id, current_user=secretary, db=db)
        assert organizer_view["is_organizer"] is True
        assert organizer_view["minutes"]["body_md"] == "Secret draft text"
        assert organizer_view["minutes"]["workflow"]["state"] == "draft"


@pytest.mark.asyncio
async def test_invitee_sees_approved_minutes_link_and_outsiders_get_404(maker):
    from bcp_project.models import Role
    from bcp_project.routers.app_shell import app_meeting

    async with maker() as db:
        meeting = await _meeting(db, minutes_state="approved")
        payload = await app_meeting(meeting.id, current_user=SimpleNamespace(username="bm", role=Role.board_member), db=db)
        assert payload["minutes"] == {"state": "approved", "doc_id": "MIN-1"}
        with pytest.raises(HTTPException) as exc:
            await app_meeting(meeting.id, current_user=SimpleNamespace(username="other", role=Role.board_member), db=db)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_login_page_remembers_safe_next():
    from starlette.requests import Request

    from bcp_project.routers.auth import LOGIN_NEXT_COOKIE, login_page

    def request(query: bytes) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/login",
                "query_string": query,
                "headers": [],
                "client": ("10.0.0.1", 1),
                "server": ("testserver", 80),
                "scheme": "http",
                "root_path": "",
                "app": FastAPI(),
            }
        )

    good = await login_page(request(b"next=%2Fapp%2Fview%2FBCP-1%3Fpage%3D2"))
    assert f"{LOGIN_NEXT_COOKIE}=" in good.headers.get("set-cookie", "")
    bad = await login_page(request(b"next=%2F%2Fevil.example"))
    assert LOGIN_NEXT_COOKIE not in bad.headers.get("set-cookie", "")
