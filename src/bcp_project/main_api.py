import asyncio
import base64
import json
import logging
import mimetypes
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, cast, func, or_, select, String, text
from sqlalchemy.ext.asyncio import AsyncSession

from .access_control import (
    assert_can_download,
    assert_can_view,
    default_expires_at,
    enrich_results_with_access,
    has_open_pending_request,
    is_archive_download_allowed,
    is_archive_privileged,
    write_audit,
)
from .auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    decode_access_token,
    get_password_hash,
    should_refresh_access_token,
    verify_password,
)
from .brand import BRAND
from .aws_utils import get_s3_client, probe_s3, storage_backend_name, store_pdf, use_local_storage
from .cache import (
    bump_search_cache_version,
    get_cached_metadata_search,
    get_cached_search,
    set_cached_metadata_search,
    set_cached_search,
)
from .calendar_utils import build_google_calendar_link, build_meeting_ics, new_meeting_uid
from .config import cookie_secure, is_production, load_environment
from .db import get_session, engine
from .document_types import merge_document_types, normalize_document_type
from .models import (
    AccessMode,
    AccessRequestStatus,
    Base,
    BoardMeeting,
    DocumentAccessRequest,
    DocumentRecord,
    MeetingAttendance,
    MeetingDocument,
    MeetingInvitation,
    MeetingStatus,
    NotificationEvent,
    Role,
    User,
)
from .graph_builder import (
    GraphBuildResult,
    GraphDoc,
    build_document_graph,
    document_summary_card,
    related_documents_payload,
    summary_entities,
)
from .chunker import chunk_documents
from .notify import notify_meeting_email
from .pdf_parser import parse_pdf
from .pdf_watermark import stamp_pdf_bytes
from .reminders import send_due_reminders
from .qdrant_store import make_qdrant_indexer, build_summary_embedding_text, embed_texts
from .security import (
    CsrfMiddleware,
    SecurityHeadersMiddleware,
    login_rate_limiter,
    new_csrf_token,
    set_csrf_cookie,
)
from .summary_extractor import extract_document_summary

logger = logging.getLogger("bcp_project.main_api")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

load_environment()

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploaded_pdfs"))
MEETING_UPLOAD_DIR = UPLOAD_DIR / "meetings"


def ensure_upload_dirs() -> None:
    """Create upload dirs if possible. Railway volumes may need entrypoint chown first."""
    for path in (UPLOAD_DIR, MEETING_UPLOAD_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create upload directory %s: %s", path, exc)


ensure_upload_dirs()

_docs_enabled = not is_production()
app = FastAPI(
    title=BRAND.product_name,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)
app.add_middleware(SecurityHeadersMiddleware, is_production=is_production())
app.add_middleware(CsrfMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.middleware("http")
async def sliding_session_middleware(request: Request, call_next):
    """Extend auth cookie while the user is actively using the app."""
    response = await call_next(request)
    if request.method == "OPTIONS":
        return response
    path = request.url.path or ""
    if path.startswith("/static/") or path in {"/sw.js", "/healthz", "/readyz"}:
        return response

    token = request.cookies.get("access_token")
    if not token:
        return response
    payload = decode_access_token(token)
    if not payload or not should_refresh_access_token(payload):
        return response
    username = payload.get("sub")
    role = payload.get("role")
    if not username or not role:
        return response
    try:
        _set_auth_cookie(response, create_access_token(subject=username, role=str(role)))
    except Exception:
        logger.exception("Failed to refresh access token cookie")
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Send full-page navigations to login on auth failure; keep JSON for APIs/SPA fetches."""
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        is_spa = request.headers.get("X-Requested-With") == "BCPNav"
        accept = (request.headers.get("accept") or "").lower()
        wants_html = "text/html" in accept
        if wants_html and not is_spa and not request.url.path.startswith("/api/"):
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

def _compute_static_version() -> str:
    candidates = [
        STATIC_DIR / "css" / "style.css",
        STATIC_DIR / "js" / "app.js",
        STATIC_DIR / "js" / "sw.js",
        STATIC_DIR / "js" / "archive-graph.js",
        STATIC_DIR / "manifest.webmanifest",
    ]
    mtimes = [p.stat().st_mtime for p in candidates if p.exists()]
    return str(int(max(mtimes))) if mtimes else "1"


templates.env.globals["static_version"] = _compute_static_version()
templates.env.globals["brand"] = BRAND


def _safe_back_url(request: Request, fallback: str = "/") -> str:
    """Prefer same-origin ?from= query or Referer for viewer back links."""
    from_param = request.query_params.get("from")
    if from_param and from_param.startswith("/") and not from_param.startswith("//"):
        return from_param

    referer = request.headers.get("referer") or request.headers.get("Referer")
    if not referer:
        return fallback

    from urllib.parse import urlparse

    parsed = urlparse(referer)
    if parsed.netloc and parsed.netloc != request.url.netloc:
        return fallback
    if not parsed.path or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return fallback
    if parsed.path == request.url.path:
        return fallback
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    icon = STATIC_DIR / "img" / "sonali-bank-logo.png"
    if not icon.exists():
        raise HTTPException(status_code=404, detail="Favicon not found")
    return FileResponse(icon, media_type="image/png")


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "js" / "sw.js",
        media_type="application/javascript; charset=utf-8",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=cookie_secure(),
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def _load_stored_pdf_bytes(file_location: str, *, resource_id: str) -> bytes:
    location = (file_location or "").strip()
    if not location:
        raise HTTPException(
            status_code=404,
            detail="Document has no file location. Re-upload with STORAGE_BACKEND=s3 on Railway.",
        )

    if location.startswith("s3://"):
        _, path = location.split("s3://", 1)
        if "/" not in path:
            raise HTTPException(status_code=502, detail="Invalid S3 location on document record")
        bucket, key = path.split("/", 1)

        def _fetch() -> bytes:
            s3_client = get_s3_client()
            obj = s3_client.get_object(Bucket=bucket, Key=key)
            return obj["Body"].read()

        try:
            return await asyncio.to_thread(_fetch)
        except (BotoCoreError, ClientError) as exc:
            logger.exception("S3 get_object failed for %s (%s)", resource_id, location)
            raise HTTPException(
                status_code=502,
                detail="Failed to fetch PDF from S3. Check AWS credentials/bucket on Railway.",
            ) from exc

    # Local path — ephemeral on Railway unless a volume is mounted.
    path = Path(location)
    if not path.is_absolute():
        path = UPLOAD_DIR / location
    path = path.resolve()
    try:
        path.relative_to(UPLOAD_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="File not found on disk") from exc
    if not path.exists():
        logger.error(
            "PDF missing on disk for %s at %s (storage=%s). "
            "On Railway set STORAGE_BACKEND=s3 and re-upload this document.",
            resource_id,
            path,
            storage_backend_name(),
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "PDF file missing from server disk. "
                "On Railway this usually means the file was stored locally and lost after redeploy. "
                "Set STORAGE_BACKEND=s3 with AWS_* secrets, then re-upload the document."
            ),
        )
    return path.read_bytes()


async def _load_pdf_bytes(document: DocumentRecord) -> bytes:
    return await _load_stored_pdf_bytes(document.file_location, resource_id=document.doc_id)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _run_reminder_sweep() -> None:
    async with get_session() as session:
        await send_due_reminders(session)


@app.on_event("startup")
async def startup_event() -> None:
    ensure_upload_dirs()
    await init_db()

    backend = storage_backend_name()
    if is_production() and use_local_storage():
        allow = (os.getenv("ALLOW_EPHEMERAL_UPLOADS") or "").strip().lower() in {"1", "true", "yes", "on"}
        if allow:
            logger.warning(
                "STORAGE_BACKEND=local on production (ALLOW_EPHEMERAL_UPLOADS=1). "
                "PDF view/download will break after Railway redeploys."
            )
        else:
            logger.error(
                "STORAGE_BACKEND is local/ephemeral while APP_ENV=production. "
                "Set STORAGE_BACKEND=s3 and AWS_* secrets or uploads cannot be viewed after redeploy."
            )
    else:
        logger.info("PDF storage backend: %s", backend)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(_run_reminder_sweep, "interval", minutes=15, next_run_time=datetime.utcnow())
    scheduler.start()
    app.state.scheduler = scheduler


@app.on_event("shutdown")
async def shutdown_event() -> None:
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.shutdown(wait=False)


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> Dict[str, Any]:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc

    storage = storage_backend_name()
    payload: Dict[str, Any] = {
        "status": "ready",
        "storage": storage,
        "upload_dir": str(UPLOAD_DIR),
    }
    if storage == "s3":
        payload["s3"] = probe_s3()
        if not payload["s3"].get("ok"):
            payload["status"] = "degraded"
    elif is_production():
        payload["status"] = "degraded"
        payload["warning"] = (
            "Local PDF storage on Railway is ephemeral. "
            "Set STORAGE_BACKEND=s3 and AWS_* or view/download will 404 after redeploy."
        )
    return payload


async def get_db() -> AsyncSession:
    async with get_session() as session:
        yield session


def _get_token_from_request(request: Request) -> Optional[str]:
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1]
    return request.cookies.get("access_token")


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = _get_token_from_request(request)
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials")
    username = payload.get("sub")
    if username is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication credentials")
    statement = select(User).where(User.username == username)
    result = await db.execute(statement)
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User account is inactive")
    return user


async def get_optional_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> Optional[User]:
    token = _get_token_from_request(request)
    if token is None:
        return None
    payload = decode_access_token(token)
    if payload is None:
        return None
    username = payload.get("sub")
    if username is None:
        return None
    statement = select(User).where(User.username == username)
    result = await db.execute(statement)
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


def require_role(user: User, allowed_roles: List[Role]) -> None:
    if user.role not in allowed_roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")


def require_meeting_organizer(user: User) -> None:
    require_role(user, [Role.admin, Role.board_secretary])


MEETING_INVITEE_ROLES = (
    Role.board_member,
    Role.board_secretary,
    Role.admin,
    Role.uploader,
)

ROLE_DISPLAY_LABELS = {
    Role.admin: "Admin",
    Role.board_secretary: "Board secretary",
    Role.uploader: "Uploader",
    Role.board_member: "Board Member",
}


def role_display_label(role: Union[Role, str]) -> str:
    if isinstance(role, Role):
        return ROLE_DISPLAY_LABELS.get(role, role.value.replace("_", " ").title())
    try:
        return ROLE_DISPLAY_LABELS.get(Role(role), str(role).replace("_", " ").title())
    except ValueError:
        return str(role).replace("_", " ").title()


templates.env.globals["role_display_label"] = role_display_label


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Any:
    return templates.TemplateResponse(request, "login.html")


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    if login_rate_limiter.is_blocked(request, username):
        return RedirectResponse(url="/login?error=locked", status_code=status.HTTP_303_SEE_OTHER)

    statement = select(User).where(User.username == username)
    result = await db.execute(statement)
    user = result.scalar_one_or_none()
    if user is None or not verify_password(password, user.hashed_password):
        login_rate_limiter.record_failure(request, username)
        return RedirectResponse(url="/login?error=invalid", status_code=status.HTTP_303_SEE_OTHER)
    if not user.is_active:
        return RedirectResponse(url="/login?error=inactive", status_code=status.HTTP_303_SEE_OTHER)

    login_rate_limiter.clear(request, username)
    token = create_access_token(subject=user.username, role=user.role.value)
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(response, token)
    set_csrf_cookie(response, new_csrf_token(), secure=cookie_secure())
    await write_audit(
        db,
        username=user.username,
        action="login",
        resource_type="session",
        resource_id=user.username,
        ip_address=_client_ip(request),
        commit=True,
    )
    return response


@app.get("/logout")
async def logout(request: Request, current_user: Optional[User] = Depends(get_optional_current_user), db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    if current_user is not None:
        await write_audit(
            db,
            username=current_user.username,
            action="logout",
            resource_type="session",
            resource_id=current_user.username,
            ip_address=_client_ip(request),
            commit=True,
        )
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("csrf_token", path="/")
    return response


@app.post("/token")
async def token(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)) -> Any:
    if login_rate_limiter.is_blocked(request, form_data.username):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts")
    statement = select(User).where(User.username == form_data.username)
    result = await db.execute(statement)
    user = result.scalar_one_or_none()
    if user is None or not verify_password(form_data.password, user.hashed_password):
        login_rate_limiter.record_failure(request, form_data.username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User account is inactive")
    login_rate_limiter.clear(request, form_data.username)
    token = create_access_token(subject=user.username, role=user.role.value)
    return {"access_token": token, "token_type": "bearer"}


@app.get("/", response_class=HTMLResponse)
async def read_root(
    request: Request,
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    if current_user is None:
        return templates.TemplateResponse(request, "login.html")
    can_upload = current_user.role in (Role.admin, Role.uploader)
    can_search = current_user.role in (Role.admin, Role.board_secretary, Role.board_member)

    document_count = await db.scalar(select(func.count()).select_from(DocumentRecord))

    next_meeting = None
    if current_user.role in (Role.admin, Role.board_secretary):
        statement = (
            select(BoardMeeting)
            .where(BoardMeeting.scheduled_at >= datetime.utcnow(), BoardMeeting.status == MeetingStatus.scheduled)
            .order_by(BoardMeeting.scheduled_at.asc())
            .limit(1)
        )
        result = await db.execute(statement)
        next_meeting = result.scalar_one_or_none()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": current_user,
            "can_upload": can_upload,
            "can_search": can_search,
            "document_count": document_count or 0,
            "next_meeting": next_meeting,
        },
    )


@app.get("/appearance", response_class=HTMLResponse)
async def appearance_page(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Any:
    return templates.TemplateResponse(
        request,
        "appearance.html",
        {"user": current_user},
    )


@app.get("/upload", response_class=HTMLResponse)
async def upload_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
) -> Any:
    require_role(current_user, [Role.admin, Role.uploader])
    today = datetime.utcnow().date()
    suggested_doc_id = await suggest_next_doc_id(db, today.year)
    document_types = await list_document_types(db)
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            "user": current_user,
            "status": status,
            "suggested_doc_id": suggested_doc_id,
            "default_doc_date": today.isoformat(),
            "document_types": document_types,
        },
    )


@app.get("/api/document-types")
async def api_document_types(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Suggested + previously used document types (dynamic catalog)."""
    require_role(current_user, [Role.admin, Role.uploader, Role.board_secretary, Role.board_member])
    types = await list_document_types(db)
    return {"types": types, "count": len(types)}


async def list_document_types(db: AsyncSession) -> List[str]:
    """Defaults plus distinct types already stored in the archive."""
    rows = (
        await db.execute(
            select(DocumentRecord.doc_type)
            .where(DocumentRecord.doc_type.is_not(None))
            .where(DocumentRecord.doc_type != "")
            .distinct()
            .order_by(DocumentRecord.doc_type.asc())
        )
    ).all()
    used = [row[0] for row in rows if row[0]]
    return merge_document_types(used)


@app.get("/api/documents/next-id")
async def api_next_document_id(
    year: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Suggest the next unique Document ID (SB-YYYY-NNN). Editable before upload."""
    require_role(current_user, [Role.admin, Role.uploader])
    resolved_year = year if year and 1990 <= year <= 2100 else datetime.utcnow().year
    doc_id = await suggest_next_doc_id(db, resolved_year)
    return {"doc_id": doc_id, "year": resolved_year}


async def suggest_next_doc_id(db: AsyncSession, year: int) -> str:
    """Next free SB-{year}-{NNN} ID based on existing archive documents."""
    prefix = BRAND.doc_id_prefix_for_year(year)
    result = await db.execute(
        select(DocumentRecord.doc_id).where(DocumentRecord.doc_id.like(f"{prefix}%"))
    )
    max_n = 0
    for (existing_id,) in result.all():
        suffix = (existing_id or "")[len(prefix) :]
        if suffix.isdigit():
            max_n = max(max_n, int(suffix))
    return f"{prefix}{max_n + 1:03d}"


@app.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    return templates.TemplateResponse(
        request,
        "search.html",
        {"user": current_user},
    )


@app.get("/api/search")
async def search_documents(
    q: str,
    lang: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Hybrid intelligent search: summary vectors + page chunks + keyword/project boosts."""
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    q = q.strip()
    lang_hint = (lang or "").strip().lower() or None
    if lang_hint not in (None, "en", "bn", "any"):
        lang_hint = None
    if not q:
        return {"results": [], "count": 0, "query": q, "mode": "hybrid"}

    cached = await get_cached_search(q, lang=lang_hint)
    if cached is not None:
        cached_results = await enrich_results_with_access(db, current_user, cached.get("results") or [])
        return {
            **cached,
            "results": cached_results,
            "count": len(cached_results),
            "cached": True,
            "mode": cached.get("mode") or "hybrid",
        }

    try:
        qdrant = make_qdrant_indexer()
        results = await asyncio.to_thread(
            qdrant.search_documents_hybrid,
            q,
            20,
            lang_hint,
            True,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Search backend unavailable") from exc

    # Promote exact / partial Document ID matches from Postgres (works even if not in top vectors).
    like = f"%{q}%"
    id_rows = (
        await db.execute(
            select(DocumentRecord.doc_id, DocumentRecord.doc_type, DocumentRecord.keywords)
            .where(DocumentRecord.doc_id.ilike(like))
            .order_by(DocumentRecord.created_at.desc())
            .limit(5)
        )
    ).all()
    by_id = {row.get("doc_id"): row for row in results if row.get("doc_id")}
    for doc_id, doc_type, keywords in id_rows:
        if doc_id in by_id:
            entry = by_id[doc_id]
            reasons = list(entry.get("match_reasons") or [])
            if "doc_id" not in reasons:
                reasons.append("doc_id")
            entry["match_reasons"] = reasons
            entry["score"] = float(entry.get("score") or 0) + 0.35
            if entry.get("source") != "hybrid":
                entry["source"] = "doc_id"
        else:
            by_id[doc_id] = {
                "doc_id": doc_id,
                "doc_type": doc_type,
                "searchable_keywords": keywords or [],
                "score": 1.2,
                "source": "doc_id",
                "match_reasons": ["doc_id"],
                "snippet": "",
            }
    results = sorted(by_id.values(), key=lambda row: (-(row.get("score") or 0), str(row.get("doc_id") or "")))[:20]

    document_ids = [row["doc_id"] for row in results if row.get("doc_id")]
    if document_ids:
        statement = select(
            DocumentRecord.doc_id,
            DocumentRecord.doc_type,
            DocumentRecord.keywords,
            DocumentRecord.doc_date,
        ).where(DocumentRecord.doc_id.in_(document_ids))
        stored_docs = await db.execute(statement)
        stored_map = {
            doc_id: (doc_type, keywords, doc_date)
            for doc_id, doc_type, keywords, doc_date in stored_docs.all()
        }
        filtered_results = []
        for row in results:
            doc_id = row.get("doc_id")
            if doc_id not in stored_map:
                continue
            doc_type, keywords, doc_date = stored_map[doc_id]
            filtered_results.append(
                {
                    **row,
                    "doc_type": doc_type or row.get("doc_type", "Document"),
                    "doc_date": doc_date.isoformat() if doc_date else row.get("doc_date"),
                    "searchable_keywords": row.get("searchable_keywords") or keywords or [],
                }
            )
    else:
        filtered_results = []

    payload = {
        "results": filtered_results,
        "count": len(filtered_results),
        "query": q,
        "mode": "hybrid",
        "lang": lang_hint or "any",
    }
    await set_cached_search(q, payload, lang=lang_hint)

    enriched = await enrich_results_with_access(db, current_user, filtered_results)
    return {"results": enriched, "count": len(enriched), "query": q, "cached": False, "mode": "hybrid", "lang": lang_hint or "any"}



@app.get("/api/search/metadata")
async def search_documents_by_metadata(
    doc_id: Optional[str] = None,
    doc_type: Optional[str] = None,
    uploaded_by: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])

    filters = {
        "doc_id": (doc_id or "").strip(),
        "doc_type": (doc_type or "").strip(),
        "uploaded_by": (uploaded_by or "").strip(),
        "date_from": date_from or "",
        "date_to": date_to or "",
    }

    cached = await get_cached_metadata_search(filters)
    if cached is not None:
        enriched = await enrich_results_with_access(db, current_user, cached.get("results") or [])
        return {**cached, "results": enriched, "count": len(enriched), "cached": True}

    conditions = []
    if filters["doc_id"]:
        conditions.append(DocumentRecord.doc_id.ilike(f"%{filters['doc_id']}%"))
    if filters["doc_type"]:
        conditions.append(DocumentRecord.doc_type.ilike(f"%{filters['doc_type']}%"))
    if filters["uploaded_by"]:
        conditions.append(DocumentRecord.uploaded_by.ilike(f"%{filters['uploaded_by']}%"))
    if filters["date_from"]:
        try:
            conditions.append(DocumentRecord.doc_date >= datetime.fromisoformat(filters["date_from"]).date())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_from, expected YYYY-MM-DD")
    if filters["date_to"]:
        try:
            conditions.append(DocumentRecord.doc_date <= datetime.fromisoformat(filters["date_to"]).date())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_to, expected YYYY-MM-DD")

    statement = select(DocumentRecord).order_by(DocumentRecord.doc_date.desc()).limit(50)
    if conditions:
        statement = statement.where(and_(*conditions))

    result = await db.execute(statement)
    documents = result.scalars().all()

    base_results = [
        {
            "doc_id": doc.doc_id,
            "doc_type": doc.doc_type,
            "doc_date": doc.doc_date.isoformat(),
            "uploaded_by": doc.uploaded_by,
            "searchable_keywords": doc.keywords,
        }
        for doc in documents
    ]
    cache_payload = {"results": base_results, "count": len(base_results)}
    await set_cached_metadata_search(filters, cache_payload)

    enriched = await enrich_results_with_access(db, current_user, base_results)
    return {"results": enriched, "count": len(enriched), "cached": False}


async def _load_graph_documents(
    db: AsyncSession,
    *,
    limit: int = 80,
    doc_ids: Optional[List[str]] = None,
    max_limit: int = 500,
) -> List[GraphDoc]:
    # Column projection keeps payload smaller than full ORM entities.
    statement = (
        select(
            DocumentRecord.doc_id,
            DocumentRecord.doc_type,
            DocumentRecord.doc_date,
            DocumentRecord.summary_json,
        )
        .order_by(DocumentRecord.created_at.desc())
    )
    if doc_ids:
        statement = statement.where(DocumentRecord.doc_id.in_(doc_ids))
    else:
        statement = statement.limit(max(1, min(limit, max_limit)))
    result = await db.execute(statement)
    rows = result.all()
    return [
        GraphDoc(
            doc_id=row.doc_id,
            doc_type=row.doc_type,
            doc_date=row.doc_date.isoformat(),
            summary_json=row.summary_json,
        )
        for row in rows
    ]


async def _find_entity_related_doc_ids(
    db: AsyncSession,
    center: GraphDoc,
    *,
    exclude: Optional[Set[str]] = None,
    limit: int = 40,
) -> List[str]:
    """Find related docs across the archive by shared keywords / summary entities.

    Uses indexed-friendly text match on keywords JSON plus a few summary terms.
    Limited result set — safe for 10k+ archives.
    """
    entities = summary_entities(center.summary_json)
    terms: List[str] = []
    for key in ("keyword", "project", "person", "organization"):
        for value in list(entities[key])[:6]:
            if value and value not in terms:
                terms.append(value)
    if not terms:
        return []

    conditions = []
    keywords_text = cast(DocumentRecord.keywords, String)
    for term in terms[:8]:
        conditions.append(keywords_text.ilike(f"%{term}%"))

    statement = (
        select(DocumentRecord.doc_id)
        .where(or_(*conditions))
        .order_by(DocumentRecord.created_at.desc())
        .limit(max(1, min(limit, 80)))
    )
    if exclude:
        statement = statement.where(DocumentRecord.doc_id.notin_(list(exclude)))
    result = await db.execute(statement)
    return [row[0] for row in result.all()]


def _graph_response(
    graph: Any,
    *,
    center_doc_id: Optional[str] = None,
    relation_guide: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    return {
        "nodes": graph.nodes,
        "edges": graph.edges,
        "count": len(graph.nodes),
        "edge_count": len(graph.edges),
        "center": center_doc_id,
        "relation_guide": relation_guide
        or {
            "semantic": "Similar meaning from document summaries (AI embeddings)",
            "project": "Same major project named in the summary",
            "person": "Same key person named in the summary",
            "organization": "Same organization in core info",
            "keyword": "Shared searchable keywords from the summary",
        },
    }


@app.get("/api/archive/documents")
async def archive_documents(
    q: Optional[str] = None,
    doc_type: Optional[str] = None,
    limit: int = 40,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Search-first document picker — paginated for large archives (10k+)."""
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    limit = max(10, min(limit, 100))
    offset = max(0, offset)
    needle = (q or "").strip()
    type_filter = (doc_type or "").strip()

    filters = []
    if needle:
        like = f"%{needle}%"
        filters.append(
            or_(
                DocumentRecord.doc_id.ilike(like),
                DocumentRecord.doc_type.ilike(like),
                cast(DocumentRecord.keywords, String).ilike(like),
            )
        )
    if type_filter:
        filters.append(DocumentRecord.doc_type == type_filter)

    count_stmt = select(func.count()).select_from(DocumentRecord)
    if filters:
        count_stmt = count_stmt.where(and_(*filters))
    total = int(await db.scalar(count_stmt) or 0)

    statement = (
        select(
            DocumentRecord.doc_id,
            DocumentRecord.doc_type,
            DocumentRecord.doc_date,
            DocumentRecord.summary_json,
        )
        .order_by(DocumentRecord.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if filters:
        statement = statement.where(and_(*filters))
    rows = (await db.execute(statement)).all()
    docs = [
        GraphDoc(
            doc_id=row.doc_id,
            doc_type=row.doc_type,
            doc_date=row.doc_date.isoformat(),
            summary_json=row.summary_json,
        )
        for row in rows
    ]
    items = [document_summary_card(doc) for doc in docs]

    type_rows = (
        await db.execute(
            select(DocumentRecord.doc_type, func.count())
            .group_by(DocumentRecord.doc_type)
            .order_by(func.count().desc())
        )
    ).all()
    types = [{"doc_type": row[0], "count": int(row[1])} for row in type_rows]

    return {
        "documents": items,
        "count": len(items),
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(items) < total,
        "types": types,
    }


@app.get("/archive/map", response_class=HTMLResponse)
async def archive_map_page(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    return templates.TemplateResponse(
        request,
        "archive_map.html",
        {"user": current_user},
    )


@app.get("/api/archive/graph")
async def archive_graph(
    limit: int = 24,
    min_similarity: float = 0.68,
    focus: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Focus-centric mind map across the full archive (scales to 10k+ docs).

    Neighbors come from:
    1) Qdrant summary similarity (semantic)
    2) Shared keywords / projects / people / org in summaries
    """
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    max_related = max(8, min(limit, 60))
    min_similarity = max(0.5, min(min_similarity, 0.99))
    focus = (focus or "").strip() or None
    if not focus:
        return _graph_response(GraphBuildResult(), center_doc_id=None)

    document = await _load_document_or_404(focus, db)
    center = GraphDoc(
        doc_id=document.doc_id,
        doc_type=document.doc_type,
        doc_date=document.doc_date.isoformat(),
        summary_json=document.summary_json,
    )

    related_ids: Set[str] = set()
    semantic_hits: List[dict] = []
    try:
        qdrant = make_qdrant_indexer()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Search backend unavailable") from exc

    semantic_task = asyncio.to_thread(
        qdrant.find_similar_documents,
        focus,
        limit=max_related,
        min_score=min_similarity,
    )
    entity_task = _find_entity_related_doc_ids(
        db,
        center,
        exclude={focus},
        limit=max_related,
    )
    try:
        semantic_hits, entity_ids = await asyncio.gather(semantic_task, entity_task)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Search backend unavailable") from exc
    for row in semantic_hits or []:
        other = row.get("doc_id")
        if other and other != focus:
            related_ids.add(str(other))
    related_ids.update(entity_ids or [])

    # Cap neighborhood size for readable maps.
    capped_ids = list(related_ids)[: max_related + 8]
    neighbors = await _load_graph_documents(db, doc_ids=capped_ids) if capped_ids else []
    docs = [center] + [d for d in neighbors if d.doc_id != focus]

    graph = build_document_graph(
        docs,
        None,
        center_doc_id=focus,
        semantic_neighbors=max_related,
        min_similarity=min_similarity,
        include_semantic=True,
        include_entities=True,
        semantic_hits=semantic_hits,
        use_provided_cluster=True,
    )
    return _graph_response(graph, center_doc_id=focus)


@app.get("/api/documents/{doc_id}/related")
async def document_related(
    doc_id: str,
    limit: int = 12,
    min_similarity: float = 0.68,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    document = await _load_document_or_404(doc_id, db)
    await assert_can_view(current_user, doc_id, db)

    limit = max(3, min(limit, 20))
    min_similarity = max(0.5, min(min_similarity, 0.99))
    center = GraphDoc(
        doc_id=document.doc_id,
        doc_type=document.doc_type,
        doc_date=document.doc_date.isoformat(),
        summary_json=document.summary_json,
    )

    related_ids: Set[str] = set()
    semantic_hits: List[dict] = []
    try:
        qdrant = make_qdrant_indexer()
        semantic_hits = qdrant.find_similar_documents(
            doc_id,
            limit=limit + 4,
            min_score=min_similarity,
        )
        for row in semantic_hits:
            other = row.get("doc_id")
            if other and other != doc_id:
                related_ids.add(str(other))
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Search backend unavailable") from exc

    entity_ids = await _find_entity_related_doc_ids(
        db, center, exclude={doc_id}, limit=limit + 8
    )
    related_ids.update(entity_ids)
    capped_ids = list(related_ids)[: limit + 8]
    neighbors = await _load_graph_documents(db, doc_ids=capped_ids) if capped_ids else []
    docs = [center] + [d for d in neighbors if d.doc_id != doc_id]

    graph = build_document_graph(
        docs,
        None,
        center_doc_id=doc_id,
        semantic_neighbors=limit,
        min_similarity=min_similarity,
        include_semantic=True,
        include_entities=True,
        semantic_hits=semantic_hits,
        use_provided_cluster=True,
    )
    return related_documents_payload(center, graph, limit=limit)


async def _load_document_or_404(doc_id: str, db: AsyncSession) -> DocumentRecord:
    statement = select(DocumentRecord).where(DocumentRecord.doc_id == doc_id)
    result = await db.execute(statement)
    document = result.scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


@app.get("/view/{doc_id}", response_class=HTMLResponse)
async def view_document(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    document = await _load_document_or_404(doc_id, db)

    try:
        await assert_can_view(current_user, doc_id, db)
    except HTTPException:
        return templates.TemplateResponse(
            request,
            "access_denied.html",
            {
                "user": current_user,
                "doc_id": doc_id,
                "doc_type": document.doc_type,
                "page_title": "Access required",
            },
            status_code=403,
        )

    can_download = is_archive_download_allowed(current_user)

    seal_text = BRAND.seal(current_user.username)
    back_url = _safe_back_url(request, fallback="/search" if current_user.role == Role.board_member else "/")
    back_label = "Back"
    if back_url.startswith("/search"):
        back_label = "Back to search"
    elif back_url.startswith("/board"):
        back_label = "Back to meetings"
    elif back_url == "/":
        back_label = "Back to home"

    await write_audit(
        db,
        username=current_user.username,
        action="view",
        resource_type="document",
        resource_id=doc_id,
        detail="viewer_page",
        ip_address=_client_ip(request),
        commit=True,
    )

    return templates.TemplateResponse(
        request,
        "viewer.html",
        {
            "user": current_user,
            "page_title": "Document Viewer",
            "page_subtitle": f"Viewing {doc_id} · {document.doc_type}",
            "file_url": f"/view/{doc_id}/file",
            "download_url": f"/download/{doc_id}" if can_download else None,
            "back_url": back_url,
            "back_label": back_label,
            "seal_text": seal_text,
            "hide_app_chrome": True,
            "can_download": can_download,
            "archive_doc_id": doc_id,
        },
    )


@app.get("/view/{doc_id}/file")
async def view_document_file(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    document = await _load_document_or_404(doc_id, db)
    await assert_can_view(current_user, doc_id, db)

    pdf_bytes = await _load_pdf_bytes(document)
    seal_text = BRAND.seal_stream(current_user.username)
    try:
        stamped = stamp_pdf_bytes(pdf_bytes, seal_text)
    except Exception as exc:
        logger.exception("Watermark stamping failed for %s", doc_id)
        raise HTTPException(status_code=502, detail="Could not prepare secure document stream") from exc

    await write_audit(
        db,
        username=current_user.username,
        action="view_file",
        resource_type="document",
        resource_id=doc_id,
        detail="watermarked_stream",
        ip_address=_client_ip(request),
        commit=True,
    )

    return Response(
        content=stamped,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{doc_id}-view.pdf"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


@app.get("/download/{doc_id}")
async def download_document(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    require_role(current_user, [Role.admin])
    document = await _load_document_or_404(doc_id, db)
    await assert_can_download(current_user, doc_id, db)

    pdf_bytes = await _load_pdf_bytes(document)
    seal_text = BRAND.seal_download(current_user.username)
    try:
        stamped = stamp_pdf_bytes(pdf_bytes, seal_text)
    except Exception as exc:
        logger.exception("Watermark stamping failed for download %s", doc_id)
        raise HTTPException(status_code=502, detail="Could not prepare watermarked download") from exc

    await write_audit(
        db,
        username=current_user.username,
        action="download",
        resource_type="document",
        resource_id=doc_id,
        detail="admin_watermarked_download",
        ip_address=_client_ip(request),
        commit=True,
    )
    return Response(
        content=stamped,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{doc_id}-watermarked.pdf"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _index_document_chunks(doc_id: str, page_payloads: List[Dict[str, Any]]) -> None:
    """Background: embed + upsert child chunks after the summary is already searchable."""
    try:
        from .pdf_parser import Document as PdfDoc

        pages = [PdfDoc(text=p["text"], metadata=p.get("metadata") or {}) for p in page_payloads if p.get("text")]
        if not pages:
            return
        qdrant = make_qdrant_indexer()
        child_documents = chunk_documents(pages, parent_doc_id=doc_id)
        if not child_documents:
            return
        chunk_records = qdrant.prepare_chunk_records(child_documents, parent_doc_id=doc_id)
        texts = [record["text"] for record in chunk_records]
        vectors: List[List[float]] = []
        batch_size = 24
        for i in range(0, len(texts), batch_size):
            vectors.extend(embed_texts(texts[i : i + batch_size]))
        qdrant.upload_chunks(chunks=chunk_records, vectors=vectors)
        logger.info("Background chunk index complete for %s (%s chunks)", doc_id, len(chunk_records))
    except Exception:
        logger.exception("Background chunk indexing failed for %s", doc_id)


def _parse_upload_keywords(keywords: str, summary_keywords: List[str]) -> List[str]:
    stored_keywords: List[str] = []
    if keywords:
        try:
            candidate = json.loads(keywords)
            if isinstance(candidate, list):
                stored_keywords = [str(item) for item in candidate]
            else:
                raise ValueError("keywords must be a JSON list")
        except ValueError:
            stored_keywords = [kw.strip() for kw in keywords.split(",") if kw.strip()]
    return stored_keywords or list(summary_keywords or [])


@app.post("/api/upload/stream")
async def upload_document_stream(
    request: Request,
    background_tasks: BackgroundTasks,
    doc_date: str = Form(...),
    doc_id: str = Form(...),
    doc_type: str = Form(...),
    keywords: str = Form(""),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """NDJSON progress stream: real % as upload → parse → summarize → index complete."""
    require_role(current_user, [Role.admin, Role.uploader])
    doc_type = normalize_document_type(doc_type)
    if not doc_type:
        async def _bad_type():
            yield json.dumps(
                {"pct": 0, "step": "upload", "title": "Upload failed", "error": "Document type is required"}
            ) + "\n"

        return StreamingResponse(_bad_type(), media_type="application/x-ndjson")

    import asyncio

    def _event(pct: int, step: str, title: str, hint: str = "", **extra: Any) -> str:
        payload = {"pct": pct, "step": step, "title": title, "hint": hint, **extra}
        return json.dumps(payload, ensure_ascii=False) + "\n"

    async def generate():
        destination = None
        try:
            yield _event(5, "upload", "Starting upload…", "Checking the file and saving it.")

            content_type = (file.content_type or "").lower()
            name = (file.filename or "").lower()
            if content_type not in ("application/pdf", "application/x-pdf", "") and not name.endswith(".pdf"):
                yield _event(0, "upload", "Upload failed", error="Only PDF files are accepted")
                return

            statement = select(DocumentRecord).where(DocumentRecord.doc_id == doc_id)
            result = await db.execute(statement)
            if result.scalar_one_or_none() is not None:
                yield _event(0, "upload", "Upload failed", error="A document with this Document ID already exists")
                return

            yield _event(12, "upload", "Receiving PDF…", "Transferring file to the server.")
            file_bytes = await file.read()
            if len(file_bytes) > 50 * 1024 * 1024:
                yield _event(0, "upload", "Upload failed", error="PDF exceeds 50MB upload limit")
                return

            safe_name = Path(file.filename or "document.pdf").name.replace("..", "")
            filename = f"{uuid.uuid4().hex}_{safe_name}"
            destination = UPLOAD_DIR / filename
            destination.write_bytes(file_bytes)
            pdf_path = str(destination.resolve())
            yield _event(22, "upload", "File saved", "PDF stored. Starting text extraction.")

            yield _event(28, "parse", "Parsing PDF…", "Extracting text from each page.")
            pages = await asyncio.to_thread(parse_pdf, pdf_path)
            if not pages:
                yield _event(0, "parse", "Parsing failed", error="Could not extract text from PDF")
                return
            yield _event(48, "parse", "Parsing complete", f"Read {len(pages)} page(s).")

            yield _event(55, "summarize", "Summarizing…", "Building bilingual keywords and an executive summary.")
            document_text = "\n\n".join(page.text for page in pages)
            if len(document_text) > 60000:
                document_text = document_text[:60000] + "\n\n[Truncated for summarization]"
            summary = await asyncio.to_thread(extract_document_summary, document_text)
            stored_keywords = _parse_upload_keywords(keywords, summary.searchable_keywords)
            yield _event(72, "summarize", "Summary ready", "Keywords and structure extracted.")

            yield _event(78, "index", "Indexing summary…", "Saving searchable summary to the archive.")
            remote_location = await asyncio.to_thread(store_pdf, pdf_path, filename)
            qdrant = make_qdrant_indexer()
            await asyncio.to_thread(qdrant.create_collections)

            summary_payload = {
                "doc_id": doc_id,
                "doc_type": doc_type,
                "doc_date": doc_date,
                "searchable_keywords": summary.searchable_keywords,
                "major_projects": [
                    project if isinstance(project, dict) else project.dict() for project in summary.major_projects
                ],
                "core_info": summary.core_info or {},
                "key_personnel": [p if isinstance(p, dict) else p.dict() for p in summary.key_personnel],
                "finance_and_admin": list(summary.finance_and_admin or []),
            }
            summary_text = build_summary_embedding_text(summary_payload)
            summary_vector = (await asyncio.to_thread(embed_texts, [summary_text]))[0]
            await asyncio.to_thread(
                qdrant.upload_summary,
                doc_id,
                summary_payload,
                summary_vector,
            )
            yield _event(92, "index", "Summary indexed", "Document is searchable. Finishing save…")

            page_payloads = [{"text": page.text, "metadata": dict(page.metadata or {})} for page in pages]
            background_tasks.add_task(_index_document_chunks, doc_id, page_payloads)

            document = DocumentRecord(
                doc_id=doc_id,
                doc_date=datetime.fromisoformat(doc_date).date(),
                doc_type=doc_type,
                keywords=stored_keywords,
                summary_json=summary_payload,
                file_location=remote_location,
                uploaded_by=current_user.username,
            )
            db.add(document)
            await write_audit(
                db,
                username=current_user.username,
                action="upload",
                resource_type="document",
                resource_id=doc_id,
                detail=doc_type,
                ip_address=_client_ip(request),
                commit=False,
            )
            await db.commit()
            await bump_search_cache_version()

            yield _event(
                100,
                "index",
                "Ingestion complete",
                "Summary is searchable now. Deeper text indexing continues briefly in the background.",
                done=True,
                redirect="/upload?status=success",
            )
        except Exception as exc:
            logger.exception("Streaming upload failed for %s", doc_id)
            if destination is not None:
                try:
                    Path(destination).unlink(missing_ok=True)
                except Exception:
                    pass
            yield _event(0, "upload", "Processing failed", error=str(exc)[:240] or "Document processing failed")

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@app.post("/upload")
async def upload_document(
    request: Request,
    background_tasks: BackgroundTasks,
    doc_date: str = Form(...),
    doc_id: str = Form(...),
    doc_type: str = Form(...),
    keywords: str = Form(""),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Fallback non-stream upload (no-JS). Prefer /api/upload/stream from the UI."""
    require_role(current_user, [Role.admin, Role.uploader])
    doc_type = normalize_document_type(doc_type)
    if not doc_type:
        raise HTTPException(status_code=400, detail="Document type is required")

    content_type = (file.content_type or "").lower()
    if content_type not in ("application/pdf", "application/x-pdf", ""):
        name = (file.filename or "").lower()
        if not name.endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    statement = select(DocumentRecord).where(DocumentRecord.doc_id == doc_id)
    result = await db.execute(statement)
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="A document with this DocId already exists")

    safe_name = Path(file.filename or "document.pdf").name.replace("..", "")
    filename = f"{uuid.uuid4().hex}_{safe_name}"
    destination = UPLOAD_DIR / filename
    file_bytes = await file.read()
    if len(file_bytes) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="PDF exceeds 50MB upload limit")
    destination.write_bytes(file_bytes)

    pdf_path = str(destination.resolve())
    try:
        pages = parse_pdf(pdf_path)
        if not pages:
            raise HTTPException(status_code=400, detail="Could not extract text from PDF")

        document_text = "\n\n".join(page.text for page in pages)
        if len(document_text) > 60000:
            document_text = document_text[:60000] + "\n\n[Truncated for summarization]"

        summary = extract_document_summary(document_text)
        stored_keywords = _parse_upload_keywords(keywords, summary.searchable_keywords)
        remote_location = store_pdf(pdf_path, filename)

        qdrant = make_qdrant_indexer()
        qdrant.create_collections()

        summary_payload = {
            "doc_id": doc_id,
            "doc_type": doc_type,
            "doc_date": doc_date,
            "searchable_keywords": summary.searchable_keywords,
            "major_projects": [project if isinstance(project, dict) else project.dict() for project in summary.major_projects],
            "core_info": summary.core_info or {},
            "key_personnel": [p if isinstance(p, dict) else p.dict() for p in summary.key_personnel],
            "finance_and_admin": list(summary.finance_and_admin or []),
        }
        summary_text = build_summary_embedding_text(summary_payload)
        summary_vector = embed_texts([summary_text])[0]
        qdrant.upload_summary(summary_id=doc_id, payload=summary_payload, vector=summary_vector)

        page_payloads = [{"text": page.text, "metadata": dict(page.metadata or {})} for page in pages]
        background_tasks.add_task(_index_document_chunks, doc_id, page_payloads)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Document processing failed for %s", doc_id)
        raise HTTPException(status_code=502, detail="Document processing failed") from exc

    document = DocumentRecord(
        doc_id=doc_id,
        doc_date=datetime.fromisoformat(doc_date).date(),
        doc_type=doc_type,
        keywords=stored_keywords,
        summary_json=summary_payload,
        file_location=remote_location,
        uploaded_by=current_user.username,
    )
    db.add(document)
    await write_audit(
        db,
        username=current_user.username,
        action="upload",
        resource_type="document",
        resource_id=doc_id,
        detail=doc_type,
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    await bump_search_cache_version()
    return RedirectResponse(url="/upload?status=success", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
    error: Optional[str] = None,
) -> Any:
    require_role(current_user, [Role.admin])

    statement = select(User).order_by(User.created_at.desc())
    result = await db.execute(statement)
    users = result.scalars().all()

    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "user": current_user,
            "users": users,
            "roles": list(Role),
            "status": status,
            "error": error,
            "form_values": {
                "username": request.query_params.get("username", ""),
                "email": request.query_params.get("email", ""),
                "role": request.query_params.get("role", ""),
            },
            "field_errors": {
                "username": request.query_params.get("err_username", ""),
                "password": request.query_params.get("err_password", ""),
                "email": request.query_params.get("err_email", ""),
                "role": request.query_params.get("err_role", ""),
            },
        },
    )


def _admin_users_error_redirect(**parts: str) -> RedirectResponse:
    from urllib.parse import urlencode

    query = urlencode({k: v for k, v in parts.items() if v})
    return RedirectResponse(url=f"/admin/users?{query}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/users")
async def admin_create_user(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    role: str = Form(""),
    email: str = Form(""),
    notifications_enabled: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])

    username = (username or "").strip()
    password = password or ""
    email = (email or "").strip()
    role_raw = (role or "").strip()
    err_username = err_password = err_email = err_role = ""

    if len(username) < 3 or len(username) > 64:
        err_username = "Enter a username between 3 and 64 characters."
    elif not all(ch.isalnum() or ch in "_-" for ch in username):
        err_username = "Use letters, numbers, hyphens, or underscores only."

    if len(password) < 8:
        err_password = "Password must be at least 8 characters."

    if email and ("@" not in email or "." not in email.split("@")[-1]):
        err_email = "Enter a valid email address, or leave it blank."

    try:
        role_value = Role(role_raw) if role_raw else None
    except ValueError:
        role_value = None
    if role_value is None:
        err_role = "Select a role for this user."

    if err_username or err_password or err_email or err_role:
        return _admin_users_error_redirect(
            error="Please fix the highlighted fields and try again.",
            username=username,
            email=email,
            role=role_raw,
            err_username=err_username,
            err_password=err_password,
            err_email=err_email,
            err_role=err_role,
        )

    statement = select(User).where(User.username == username)
    result = await db.execute(statement)
    if result.scalar_one_or_none() is not None:
        return _admin_users_error_redirect(
            error="That username is already taken. Choose another.",
            username=username,
            email=email,
            role=role_raw,
            err_username="Username already exists.",
        )

    try:
        hashed = get_password_hash(password)
    except Exception:
        logger.exception("Password hashing failed for new user %s", username)
        return _admin_users_error_redirect(
            error="Could not save this password. Try a different one.",
            username=username,
            email=email,
            role=role_raw,
            err_password="Password could not be saved. Try another password.",
        )

    new_user = User(
        username=username,
        email=email or None,
        hashed_password=hashed,
        role=role_value,
        is_active=True,
        notifications_enabled=notifications_enabled is not None,
    )
    db.add(new_user)
    await write_audit(
        db,
        username=current_user.username,
        action="user_create",
        resource_type="user",
        resource_id=username,
        detail=f"role={role_value.value}",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()

    return RedirectResponse(url="/admin/users?status=created", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/users/{user_id}/update")
async def admin_update_user(
    request: Request,
    user_id: int,
    username: str = Form(""),
    email: str = Form(""),
    role: str = Form(...),
    is_active: Optional[str] = Form(None),
    notifications_enabled: Optional[str] = Form(None),
    new_password: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    statement = select(User).where(User.id == user_id)
    result = await db.execute(statement)
    target = result.scalar_one_or_none()
    if target is None:
        return RedirectResponse(url="/admin/users?error=User+not+found", status_code=303)

    username = (username or "").strip() or target.username
    if len(username) < 3 or len(username) > 64:
        return RedirectResponse(
            url="/admin/users?error=Username+must+be+3–64+characters",
            status_code=303,
        )
    if not all(ch.isalnum() or ch in "_-" for ch in username):
        return RedirectResponse(
            url="/admin/users?error=Username+may+only+use+letters,+numbers,+hyphens,+underscores",
            status_code=303,
        )
    if username != target.username:
        taken = await db.execute(select(User).where(User.username == username, User.id != target.id))
        if taken.scalar_one_or_none() is not None:
            return RedirectResponse(url="/admin/users?error=That+username+is+already+taken", status_code=303)
        target.username = username

    try:
        target.role = Role(role.strip())
    except ValueError:
        return RedirectResponse(url="/admin/users?error=Invalid+role", status_code=303)

    email = email.strip()
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        return RedirectResponse(url="/admin/users?error=Invalid+email+address", status_code=303)
    target.email = email or None
    target.notifications_enabled = notifications_enabled is not None

    if target.id == current_user.id:
        target.is_active = True
        target.role = Role.admin
    else:
        target.is_active = is_active is not None

    if new_password.strip():
        if len(new_password.strip()) < 8:
            return RedirectResponse(
                url="/admin/users?error=New+password+must+be+at+least+8+characters",
                status_code=303,
            )
        target.hashed_password = get_password_hash(new_password.strip())

    await write_audit(
        db,
        username=current_user.username,
        action="user_update",
        resource_type="user",
        resource_id=target.username,
        detail=f"role={target.role.value};active={target.is_active}",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    return RedirectResponse(url="/admin/users?status=updated", status_code=303)


@app.post("/admin/users/{user_id}/delete")
async def admin_delete_user(
    request: Request,
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    if user_id == current_user.id:
        return RedirectResponse(url="/admin/users?error=You+cannot+delete+your+own+account", status_code=303)

    statement = select(User).where(User.id == user_id)
    result = await db.execute(statement)
    target = result.scalar_one_or_none()
    if target is None:
        return RedirectResponse(url="/admin/users?error=User+not+found", status_code=303)

    username = target.username
    await db.delete(target)
    await write_audit(
        db,
        username=current_user.username,
        action="user_delete",
        resource_type="user",
        resource_id=username,
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    return RedirectResponse(url="/admin/users?status=deleted", status_code=303)


# --- Document access requests -------------------------------------------------


@app.get("/access-requests", response_class=HTMLResponse)
async def my_access_requests_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
    error: Optional[str] = None,
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    statement = (
        select(DocumentAccessRequest)
        .where(DocumentAccessRequest.requester_username == current_user.username)
        .order_by(DocumentAccessRequest.created_at.desc())
        .limit(100)
    )
    result = await db.execute(statement)
    rows = result.scalars().all()
    return templates.TemplateResponse(
        request,
        "access_requests.html",
        {
            "user": current_user,
            "requests": rows,
            "status": status,
            "error": error,
            "modes": [AccessMode.view_only],
            "can_create_requests": not is_archive_privileged(current_user),
            "is_reviewer": current_user.role in (Role.admin, Role.board_secretary),
        },
    )


@app.post("/access-requests")
async def create_access_request(
    request: Request,
    doc_id: str = Form(...),
    purpose: str = Form(...),
    requested_mode: AccessMode = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    doc_id = doc_id.strip()
    purpose = purpose.strip()
    if not doc_id or not purpose:
        return RedirectResponse(
            url="/access-requests?error=Document+ID+and+purpose+are+required",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    await _load_document_or_404(doc_id, db)

    if is_archive_privileged(current_user):
        return RedirectResponse(
            url="/access-requests?error=Your+role+already+has+archive+view+access",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if requested_mode != AccessMode.view_only:
        return RedirectResponse(
            url="/access-requests?error=Download+requests+are+not+available",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if await has_open_pending_request(db, current_user.username, doc_id):
        return RedirectResponse(
            url="/access-requests?error=You+already+have+a+pending+request+for+this+document",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    row = DocumentAccessRequest(
        doc_id=doc_id,
        requester_username=current_user.username,
        purpose=purpose,
        requested_mode=requested_mode,
        status=AccessRequestStatus.pending,
    )
    db.add(row)
    await write_audit(
        db,
        username=current_user.username,
        action="access_request_create",
        resource_type="document",
        resource_id=doc_id,
        detail=f"mode={requested_mode.value}",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    return RedirectResponse(url="/access-requests?status=submitted", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/documents/{doc_id}/access-requests")
async def api_create_access_request(
    request: Request,
    doc_id: str,
    purpose: str = Form(...),
    requested_mode: AccessMode = Form(AccessMode.view_only),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    await _load_document_or_404(doc_id, db)
    if is_archive_privileged(current_user):
        raise HTTPException(status_code=400, detail="Your role already has archive view access")
    if requested_mode != AccessMode.view_only:
        raise HTTPException(status_code=400, detail="Download requests are not available")
    if await has_open_pending_request(db, current_user.username, doc_id):
        raise HTTPException(status_code=400, detail="Pending request already exists")
    purpose = purpose.strip()
    if not purpose:
        raise HTTPException(status_code=400, detail="Purpose is required")
    row = DocumentAccessRequest(
        doc_id=doc_id,
        requester_username=current_user.username,
        purpose=purpose,
        requested_mode=requested_mode,
        status=AccessRequestStatus.pending,
    )
    db.add(row)
    await write_audit(
        db,
        username=current_user.username,
        action="access_request_create",
        resource_type="document",
        resource_id=doc_id,
        detail=f"mode={requested_mode.value}",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    return {"ok": True, "id": row.id, "status": row.status.value}


@app.get("/admin/access-requests", response_class=HTMLResponse)
async def review_access_requests_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status_filter: Optional[str] = None,
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary])
    statement = select(DocumentAccessRequest).order_by(DocumentAccessRequest.created_at.desc()).limit(200)
    if status_filter in {s.value for s in AccessRequestStatus}:
        statement = statement.where(DocumentAccessRequest.status == AccessRequestStatus(status_filter))
    else:
        statement = statement.where(DocumentAccessRequest.status == AccessRequestStatus.pending)
    result = await db.execute(statement)
    rows = result.scalars().all()
    return templates.TemplateResponse(
        request,
        "admin_access_requests.html",
        {
            "user": current_user,
            "requests": rows,
            "status_filter": status_filter or AccessRequestStatus.pending.value,
            "statuses": list(AccessRequestStatus),
        },
    )


@app.post("/admin/access-requests/{request_id}/review")
async def review_access_request(
    request: Request,
    request_id: int,
    decision: str = Form(...),
    review_note: str = Form(""),
    grant_days: int = Form(7),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin, Role.board_secretary])
    statement = select(DocumentAccessRequest).where(DocumentAccessRequest.id == request_id)
    result = await db.execute(statement)
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Access request not found")
    if row.status != AccessRequestStatus.pending:
        return RedirectResponse(url="/admin/access-requests?status_filter=pending", status_code=303)

    decision_norm = decision.strip().lower()
    if decision_norm not in {"approve", "deny"}:
        raise HTTPException(status_code=400, detail="decision must be approve or deny")

    row.reviewed_by = current_user.username
    row.reviewed_at = datetime.utcnow()
    row.review_note = review_note.strip() or None
    if decision_norm == "approve":
        row.status = AccessRequestStatus.approved
        days = max(1, min(grant_days, 365))
        row.expires_at = default_expires_at(days)
    else:
        row.status = AccessRequestStatus.denied
        row.expires_at = None

    await write_audit(
        db,
        username=current_user.username,
        action=f"access_request_{decision_norm}",
        resource_type="document",
        resource_id=row.doc_id,
        detail=f"request_id={row.id};requester={row.requester_username};mode={row.requested_mode.value}",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    return RedirectResponse(url="/admin/access-requests", status_code=status.HTTP_303_SEE_OTHER)


async def _load_meeting_or_404(meeting_id: int, db: AsyncSession) -> BoardMeeting:
    statement = select(BoardMeeting).where(BoardMeeting.id == meeting_id)
    result = await db.execute(statement)
    meeting = result.scalar_one_or_none()
    if meeting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    return meeting


async def _require_invited_or_404(meeting_id: int, user: User, db: AsyncSession) -> BoardMeeting:
    meeting = await _load_meeting_or_404(meeting_id, db)
    statement = select(MeetingInvitation).where(
        MeetingInvitation.meeting_id == meeting_id,
        MeetingInvitation.username == user.username,
    )
    result = await db.execute(statement)
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    return meeting


async def _load_meeting_document_or_404(meeting_id: int, document_id: int, db: AsyncSession) -> MeetingDocument:
    statement = select(MeetingDocument).where(
        MeetingDocument.id == document_id,
        MeetingDocument.meeting_id == meeting_id,
    )
    result = await db.execute(statement)
    document = result.scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting document not found")
    return document


def _meeting_document_filename(document: MeetingDocument) -> str:
    location = (document.file_location or "").strip()
    basename = location.rsplit("/", 1)[-1] if location else ""
    suffix = Path(basename).suffix or ".pdf"
    safe_title = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in document.title.strip())
    safe_title = safe_title.strip("_") or f"meeting-document-{document.id}"
    return f"{safe_title}{suffix}"


def _serve_meeting_document_file(document: MeetingDocument, stamped_pdf: bytes) -> Response:
    filename = _meeting_document_filename(document)
    return Response(
        content=stamped_pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


def _validate_pdf_upload(upload: UploadFile) -> None:
    content_type = (upload.content_type or "").lower()
    name = (upload.filename or "").lower()
    if content_type not in ("application/pdf", "application/x-pdf", "") and not name.endswith(".pdf"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are allowed")
    if content_type == "" and not name.endswith(".pdf"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are allowed")


@app.get("/meetings", response_class=HTMLResponse)
async def list_meetings(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
) -> Any:
    require_meeting_organizer(current_user)

    statement = select(BoardMeeting).order_by(BoardMeeting.id.desc())
    result = await db.execute(statement)
    meetings = result.scalars().all()

    return templates.TemplateResponse(
        request,
        "meetings_list.html",
        {"user": current_user, "meetings": meetings, "status": status},
    )


@app.get("/meetings/new", response_class=HTMLResponse)
async def new_meeting_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_meeting_organizer(current_user)

    statement = (
        select(User)
        .where(User.role.in_(MEETING_INVITEE_ROLES), User.is_active.is_(True))
        .order_by(User.role, User.username)
    )
    result = await db.execute(statement)
    invite_candidates = result.scalars().all()

    invitees_by_role = []
    for role in MEETING_INVITEE_ROLES:
        members = [u for u in invite_candidates if u.role == role]
        if members:
            invitees_by_role.append(
                {"role": role, "label": role_display_label(role), "users": members}
            )

    return templates.TemplateResponse(
        request,
        "meeting_form.html",
        {
            "user": current_user,
            "invitees_by_role": invitees_by_role,
            "invite_candidates": invite_candidates,
        },
    )


@app.post("/meetings")
async def create_meeting(
    title: str = Form(..., min_length=3, max_length=200),
    scheduled_at: str = Form(...),
    location: str = Form(""),
    agenda: str = Form(""),
    invitees: List[str] = Form([]),
    notifications_enabled: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)

    try:
        meeting_datetime = datetime.fromisoformat(scheduled_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid meeting date/time")

    notify = notifications_enabled is not None
    meeting = BoardMeeting(
        title=title,
        scheduled_at=meeting_datetime,
        location=location.strip() or None,
        agenda=agenda,
        status=MeetingStatus.scheduled,
        created_by=current_user.username,
        notifications_enabled=notify,
    )
    db.add(meeting)
    await db.flush()

    invitee_usernames = {name.strip() for name in invitees if name and name.strip()}
    if invitee_usernames:
        statement = select(User).where(
            User.username.in_(invitee_usernames),
            User.role.in_(MEETING_INVITEE_ROLES),
            User.is_active.is_(True),
        )
        result = await db.execute(statement)
        invitee_users = result.scalars().all()

        ics_content = build_meeting_ics(
            meeting_uid=new_meeting_uid(meeting.id),
            title=meeting.title,
            scheduled_at=meeting.scheduled_at,
            location=meeting.location or "",
            description=meeting.agenda or "Board meeting",
        )
        google_calendar_link = build_google_calendar_link(
            title=meeting.title,
            scheduled_at=meeting.scheduled_at,
            location=meeting.location or "",
            description=meeting.agenda or "Board meeting",
        )
        for member in invitee_users:
            invitation = MeetingInvitation(meeting_id=meeting.id, username=member.username)
            db.add(invitation)
            if notify:
                sent = await notify_meeting_email(
                    db,
                    kind="invitation",
                    user=member,
                    meeting_title=meeting.title,
                    scheduled_at=meeting.scheduled_at,
                    location=meeting.location or "",
                    agenda=meeting.agenda or "",
                    ics_content=ics_content,
                    google_calendar_link=google_calendar_link,
                    meeting_id=meeting.id,
                    meeting_notifications_enabled=True,
                )
                if sent:
                    invitation.invitation_email_sent_at = datetime.utcnow()

    await db.commit()

    return RedirectResponse(url=f"/meetings/{meeting.id}?status=created", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/meetings/{meeting_id}", response_class=HTMLResponse)
async def meeting_detail(
    request: Request,
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
) -> Any:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)

    doc_statement = (
        select(MeetingDocument)
        .where(MeetingDocument.meeting_id == meeting_id)
        .order_by(MeetingDocument.uploaded_at.desc())
    )
    doc_result = await db.execute(doc_statement)
    documents = doc_result.scalars().all()

    invite_statement = (
        select(MeetingInvitation, User)
        .outerjoin(User, User.username == MeetingInvitation.username)
        .where(MeetingInvitation.meeting_id == meeting_id)
        .order_by(MeetingInvitation.username)
    )
    invite_result = await db.execute(invite_statement)
    invitations = []
    for invite, invitee_user in invite_result.all():
        invitations.append(
            {
                "username": invite.username,
                "invited_at": invite.invited_at,
                "role_label": role_display_label(invitee_user.role) if invitee_user else "User",
            }
        )

    attendance_statement = select(MeetingAttendance).where(MeetingAttendance.meeting_id == meeting_id)
    attendance_result = await db.execute(attendance_statement)
    attendance = {row.username: row for row in attendance_result.scalars().all()}

    return templates.TemplateResponse(
        request,
        "meeting_detail.html",
        {
            "user": current_user,
            "meeting": meeting,
            "documents": documents,
            "invitations": invitations,
            "attendance": attendance,
            "status": status,
        },
    )


@app.post("/meetings/{meeting_id}/attendance/open")
async def open_meeting_attendance(
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)

    meeting.attendance_open = True
    meeting.attendance_opened_at = datetime.utcnow()
    meeting.attendance_closed_at = None
    await db.commit()

    return RedirectResponse(url=f"/meetings/{meeting_id}?status=attendance_opened", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/meetings/{meeting_id}/attendance/close")
async def close_meeting_attendance(
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)

    meeting.attendance_open = False
    meeting.attendance_closed_at = datetime.utcnow()
    await db.commit()

    return RedirectResponse(url=f"/meetings/{meeting_id}?status=attendance_closed", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/meetings/{meeting_id}/attendance/print", response_class=HTMLResponse)
async def print_meeting_attendance(
    request: Request,
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)

    invite_statement = (
        select(MeetingInvitation)
        .where(MeetingInvitation.meeting_id == meeting_id)
        .order_by(MeetingInvitation.username)
    )
    invite_result = await db.execute(invite_statement)
    invitations = invite_result.scalars().all()

    attendance_statement = select(MeetingAttendance).where(MeetingAttendance.meeting_id == meeting_id)
    attendance_result = await db.execute(attendance_statement)
    attendance = {row.username: row for row in attendance_result.scalars().all()}

    return templates.TemplateResponse(
        request,
        "attendance_print.html",
        {
            "meeting": meeting,
            "invitations": invitations,
            "attendance": attendance,
            "generated_at": datetime.utcnow(),
            "generated_by": current_user.username,
        },
    )


@app.get("/meetings/{meeting_id}/attendance/{username}/signature")
async def meeting_attendance_signature(
    meeting_id: int,
    username: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    require_meeting_organizer(current_user)
    await _load_meeting_or_404(meeting_id, db)

    statement = select(MeetingAttendance).where(
        MeetingAttendance.meeting_id == meeting_id,
        MeetingAttendance.username == username,
    )
    result = await db.execute(statement)
    attendance = result.scalar_one_or_none()
    if attendance is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signature not found")

    path = UPLOAD_DIR / attendance.signature_file
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signature file not found on disk")

    return Response(
        content=path.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/meetings/{meeting_id}/agenda")
async def update_meeting_agenda(
    meeting_id: int,
    agenda: str = Form(...),
    notify_invitees: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)

    meeting.agenda = agenda

    if notify_invitees is not None and meeting.notifications_enabled:
        invites_result = await db.execute(
            select(MeetingInvitation, User)
            .join(User, User.username == MeetingInvitation.username)
            .where(MeetingInvitation.meeting_id == meeting_id)
        )
        ics_content = build_meeting_ics(
            meeting_uid=new_meeting_uid(meeting.id),
            title=meeting.title,
            scheduled_at=meeting.scheduled_at,
            location=meeting.location or "",
            description=agenda or "Board meeting",
        )
        google_calendar_link = build_google_calendar_link(
            title=meeting.title,
            scheduled_at=meeting.scheduled_at,
            location=meeting.location or "",
            description=agenda or "Board meeting",
        )
        for _invite, member in invites_result.all():
            await notify_meeting_email(
                db,
                kind="agenda_updated",
                user=member,
                meeting_title=meeting.title,
                scheduled_at=meeting.scheduled_at,
                location=meeting.location or "",
                agenda=agenda or "",
                ics_content=ics_content,
                google_calendar_link=google_calendar_link,
                meeting_id=meeting.id,
                meeting_notifications_enabled=True,
            )

    await db.commit()

    return RedirectResponse(url=f"/meetings/{meeting_id}?status=agenda_updated", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/meetings/{meeting_id}/notifications")
async def update_meeting_notifications(
    meeting_id: int,
    notifications_enabled: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)
    meeting.notifications_enabled = notifications_enabled is not None
    await db.commit()
    return RedirectResponse(
        url=f"/meetings/{meeting_id}?status=notifications_updated",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/meetings/{meeting_id}/notifications/resend")
async def resend_meeting_invitations(
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)
    if not meeting.notifications_enabled:
        return RedirectResponse(
            url=f"/meetings/{meeting_id}?status=notifications_disabled",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    invites_result = await db.execute(
        select(MeetingInvitation, User)
        .join(User, User.username == MeetingInvitation.username)
        .where(MeetingInvitation.meeting_id == meeting_id)
    )
    ics_content = build_meeting_ics(
        meeting_uid=new_meeting_uid(meeting.id),
        title=meeting.title,
        scheduled_at=meeting.scheduled_at,
        location=meeting.location or "",
        description=meeting.agenda or "Board meeting",
    )
    google_calendar_link = build_google_calendar_link(
        title=meeting.title,
        scheduled_at=meeting.scheduled_at,
        location=meeting.location or "",
        description=meeting.agenda or "Board meeting",
    )
    sent_count = 0
    for invite, member in invites_result.all():
        sent = await notify_meeting_email(
            db,
            kind="invitation",
            user=member,
            meeting_title=meeting.title,
            scheduled_at=meeting.scheduled_at,
            location=meeting.location or "",
            agenda=meeting.agenda or "",
            ics_content=ics_content,
            google_calendar_link=google_calendar_link,
            meeting_id=meeting.id,
            meeting_notifications_enabled=True,
        )
        if sent:
            invite.invitation_email_sent_at = datetime.utcnow()
            sent_count += 1
    await db.commit()
    return RedirectResponse(
        url=f"/meetings/{meeting_id}?status=invites_resent&sent={sent_count}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/notifications", response_class=HTMLResponse)
async def notifications_page(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Any:
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {"user": current_user},
    )


@app.get("/api/notifications")
async def list_notifications(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    result = await db.execute(
        select(NotificationEvent)
        .where(NotificationEvent.username == current_user.username)
        .order_by(NotificationEvent.created_at.desc())
        .limit(50)
    )
    rows = result.scalars().all()
    unread = sum(1 for r in rows if r.read_at is None)
    return {
        "unread": unread,
        "count": len(rows),
        "results": [
            {
                "id": r.id,
                "kind": r.kind,
                "title": r.title,
                "body": r.body,
                "meeting_id": r.meeting_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "read": r.read_at is not None,
            }
            for r in rows
        ],
    }


@app.post("/api/notifications/read")
async def mark_notifications_read(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    result = await db.execute(
        select(NotificationEvent).where(
            NotificationEvent.username == current_user.username,
            NotificationEvent.read_at.is_(None),
        )
    )
    now = datetime.utcnow()
    for row in result.scalars().all():
        row.read_at = now
    await db.commit()
    return {"status": "ok"}


@app.post("/meetings/{meeting_id}/documents")
async def upload_meeting_document(
    meeting_id: int,
    title: str = Form(..., min_length=1, max_length=255),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    await _load_meeting_or_404(meeting_id, db)
    _validate_pdf_upload(file)

    meeting_dir = MEETING_UPLOAD_DIR / str(meeting_id)
    meeting_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "document.pdf").name.replace("..", "")
    filename = f"{uuid.uuid4().hex}_{safe_name}"
    destination = meeting_dir / filename
    file_bytes = await file.read()
    destination.write_bytes(file_bytes)

    storage_key = f"meetings/{meeting_id}/{filename}"
    try:
        remote_location = await asyncio.to_thread(store_pdf, str(destination.resolve()), storage_key)
    except Exception as exc:
        logger.exception("Failed to store meeting document for meeting %s", meeting_id)
        raise HTTPException(status_code=502, detail="Failed to store meeting document") from exc

    document = MeetingDocument(
        meeting_id=meeting_id,
        title=title,
        file_location=remote_location,
        uploaded_by=current_user.username,
    )
    db.add(document)
    await db.commit()

    return RedirectResponse(url=f"/meetings/{meeting_id}?status=doc_uploaded", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/meetings/{meeting_id}/documents/{document_id}/file")
async def meeting_document_file(
    request: Request,
    meeting_id: int,
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    require_meeting_organizer(current_user)
    document = await _load_meeting_document_or_404(meeting_id, document_id, db)
    pdf_bytes = await _load_stored_pdf_bytes(
        document.file_location,
        resource_id=f"meeting_document:{document.id}",
    )
    seal_text = BRAND.seal_meeting_doc(current_user.username)
    try:
        stamped = stamp_pdf_bytes(pdf_bytes, seal_text)
    except Exception as exc:
        logger.exception("Watermark stamping failed for meeting document %s", document.id)
        raise HTTPException(status_code=502, detail="Could not prepare secure meeting document stream") from exc
    await write_audit(
        db,
        username=current_user.username,
        action="view_file",
        resource_type="meeting_document",
        resource_id=str(document.id),
        detail=f"meeting_id={meeting_id}",
        ip_address=_client_ip(request),
        commit=True,
    )
    return _serve_meeting_document_file(document, stamped)


@app.get("/meetings/{meeting_id}/documents/{document_id}/view", response_class=HTMLResponse)
async def meeting_document_view(
    request: Request,
    meeting_id: int,
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)
    document = await _load_meeting_document_or_404(meeting_id, document_id, db)

    seal_text = BRAND.seal(current_user.username)
    return templates.TemplateResponse(
        request,
        "viewer.html",
        {
            "user": current_user,
            "page_title": document.title,
            "page_subtitle": f"{meeting.title} · Uploaded {document.uploaded_at.strftime('%Y-%m-%d %H:%M')}",
            "file_url": f"/meetings/{meeting_id}/documents/{document_id}/file",
            "back_url": f"/meetings/{meeting_id}",
            "back_label": "Back to meeting",
            "seal_text": seal_text,
            "hide_app_chrome": True,
        },
    )


@app.get("/board/meetings", response_class=HTMLResponse)
async def board_meetings_list(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    statement = (
        select(BoardMeeting)
        .join(MeetingInvitation, MeetingInvitation.meeting_id == BoardMeeting.id)
        .where(MeetingInvitation.username == current_user.username)
        .order_by(BoardMeeting.id.desc())
    )
    result = await db.execute(statement)
    meetings = result.scalars().all()

    return templates.TemplateResponse(
        request,
        "board_meetings_list.html",
        {"user": current_user, "meetings": meetings},
    )


@app.get("/board/meetings/{meeting_id}", response_class=HTMLResponse)
async def board_meeting_detail(
    request: Request,
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
) -> Any:
    meeting = await _require_invited_or_404(meeting_id, current_user, db)

    doc_statement = (
        select(MeetingDocument)
        .where(MeetingDocument.meeting_id == meeting_id)
        .order_by(MeetingDocument.uploaded_at.desc())
    )
    doc_result = await db.execute(doc_statement)
    documents = doc_result.scalars().all()

    attendance_statement = select(MeetingAttendance).where(
        MeetingAttendance.meeting_id == meeting_id,
        MeetingAttendance.username == current_user.username,
    )
    attendance_result = await db.execute(attendance_statement)
    my_attendance = attendance_result.scalar_one_or_none()

    return templates.TemplateResponse(
        request,
        "board_meeting_detail.html",
        {
            "user": current_user,
            "meeting": meeting,
            "documents": documents,
            "my_attendance": my_attendance,
            "status": status,
        },
    )


@app.post("/board/meetings/{meeting_id}/attendance")
async def sign_meeting_attendance(
    meeting_id: int,
    signature: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    meeting = await _require_invited_or_404(meeting_id, current_user, db)

    if not meeting.attendance_open:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Attendance is not open for this meeting")

    existing_statement = select(MeetingAttendance).where(
        MeetingAttendance.meeting_id == meeting_id,
        MeetingAttendance.username == current_user.username,
    )
    existing_result = await db.execute(existing_statement)
    if existing_result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Attendance already recorded")

    try:
        _, _, b64data = signature.partition(",")
        image_bytes = base64.b64decode(b64data)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature data")

    sig_dir = MEETING_UPLOAD_DIR / str(meeting_id) / "signatures"
    sig_dir.mkdir(parents=True, exist_ok=True)
    destination = sig_dir / f"{uuid.uuid4().hex}_{current_user.username}.png"
    destination.write_bytes(image_bytes)

    db.add(MeetingAttendance(
        meeting_id=meeting_id,
        username=current_user.username,
        signature_file=str(destination.relative_to(UPLOAD_DIR)),
    ))
    await db.commit()

    return RedirectResponse(url=f"/board/meetings/{meeting_id}?status=signed", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/board/meetings/{meeting_id}/documents/{document_id}/file")
async def board_meeting_document_file(
    request: Request,
    meeting_id: int,
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await _require_invited_or_404(meeting_id, current_user, db)
    document = await _load_meeting_document_or_404(meeting_id, document_id, db)
    pdf_bytes = await _load_stored_pdf_bytes(
        document.file_location,
        resource_id=f"meeting_document:{document.id}",
    )
    seal_text = BRAND.seal_meeting_doc(current_user.username)
    try:
        stamped = stamp_pdf_bytes(pdf_bytes, seal_text)
    except Exception as exc:
        logger.exception("Watermark stamping failed for board meeting document %s", document.id)
        raise HTTPException(status_code=502, detail="Could not prepare secure meeting document stream") from exc
    await write_audit(
        db,
        username=current_user.username,
        action="view_file",
        resource_type="meeting_document",
        resource_id=str(document.id),
        detail=f"meeting_id={meeting_id}",
        ip_address=_client_ip(request),
        commit=True,
    )
    return _serve_meeting_document_file(document, stamped)


@app.get("/board/meetings/{meeting_id}/documents/{document_id}/view", response_class=HTMLResponse)
async def board_meeting_document_view(
    request: Request,
    meeting_id: int,
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    meeting = await _require_invited_or_404(meeting_id, current_user, db)
    document = await _load_meeting_document_or_404(meeting_id, document_id, db)

    seal_text = BRAND.seal(current_user.username)
    return templates.TemplateResponse(
        request,
        "viewer.html",
        {
            "user": current_user,
            "page_title": document.title,
            "page_subtitle": f"{meeting.title} · Uploaded {document.uploaded_at.strftime('%Y-%m-%d %H:%M')}",
            "file_url": f"/board/meetings/{meeting_id}/documents/{document_id}/file",
            "back_url": f"/board/meetings/{meeting_id}",
            "back_label": "Back to meeting",
            "seal_text": seal_text,
            "hide_app_chrome": True,
        },
    )
