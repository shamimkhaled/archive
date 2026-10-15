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

from ..access_control import assert_can_view, viewable_doc_ids, write_audit
from ..brand import BRAND
from ..config import is_production
from ..document_types import normalize_document_type
from ..deps import _client_ip, _load_document_or_404, _load_stored_pdf_bytes, get_current_user, get_db, require_role, templates
from ..jobs.outbox import notify_outbox
from ..models import DocumentRecord, DocumentVersion, Role, User
from ..pdf_delivery import is_initial_request, pdf_response, stamped_pdf
from ..services.document_versions import (
    LANGUAGE_CHOICES,
    METADATA_FIELDS,
    clean_metadata_value,
    list_versions,
    version_payload,
)
from ..services.metadata_suggest import REASON_CODES, REASON_LABELS, field_min_confidence, low_confidence_fields
from ..services.ingest import (
    INGEST_INDEXED,
    INGEST_QUEUED,
    INGEST_REJECTED,
    VERSION_PENDING_REVIEW,
    VERSION_REJECTED,
    VERSION_REVIEWED,
    load_pages,
    refresh_search_payload,
)
from ..services.taxonomy import confirm_document_tags, document_tags, parse_tag_input, set_document_tags
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


def _own_upload(user: User, document: DocumentRecord) -> bool:
    return four_eyes_required() and bool(document.uploaded_by) and document.uploaded_by == user.username


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
            "review_reasons": review_reason_labels(doc.suggested_metadata),
            "review_reason_codes": review_reason_codes(doc.suggested_metadata),
            "page_count": doc.page_count,
            "uploaded_by": ver.created_by,
            "uploaded_at": ver.created_at.isoformat() if ver.created_at else None,
            "is_new_version": ver.version > 1,
        }
        for ver, doc in rows
    ]


def ai_filled_fields(document: DocumentRecord) -> List[str]:
    return [f for f in (document.suggested_metadata or {}).get("_applied") or [] if isinstance(f, str)]


def field_label(field: str) -> str:
    return FIELD_LABELS.get(field) or ("Tags" if field == "keywords" else field.replace("_", " ").capitalize())


async def metadata_to_confirm(db: AsyncSession, user: User, limit: int = 200) -> List[Dict[str, Any]]:
    """Indexed documents whose AI-filled details no person has confirmed yet, oldest first."""
    rows = (
        await db.execute(
            select(DocumentRecord)
            .where(DocumentRecord.metadata_check_pending.is_(True))
            .order_by(DocumentRecord.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    visible = await viewable_doc_ids(db, user, [doc.doc_id for doc in rows])
    return [
        {
            "doc_id": doc.doc_id,
            "title": doc.title,
            "doc_type": doc.doc_type,
            "sensitivity": doc.sensitivity,
            "uploaded_by": doc.uploaded_by,
            "uploaded_at": doc.created_at.isoformat() if doc.created_at else None,
            "ai_filled": [field_label(f) for f in ai_filled_fields(doc)],
            "low_fields": [field_label(f) for f in low_confidence_fields(doc.metadata_confidence)],
        }
        for doc in rows
        if doc.doc_id in visible
    ]


def review_reason_codes(suggested: Optional[Dict[str, Any]]) -> List[str]:
    return list((suggested or {}).get("_review_reasons") or [])


def review_reason_labels(suggested: Optional[Dict[str, Any]]) -> List[str]:
    return [REASON_LABELS.get(r, r) for r in review_reason_codes(suggested)]


def _differing_suggestions(document: DocumentRecord, suggested: Dict[str, Any]) -> Dict[str, str]:
    """AI suggestions that differ from a value someone already entered, offered as "Use this"."""
    out: Dict[str, str] = {}
    for field in METADATA_FIELDS:
        value = suggested.get(field)
        current = getattr(document, field)
        if isinstance(value, str) and value.strip() and current and value.strip() != current.strip():
            out[field] = value.strip()
    return out


FIELD_LABELS = {
    "title": "Title",
    "reference_number": "Reference number",
    "department": "Department",
    "subject": "Subject",
    "category": "Category",
    "language": "Language",
    "doc_type": "Type of document",
    "doc_date": "Document date",
}


def _metadata_review_context(document: DocumentRecord) -> Dict[str, Any]:
    """Per-field confidence, the fields below the bar (shown first), and the field order for the form."""
    confidence = dict(document.metadata_confidence or {})
    low = low_confidence_fields(confidence)
    ordered = sorted(METADATA_FIELDS, key=lambda f: (f not in low, METADATA_FIELDS.index(f)))
    return {
        "confidence": confidence,
        "low_fields": low,
        "ordered_fields": ordered,
        "min_confidence": field_min_confidence(),
    }


def _apply_metadata_form(document: DocumentRecord, form: Any) -> List[str]:
    """Write the metadata fields and type from a submitted form. Returns the fields that changed."""
    changed = []
    for field in METADATA_FIELDS:
        if field not in form:
            continue
        value = clean_metadata_value(field, form.get(field))
        if value != getattr(document, field):
            changed.append(field)
        setattr(document, field, value)
    new_type = normalize_document_type(str(form.get("doc_type") or ""))
    if new_type and new_type != document.doc_type:
        changed.append("doc_type")
        document.doc_type = new_type
    return changed


def _mark_confirmed(document: DocumentRecord, username: str, now: datetime) -> None:
    """After a person confirms the details, every filled field is as certain as a typed value."""
    confirmed = {f: 1.0 for f in METADATA_FIELDS if getattr(document, f, None)}
    confirmed.update(doc_type=1.0, doc_date=1.0)
    document.metadata_confidence = confirmed
    document.metadata_confirmed_by = username
    document.metadata_confirmed_at = now
    document.metadata_check_pending = False


def _source_label(source: Optional[Dict[str, Any]]) -> Optional[str]:
    if not source:
        return None
    if source.get("kind") == "ai":
        return f"AI ({source.get('provider') or '?'} / {source.get('model') or '?'})"
    return "rules (AI unavailable)"


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
    details = await metadata_to_confirm(db, current_user) if current_user.role in METADATA_EDITOR_ROLES else []
    return {"items": await pending_queue(db), "metadata_to_confirm": details, "four_eyes": four_eyes_required()}


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
        "suggested_source": _source_label(suggested.get("_source")),
        "suggested": {field: suggested.get(field) for field in ("doc_type", "doc_date", "keywords") if suggested.get(field)},
        "confidence": dict(document.metadata_confidence or {}),
        "low_confidence_fields": low_confidence_fields(document.metadata_confidence),
        "tags": await document_tags(db, doc_id),
        "review_reasons": review_reason_labels(suggested),
        "review_reason_codes": review_reason_codes(suggested),
        "differing_suggestions": _differing_suggestions(document, suggested),
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
    items = await pending_queue(db)
    reason = request.query_params.get("reason") or ""
    reason_counts = {code: sum(1 for it in items if code in it["review_reason_codes"]) for code in REASON_CODES}
    if reason in REASON_CODES:
        items = [it for it in items if reason in it["review_reason_codes"]]
    else:
        reason = ""
    can_confirm_details = current_user.role in METADATA_EDITOR_ROLES
    return templates.TemplateResponse(
        request,
        "review_queue.html",
        {
            "user": current_user,
            "page_title": "Verification queue",
            "items": items,
            "can_confirm_details": can_confirm_details,
            "details": await metadata_to_confirm(db, current_user) if can_confirm_details else [],
            "list_view": "details" if can_confirm_details and request.query_params.get("list") == "details" else "publish",
            "reason": reason,
            "reason_counts": {code: n for code, n in reason_counts.items() if n},
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
    tags = await document_tags(db, doc_id)
    if not tags and suggested.get("keywords"):
        tags = [{"label": k, "confirmed": False} for k in suggested["keywords"]]
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
            "tags": tags,
            "field_labels": FIELD_LABELS,
            **_metadata_review_context(document),
            "suggested_source": (suggested.get("_source") or {}).get("kind"),
            "suggested_type": suggested.get("doc_type") or "",
            "review_reasons": review_reason_labels(suggested),
            "review_reason_codes": review_reason_codes(suggested),
            "differing_suggestions": _differing_suggestions(document, suggested),
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

    low_before = low_confidence_fields(document.metadata_confidence)
    changed = _apply_metadata_form(document, form)
    if "tags" in form:
        before = list(document.keywords or [])
        await set_document_tags(
            db, document, parse_tag_input(str(form.get("tags") or "")), source="reviewer", confirmed=True, username=current_user.username
        )
        if [k.casefold() for k in before] != [k.casefold() for k in document.keywords or []]:
            changed.append("tags")
    else:
        await confirm_document_tags(db, doc_id)
    _mark_confirmed(document, current_user.username, now)
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
    corrected = [f for f in low_before if f in changed]
    await _audit(
        db,
        request,
        current_user,
        "metadata_confirm",
        doc_id,
        f"changed={','.join(changed) or 'none'} low={','.join(low_before) or 'none'} low_corrected={','.join(corrected) or 'none'}",
    )
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


METADATA_EDITOR_ROLES = [Role.admin, Role.board_secretary]


@router.get("/documents/{doc_id}/metadata", response_class=HTMLResponse)
async def document_metadata_page(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Correct an archived document's details without re-running OCR."""
    require_role(current_user, METADATA_EDITOR_ROLES)
    document = await _load_document_or_404(doc_id, db)
    await assert_can_view(current_user, doc_id, db)
    return templates.TemplateResponse(
        request,
        "document_metadata.html",
        {
            "user": current_user,
            "page_title": f"Details of {doc_id}",
            "document": document,
            "metadata": {field: getattr(document, field) or "" for field in METADATA_FIELDS},
            "tags": await document_tags(db, doc_id),
            "field_labels": FIELD_LABELS,
            "language_choices": LANGUAGE_CHOICES,
            **_metadata_review_context(document),
            "check_pending": document.metadata_check_pending,
            "ai_filled": ai_filled_fields(document) if document.metadata_check_pending else [],
            "own_upload": _own_upload(current_user, document),
            "status": request.query_params.get("status") or "",
        },
    )


@router.post("/documents/{doc_id}/metadata")
async def document_metadata_save(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, METADATA_EDITOR_ROLES)
    document = await _load_document_or_404(doc_id, db)
    await assert_can_view(current_user, doc_id, db)
    form = await request.form()
    changed = _apply_metadata_form(document, form)
    raw_date = str(form.get("doc_date") or "").strip()
    if raw_date:
        try:
            new_date = datetime.fromisoformat(raw_date).date()
        except ValueError:
            return RedirectResponse(url=f"/documents/{quote(doc_id)}/metadata?status=bad_date", status_code=303)
        if new_date != document.doc_date:
            changed.append("doc_date")
            document.doc_date = new_date
    if "tags" in form:
        before = [k.casefold() for k in document.keywords or []]
        await set_document_tags(
            db, document, parse_tag_input(str(form.get("tags") or "")), source="reviewer", confirmed=True, username=current_user.username
        )
        if before != [k.casefold() for k in document.keywords or []]:
            changed.append("tags")
    was_pending = document.metadata_check_pending
    detail = f"changed={','.join(changed) or 'none'}"
    if was_pending and _own_upload(current_user, document):
        document.metadata_confidence = {**(document.metadata_confidence or {}), **{f: 1.0 for f in changed if f != "tags"}}
        status, action, confirmed = "own_upload", "metadata_edit", False
    elif was_pending:
        _mark_confirmed(document, current_user.username, datetime.utcnow())
        detail += f" ai_filled={','.join(ai_filled_fields(document)) or 'none'}"
        status, action, confirmed = "confirmed", "metadata_confirm", True
    else:
        _mark_confirmed(document, current_user.username, datetime.utcnow())
        status, action, confirmed = "saved", "metadata_edit", False
    await _audit(db, request, current_user, action, doc_id, detail)
    await db.commit()
    if changed or confirmed:
        from ..cache import bump_search_cache_version

        if changed:
            await refresh_search_payload(document)
        await bump_search_cache_version()
    return RedirectResponse(url=f"/documents/{quote(doc_id)}/metadata?status={status}", status_code=303)
