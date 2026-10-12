"""OCR verification queue and document version history.

A version whose OCR confidence is below threshold waits here. The reviewer sees
each page image beside its extracted text, corrects the text, confirms the
archive metadata, and approves (which indexes the corrected text) or rejects.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import assert_can_view, write_audit
from ..brand import BRAND
from ..config import is_production
from ..deps import _client_ip, _load_document_or_404, _load_stored_pdf_bytes, get_current_user, get_db, require_role, templates
from ..jobs.outbox import notify_outbox
from ..models import DocumentRecord, DocumentVersion, Role, User
from ..pdf_delivery import is_initial_request, pdf_response, stamped_pdf
from ..services.document_versions import METADATA_FIELDS, list_versions, version_payload
from ..services.ingest import (
    INGEST_INDEXED,
    INGEST_QUEUED,
    INGEST_REJECTED,
    VERSION_PENDING_REVIEW,
    VERSION_REJECTED,
    VERSION_REVIEWED,
    load_pages,
)
from .ingest import _run_inline, _schedule

logger = logging.getLogger("bcp_project.routers.review")
router = APIRouter()

REVIEWER_ROLES = [Role.admin, Role.board_secretary, Role.uploader]
MAX_PAGE_TEXT = 200_000


def four_eyes_required() -> bool:
    configured = (os.getenv("OCR_REVIEW_FOUR_EYES") or "").strip().lower()
    if configured in {"1", "true", "yes", "on"}:
        return True
    if configured in {"0", "false", "no", "off"}:
        return False
    return is_production()


def _four_eyes_problem(user: User, ver: DocumentVersion) -> Optional[str]:
    if four_eyes_required() and ver.created_by and ver.created_by == user.username:
        return "Another person must verify a document you uploaded."
    return None


async def pending_queue(db: AsyncSession, limit: int = 200) -> List[Dict[str, Any]]:
    rows = (
        await db.execute(
            select(DocumentVersion, DocumentRecord)
            .join(DocumentRecord, DocumentRecord.doc_id == DocumentVersion.doc_id)
            .where(DocumentVersion.status == VERSION_PENDING_REVIEW)
            .order_by(DocumentVersion.created_at.asc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "doc_id": doc.doc_id,
            "version": ver.version,
            "doc_type": doc.doc_type,
            "title": doc.title or (doc.suggested_metadata or {}).get("title"),
            "sensitivity": doc.sensitivity,
            "ocr_confidence": doc.ocr_confidence,
            "page_count": doc.page_count,
            "uploaded_by": ver.created_by,
            "uploaded_at": ver.created_at.isoformat() if ver.created_at else None,
            "is_new_version": ver.version > 1,
        }
        for ver, doc in rows
    ]


async def _pending_version(db: AsyncSession, doc_id: str, version: Optional[int]) -> DocumentVersion:
    stmt = select(DocumentVersion).where(DocumentVersion.doc_id == doc_id)
    if version is not None:
        stmt = stmt.where(DocumentVersion.version == version)
    else:
        stmt = stmt.where(DocumentVersion.status == VERSION_PENDING_REVIEW)
    row = (await db.execute(stmt.order_by(DocumentVersion.version.desc()).limit(1))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No version awaiting verification")
    return row


async def _audit(db: AsyncSession, request: Request, user: User, action: str, doc_id: str, detail: str) -> None:
    await write_audit(
        db,
        username=user.username,
        action=action,
        resource_type="document",
        resource_id=doc_id,
        detail=detail[:500],
        ip_address=_client_ip(request),
        commit=False,
    )


@router.get("/api/review")
async def api_review_queue(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    require_role(current_user, REVIEWER_ROLES)
    return {"items": await pending_queue(db), "four_eyes": four_eyes_required()}


@router.get("/api/review/{doc_id}")
async def api_review_detail(
    request: Request,
    doc_id: str,
    v: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """What the verification screen needs: OCR text per page, metadata to confirm, and whether this user may act."""
    require_role(current_user, REVIEWER_ROLES)
    document = await _load_document_or_404(doc_id, db)
    ver = await _pending_version(db, doc_id, v)
    pages = await load_pages(db, doc_id, ver.version)
    suggested = dict(document.suggested_metadata or {})
    await _audit(db, request, current_user, "verify_open", doc_id, f"v{ver.version}")
    await db.commit()
    return {
        "doc_id": doc_id,
        "version": ver.version,
        "status": ver.status,
        "doc_type": document.doc_type,
        "sensitivity": document.sensitivity,
        "ocr_confidence": document.ocr_confidence,
        "uploaded_by": ver.created_by,
        "fields": list(METADATA_FIELDS),
        "metadata": {field: getattr(document, field) or suggested.get(field) or "" for field in METADATA_FIELDS},
        "suggested_source": suggested.get("_source"),
        "pages": [
            {"page": p.page_number, "text": p.text or "", "ocr_confidence": p.ocr_confidence, "source": p.source}
            for p in pages
        ],
        "four_eyes_problem": _four_eyes_problem(current_user, ver),
        "can_act": ver.status == VERSION_PENDING_REVIEW,
    }


@router.get("/review", response_class=HTMLResponse)
async def review_queue_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, REVIEWER_ROLES)
    return templates.TemplateResponse(
        request,
        "review_queue.html",
        {
            "user": current_user,
            "page_title": "Verification queue",
            "items": await pending_queue(db),
            "four_eyes": four_eyes_required(),
            "status": request.query_params.get("status"),
        },
    )


@router.get("/review/{doc_id}", response_class=HTMLResponse)
async def review_detail_page(
    request: Request,
    doc_id: str,
    v: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, REVIEWER_ROLES)
    document = await _load_document_or_404(doc_id, db)
    ver = await _pending_version(db, doc_id, v)
    pages = await load_pages(db, doc_id, ver.version)
    suggested = dict(document.suggested_metadata or {})
    metadata = {field: getattr(document, field) or suggested.get(field) or "" for field in METADATA_FIELDS}
    await _audit(db, request, current_user, "verify_open", doc_id, f"v{ver.version}")
    await db.commit()
    return templates.TemplateResponse(
        request,
        "review_detail.html",
        {
            "user": current_user,
            "page_title": f"Verify {doc_id}",
            "document": document,
            "version": ver,
            "pages": pages,
            "metadata": metadata,
            "suggested_source": suggested.get("_source"),
            "fields": METADATA_FIELDS,
            "four_eyes_problem": _four_eyes_problem(current_user, ver),
            "can_act": ver.status == VERSION_PENDING_REVIEW,
            "error": request.query_params.get("error"),
        },
    )


@router.get("/review/{doc_id}/v/{version}/page/{page}.png")
async def review_page_image(
    doc_id: str,
    version: int,
    page: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    require_role(current_user, REVIEWER_ROLES)
    from ..ocr import render_page_png

    ver = await _pending_version(db, doc_id, version)
    data = await _load_stored_pdf_bytes(ver.file_location, resource_id=doc_id)
    try:
        png = await asyncio.to_thread(render_page_png, data, page)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Page could not be rendered") from exc
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/review/{doc_id}/v/{version}/approve")
async def review_approve(
    request: Request,
    doc_id: str,
    version: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, REVIEWER_ROLES)
    document = await _load_document_or_404(doc_id, db)
    ver = await _pending_version(db, doc_id, version)
    if ver.status != VERSION_PENDING_REVIEW:
        raise HTTPException(status_code=409, detail=f"Version is {ver.status}, not awaiting review")
    problem = _four_eyes_problem(current_user, ver)
    if problem:
        return RedirectResponse(url=f"/review/{doc_id}?v={version}&error={quote(problem)}", status_code=303)

    form = await request.form()
    now = datetime.utcnow()
    edited = 0
    for page in await load_pages(db, doc_id, version):
        field = f"page_{page.page_number}"
        if field not in form:
            continue
        text = str(form.get(field) or "").replace("\r\n", "\n")[:MAX_PAGE_TEXT]
        if text != (page.text or ""):
            page.text = text
            page.source = "reviewed"
            page.edited_by = current_user.username
            page.edited_at = now
            edited += 1

    changed = []
    for field in METADATA_FIELDS:
        value = str(form.get(field) or "").strip()[:500] or None
        if value != getattr(document, field):
            changed.append(field)
        setattr(document, field, value)
    document.metadata_confirmed_by = current_user.username
    document.metadata_confirmed_at = now
    document.reviewed_by = current_user.username
    document.reviewed_at = now
    ver.status = VERSION_REVIEWED
    ver.reviewed_by = current_user.username
    ver.reviewed_at = now
    document.ingest_status = INGEST_QUEUED
    document.ingest_error = None
    document.ingest_updated_at = now
    job_id = _schedule(db, doc_id, version)

    await _audit(db, request, current_user, "verify_approve", doc_id, f"v{version} pages_edited={edited}")
    await _audit(db, request, current_user, "metadata_confirm", doc_id, f"changed={','.join(changed) or 'none'}")
    await db.commit()
    if job_id:
        notify_outbox()
    else:
        background_tasks.add_task(_run_inline, doc_id, version)
    return RedirectResponse(url="/review?status=approved", status_code=303)


@router.post("/review/{doc_id}/v/{version}/reject")
async def review_reject(
    request: Request,
    doc_id: str,
    version: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, REVIEWER_ROLES)
    document = await _load_document_or_404(doc_id, db)
    ver = await _pending_version(db, doc_id, version)
    if ver.status != VERSION_PENDING_REVIEW:
        raise HTTPException(status_code=409, detail=f"Version is {ver.status}, not awaiting review")
    problem = _four_eyes_problem(current_user, ver)
    if problem:
        return RedirectResponse(url=f"/review/{doc_id}?v={version}&error={quote(problem)}", status_code=303)
    form = await request.form()
    reason = str(form.get("reason") or "").strip()[:500] or "Rejected at verification"
    now = datetime.utcnow()
    ver.status = VERSION_REJECTED
    ver.reviewed_by = current_user.username
    ver.reviewed_at = now
    ver.note = reason
    if document.summary_json and document.current_version != version:
        # An earlier approved version stays the searchable one.
        document.ingest_status = INGEST_INDEXED
        document.ingest_error = None
    else:
        document.verification_status = "rejected"
        document.ingest_status = INGEST_REJECTED
        document.ingest_error = reason
    document.reviewed_by = current_user.username
    document.reviewed_at = now
    document.ingest_updated_at = now
    await _audit(db, request, current_user, "verify_reject", doc_id, f"v{version} {reason}")
    await db.commit()
    return RedirectResponse(url="/review?status=rejected", status_code=303)


async def _version_or_404(db: AsyncSession, doc_id: str, version: int) -> DocumentVersion:
    row = (
        await db.execute(
            select(DocumentVersion).where(DocumentVersion.doc_id == doc_id, DocumentVersion.version == version)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return row


@router.get("/view/{doc_id}/versions/{version}/file")
async def view_version_file(
    request: Request,
    doc_id: str,
    version: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Watermarked stream of one version (the PDF rendition for image uploads)."""
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member, Role.uploader])
    document = await _load_document_or_404(doc_id, db)
    if current_user.role == Role.uploader and document.uploaded_by != current_user.username:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    await assert_can_view(current_user, doc_id, db)
    row = await _version_or_404(db, doc_id, version)
    if current_user.role == Role.board_member and row.version != document.current_version:
        raise HTTPException(status_code=403, detail="Only the current version is shared with board members")
    key = ("version", doc_id, version, row.file_location, current_user.username)
    try:
        stamped = await stamped_pdf(
            key,
            lambda: _load_stored_pdf_bytes(row.file_location, resource_id=doc_id),
            BRAND.seal_stream(current_user.username),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Watermark stamping failed for %s v%s", doc_id, version)
        raise HTTPException(status_code=502, detail="Could not prepare secure document stream") from exc
    if is_initial_request(request):
        await write_audit(
            db,
            username=current_user.username,
            action="view_file",
            resource_type="document",
            resource_id=doc_id,
            detail=f"watermarked_stream v{version}",
            ip_address=_client_ip(request),
            commit=True,
        )
    return pdf_response(request, stamped, filename=f"{doc_id}-v{version}-view.pdf")


@router.get("/documents/{doc_id}/versions", response_class=HTMLResponse)
async def document_versions_page(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.uploader])
    document = await _load_document_or_404(doc_id, db)
    rows = await list_versions(db, doc_id)
    return templates.TemplateResponse(
        request,
        "document_versions.html",
        {
            "user": current_user,
            "page_title": f"Versions of {doc_id}",
            "document": document,
            "versions": [version_payload(r, current=document.current_version) for r in rows],
            "can_upload": current_user.role == Role.admin or document.uploaded_by == current_user.username,
            "can_verify": current_user.role in {Role.admin, Role.board_secretary},
        },
    )
