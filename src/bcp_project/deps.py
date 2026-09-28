"""Shared FastAPI dependencies and helpers for routers."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import ACCESS_TOKEN_EXPIRE_MINUTES, decode_access_token
from .aws_utils import get_s3_client, storage_backend_name
from .brand import BRAND
from .config import cookie_secure, load_environment
from .db import get_session
from .jobs.handlers import JOB_CHUNK_INDEX, run_chunk_index
from .jobs.queue import enqueue_job, job_queue_enabled
from .models import DocumentRecord, Role, User
from .pdf_parser import parse_pdf

logger = logging.getLogger("bcp_project.deps")

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

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _compute_static_version() -> str:
    candidates = [
        STATIC_DIR / "css" / "style.css",
        STATIC_DIR / "js" / "app.js",
        STATIC_DIR / "js" / "sw.js",
        STATIC_DIR / "js" / "archive-graph.js",
        STATIC_DIR / "js" / "meeting-transcription.js",
        STATIC_DIR / "manifest.webmanifest",
    ]
    mtimes = [p.stat().st_mtime for p in candidates if p.exists()]
    return str(int(max(mtimes))) if mtimes else "1"


STATIC_VERSION = _compute_static_version()
templates.env.globals["static_version"] = STATIC_VERSION
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


async def _load_document_or_404(doc_id: str, db: AsyncSession) -> DocumentRecord:
    statement = select(DocumentRecord).where(DocumentRecord.doc_id == doc_id)
    result = await db.execute(statement)
    document = result.scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


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

def _upsert_document_chunks(doc_id: str, page_payloads: List[Dict[str, Any]]) -> int:
    """Embed OCR/extracted page text and upsert into Qdrant document_chunks."""
    return run_chunk_index(doc_id, page_payloads)


def _index_document_chunks(doc_id: str, page_payloads: List[Dict[str, Any]]) -> None:
    """Background: embed + upsert child chunks after the summary is already searchable."""
    try:
        _upsert_document_chunks(doc_id, page_payloads)
    except Exception:
        logger.exception("Background chunk indexing failed for %s", doc_id)


async def schedule_chunk_index(
    doc_id: str,
    page_payloads: List[Dict[str, Any]],
    background_tasks: BackgroundTasks,
) -> None:
    """Prefer durable Redis queue when enabled; fall back to in-process BackgroundTasks."""
    if job_queue_enabled():
        job_id = await enqueue_job(
            JOB_CHUNK_INDEX,
            {"doc_id": doc_id, "page_payloads": page_payloads},
        )
        if job_id:
            logger.info("Queued chunk_index job %s for %s", job_id, doc_id)
            return
        logger.warning("Job enqueue failed for %s; falling back to BackgroundTasks", doc_id)
    background_tasks.add_task(_index_document_chunks, doc_id, page_payloads)


def reindex_chunks_from_pdf_bytes(doc_id: str, pdf_bytes: bytes) -> int:
    """Re-parse a stored PDF and rebuild page-text chunk vectors."""
    import tempfile

    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name
        pages = parse_pdf(tmp_path)
        page_payloads = [
            {"text": page.text, "metadata": dict(page.metadata or {})}
            for page in pages
            if getattr(page, "text", None)
        ]
        return _upsert_document_chunks(doc_id, page_payloads)
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


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
