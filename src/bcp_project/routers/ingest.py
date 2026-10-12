"""Asynchronous document intake (202 + status polling), versions, and ingest retry."""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import assert_can_view, write_audit
from ..deps import _client_ip, _load_document_or_404, _load_stored_pdf_bytes, _parse_upload_keywords, get_current_user, get_db, require_role
from ..rate_limit import rate_limit
from ..document_types import normalize_document_type
from ..intake import IntakeRejected, WriteOnceViolation, sha256_hex, validate_upload
from ..jobs.outbox import notify_outbox, stage_job
from ..jobs.queue import job_queue_enabled
from ..models import DocumentRecord, DocumentVersion, JobRun, Role, User, parse_sensitivity
from ..services.document_versions import (
    IntakeResult,
    intake_new_document,
    intake_new_version,
    list_versions,
    version_payload,
)
from ..services.ingest import (
    INGEST_FAILED,
    INGEST_QUEUED,
    JOB_DOCUMENT_INGEST,
    ingest_status_payload,
    run_document_ingest,
)
from ..services.retention import doc_id_conflict

logger = logging.getLogger("bcp_project.routers.ingest")
router = APIRouter()


def ingest_mode() -> str:
    """``async`` = 202 + worker pipeline; ``stream`` = legacy in-request NDJSON pipeline."""
    configured = (os.getenv("INGEST_MODE") or "").strip().lower()
    if configured in {"async", "stream"}:
        return configured
    return "async" if job_queue_enabled() else "stream"


async def _run_inline(doc_id: str, version: Optional[int] = None) -> None:
    try:
        await run_document_ingest(doc_id, version=version)
    except Exception:
        logger.exception("Inline ingest failed for %s", doc_id)


def _schedule(db: AsyncSession, doc_id: str, version: Optional[int] = None) -> Optional[str]:
    """Stage the ingest job in the caller's transaction when the queue is on."""
    if not job_queue_enabled():
        return None
    payload: Dict[str, Any] = {"doc_id": doc_id}
    if version is not None:
        payload["version"] = version
    return stage_job(db, JOB_DOCUMENT_INGEST, payload, resource_type="document", resource_id=doc_id)


def dispatch_after_commit(result: IntakeResult, background_tasks: BackgroundTasks) -> None:
    if result.job_id:
        notify_outbox()
    else:
        background_tasks.add_task(_run_inline, result.document.doc_id, result.version.version)


async def read_validated(file: UploadFile):
    try:
        return validate_upload(await file.read())
    except IntakeRejected as exc:
        status_code = 413 if "limit" in str(exc) or "too large" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


def accepted(result: IntakeResult) -> JSONResponse:
    doc_id = result.document.doc_id
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "doc_id": doc_id,
            "version": result.version.version,
            "sha256": result.version.sha256,
            "job_id": result.job_id,
            "status": INGEST_QUEUED,
            "status_url": f"/api/documents/{doc_id}/status",
            "duplicate_of": result.duplicate_of,
        },
        headers={"Location": f"/api/documents/{doc_id}/status"},
    )


@router.post("/api/documents/ingest", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(rate_limit("upload"))])
async def ingest_document(
    request: Request,
    background_tasks: BackgroundTasks,
    doc_date: str = Form(...),
    doc_id: str = Form(...),
    doc_type: str = Form(...),
    keywords: str = Form(""),
    sensitivity: str = Form("internal"),
    title: str = Form(""),
    reference_number: str = Form(""),
    department: str = Form(""),
    subject: str = Form(""),
    category: str = Form(""),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    require_role(current_user, [Role.admin, Role.uploader])
    doc_id = (doc_id or "").strip()
    doc_type = normalize_document_type(doc_type)
    if not doc_id or not doc_type:
        raise HTTPException(status_code=400, detail="Document ID and type are required")
    try:
        parsed_date = datetime.fromisoformat(doc_date).date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid document date, expected YYYY-MM-DD") from exc

    conflict = await doc_id_conflict(db, doc_id)
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)

    upload = await read_validated(file)
    sensitivity_value = parse_sensitivity(sensitivity)
    try:
        result = await intake_new_document(
            db,
            upload=upload,
            doc_id=doc_id,
            doc_date=parsed_date,
            doc_type=doc_type,
            keywords=_parse_upload_keywords(keywords, []),
            sensitivity=sensitivity_value,
            uploaded_by=current_user.username,
            metadata={
                "title": title.strip(),
                "reference_number": reference_number.strip(),
                "department": department.strip(),
                "subject": subject.strip(),
                "category": category.strip(),
            },
        )
    except WriteOnceViolation as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Storing original failed for %s", doc_id)
        raise HTTPException(status_code=502, detail="Could not store the original file") from exc

    await write_audit(
        db,
        username=current_user.username,
        action="upload",
        resource_type="document",
        resource_id=doc_id,
        detail=(
            f"{doc_type} sensitivity={sensitivity_value} type={upload.content_type} "
            f"sha256={upload.sha256} job={result.job_id or 'inline'}"
        ),
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    dispatch_after_commit(result, background_tasks)
    return accepted(result)


async def _latest_job(db: AsyncSession, doc_id: str) -> Optional[JobRun]:
    result = await db.execute(
        select(JobRun)
        .where(JobRun.resource_type == "document", JobRun.resource_id == doc_id)
        .order_by(JobRun.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _assert_can_see(current_user: User, document: DocumentRecord, db: AsyncSession) -> None:
    if current_user.role == Role.board_member:
        await assert_can_view(current_user, document.doc_id, db)
    elif current_user.role == Role.uploader and document.uploaded_by != current_user.username:
        raise HTTPException(status_code=403, detail="Insufficient permissions")


@router.get("/api/documents/{doc_id}/status")
async def document_ingest_status(
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    document = await _load_document_or_404(doc_id, db)
    await _assert_can_see(current_user, document, db)
    return ingest_status_payload(document, await _latest_job(db, doc_id))


@router.post("/api/documents/{doc_id}/ingest/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_document_ingest(
    request: Request,
    doc_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    require_role(current_user, [Role.admin])
    document = await _load_document_or_404(doc_id, db)
    if document.ingest_status != INGEST_FAILED:
        raise HTTPException(status_code=409, detail=f"Ingest is {document.ingest_status}, not failed")
    document.ingest_status = INGEST_QUEUED
    document.ingest_error = None
    document.ingest_updated_at = datetime.utcnow()
    job_id = _schedule(db, doc_id)
    await write_audit(
        db,
        username=current_user.username,
        action="ingest_retry",
        resource_type="document",
        resource_id=doc_id,
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    if job_id:
        notify_outbox()
    else:
        background_tasks.add_task(_run_inline, doc_id)
    return {"doc_id": doc_id, "job_id": job_id, "status": INGEST_QUEUED}


@router.get("/api/documents/{doc_id}/versions")
async def document_versions(
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    document = await _load_document_or_404(doc_id, db)
    if current_user.role == Role.board_member:
        await assert_can_view(current_user, doc_id, db)
    rows = await list_versions(db, doc_id)
    return {
        "doc_id": doc_id,
        "current_version": document.current_version,
        "versions": [version_payload(r, current=document.current_version) for r in rows],
    }


@router.post("/api/documents/{doc_id}/versions", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(rate_limit("upload"))])
async def upload_document_version(
    request: Request,
    doc_id: str,
    background_tasks: BackgroundTasks,
    note: str = Form(""),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    require_role(current_user, [Role.admin, Role.uploader])
    document = await _load_document_or_404(doc_id, db)
    if current_user.role == Role.uploader and document.uploaded_by != current_user.username:
        raise HTTPException(status_code=403, detail="Only the original uploader or an admin can add versions")
    if document.ingest_status not in {"indexed", "failed", "rejected", "needs_review"}:
        raise HTTPException(status_code=409, detail="The previous version is still processing")
    upload = await read_validated(file)
    try:
        result = await intake_new_version(
            db, document=document, upload=upload, uploaded_by=current_user.username, note=note.strip() or None
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WriteOnceViolation as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await write_audit(
        db,
        username=current_user.username,
        action="version_upload",
        resource_type="document",
        resource_id=doc_id,
        detail=f"v{result.version.version} type={upload.content_type} sha256={upload.sha256}",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    dispatch_after_commit(result, background_tasks)
    return accepted(result)


@router.get("/api/documents/{doc_id}/versions/{version}/verify")
async def verify_document_version(
    request: Request,
    doc_id: str,
    version: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Re-read the stored original and compare its SHA-256 with the recorded hash."""
    require_role(current_user, [Role.admin, Role.board_secretary])
    row = (
        await db.execute(
            select(DocumentVersion).where(DocumentVersion.doc_id == doc_id, DocumentVersion.version == version)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found")
    data = await _load_stored_pdf_bytes(row.original_location or row.file_location, resource_id=doc_id)
    actual = sha256_hex(data)
    match = row.sha256 is not None and actual == row.sha256
    await write_audit(
        db,
        username=current_user.username,
        action="integrity_check",
        resource_type="document",
        resource_id=doc_id,
        detail=f"v{version} match={match}",
        ip_address=_client_ip(request),
        commit=True,
    )
    return {"doc_id": doc_id, "version": version, "recorded_sha256": row.sha256, "actual_sha256": actual, "match": match}
