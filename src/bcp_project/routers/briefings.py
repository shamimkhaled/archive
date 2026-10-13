"""Board-paper briefings and the decision register.

Organizers (admin, board secretary) generate, edit, review, and approve briefings. Board members
see only approved briefings, and only for papers they may open. Historical references and prior
decisions are computed per reader at request time, so a restricted paper never surfaces to
someone who cannot open it.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import assert_can_view, write_audit
from ..deps import _client_ip, get_current_user, get_db, require_meeting_organizer, require_role, templates
from ..jobs.queue import enqueue_job, job_queue_enabled
from ..models import BoardBriefing, DocumentRecord, MeetingDocument, MeetingInvitation, Role, User
from ..rate_limit import rate_limit
from ..services.briefing import (
    HEADING_LABELS,
    JOB_BOARD_BRIEFING,
    STATE_APPROVED,
    STATUS_READY,
    BriefingWorkflowError,
    approval_blocker,
    approve,
    briefing_view,
    current_version_number,
    four_eyes_required,
    get_briefing,
    latest_briefing,
    load_summary,
    mark_reviewed,
    reopen,
    request_briefing,
    return_to_draft,
    run_board_briefing,
    save_section,
)
from ..services.decision_register import decision_register, historical_references, related_decisions
from ..services.search import citation_url

logger = logging.getLogger("bcp_project.routers.briefings")
router = APIRouter()

READER_ROLES = [Role.admin, Role.board_secretary, Role.board_member]
_ERROR_CODES = {400: "briefing_input", 403: "briefing_four_eyes", 409: "briefing_locked", 422: "briefing_failed"}


def _is_organizer(user: User) -> bool:
    return user.role in (Role.admin, Role.board_secretary)


def _back(doc_id: str, code: str, anchor: str = "") -> RedirectResponse:
    suffix = f"#{anchor}" if anchor else ""
    return RedirectResponse(url=f"/documents/{doc_id}/briefing?status={code}{suffix}", status_code=status.HTTP_303_SEE_OTHER)


def _fail(doc_id: str, exc: BriefingWorkflowError) -> RedirectResponse:
    return _back(doc_id, _ERROR_CODES.get(exc.status_code, "briefing_locked"))


async def _audit(db: AsyncSession, request: Request, user: User, action: str, doc_id: str, detail: str) -> None:
    await write_audit(
        db,
        username=user.username,
        action=action,
        resource_type="briefing",
        resource_id=doc_id,
        detail=detail[:1000],
        ip_address=_client_ip(request),
        commit=False,
    )


async def _document_or_404(db: AsyncSession, doc_id: str) -> DocumentRecord:
    document = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == doc_id))).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


async def _invited_paper_link(db: AsyncSession, user: User, doc_id: str) -> Optional[str]:
    row = (
        await db.execute(
            select(MeetingDocument.meeting_id, MeetingDocument.id)
            .join(
                MeetingInvitation,
                and_(MeetingInvitation.meeting_id == MeetingDocument.meeting_id, MeetingInvitation.username == user.username),
            )
            .where(MeetingDocument.archive_doc_id == doc_id)
            .order_by(MeetingDocument.id.desc())
            .limit(1)
        )
    ).first()
    return f"/board/meetings/{row[0]}/documents/{row[1]}/view" if row else None


async def _authorize_reader(db: AsyncSession, user: User, doc_id: str) -> Optional[str]:
    """Archive view rights, or an invitation to a meeting whose pack holds the paper.

    Returns the meeting viewer link for invitees (``None`` means the archive viewer is open to
    them); raises 403 otherwise.
    """
    try:
        await assert_can_view(user, doc_id, db)
        return None
    except HTTPException:
        link = await _invited_paper_link(db, user, doc_id)
        if link:
            return link
        raise


async def _current_briefing(db: AsyncSession, document: DocumentRecord, *, for_update: bool = False) -> BoardBriefing:
    version = await current_version_number(db, document)
    briefing = await get_briefing(db, document.doc_id, version, for_update=for_update)
    if briefing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No briefing for the current version")
    return briefing


async def _reader_briefing(db: AsyncSession, user: User, document: DocumentRecord) -> Optional[BoardBriefing]:
    """Organizers see the current version's briefing in any state; others only an approved one."""
    if _is_organizer(user):
        version = await current_version_number(db, document)
        return await get_briefing(db, document.doc_id, version) or await latest_briefing(db, document.doc_id)
    return await latest_briefing(db, document.doc_id, approved_only=True)


async def _context_for(
    db: AsyncSession,
    user: User,
    document: DocumentRecord,
    briefing: Optional[BoardBriefing],
    paper_link: Optional[str] = None,
) -> Dict[str, Any]:
    view = briefing_view(briefing)
    references, decisions = [], []
    if view and briefing.status == STATUS_READY and briefing.content:
        summary = load_summary(briefing.content)
        query = " ".join(filter(None, [document.title, summary.headline_text()]))
        references = await historical_references(db, user, document, query)
        decisions = await related_decisions(db, user, query)
        for section in view["sections"]:
            for point in section["points"]:
                point["url"] = paper_link or citation_url(document.doc_id, point.get("page"))
    current_version = await current_version_number(db, document)
    return {
        "briefing": view,
        "references": references,
        "decisions": decisions,
        "current_version": current_version,
        "stale": bool(view and view["version"] != current_version),
        "paper_url": paper_link or f"/view/{document.doc_id}",
    }


async def _schedule(briefing_id: int, background_tasks: BackgroundTasks) -> str:
    if job_queue_enabled():
        job_id = await enqueue_job(JOB_BOARD_BRIEFING, {"briefing_id": int(briefing_id)})
        if job_id:
            return f"job={job_id}"
    background_tasks.add_task(run_board_briefing, int(briefing_id))
    return "background"


# --- Pages ---------------------------------------------------------------------------


@router.get("/documents/{doc_id}/briefing", response_class=HTMLResponse)
async def briefing_page(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, READER_ROLES)
    document = await _document_or_404(db, doc_id)
    try:
        paper_link = await _authorize_reader(db, current_user, doc_id)
    except HTTPException:
        return templates.TemplateResponse(
            request,
            "access_denied.html",
            {"user": current_user, "doc_id": doc_id, "doc_type": document.doc_type, "page_title": "Access required"},
            status_code=403,
        )
    briefing = await _reader_briefing(db, current_user, document)
    context = await _context_for(db, current_user, document, briefing, paper_link)
    organizer = _is_organizer(current_user)
    await _audit(db, request, current_user, "briefing_view", doc_id, f"state={briefing.state if briefing else 'none'}")
    await db.commit()
    return templates.TemplateResponse(
        request,
        "briefing.html",
        {
            **context,
            "user": current_user,
            "document": document,
            "is_organizer": organizer,
            "can_amend": current_user.role == Role.admin,
            "approval_blocker": approval_blocker(briefing, current_user.username) if (briefing and organizer) else None,
            "four_eyes": four_eyes_required(),
            "status": request.query_params.get("status") or "",
            "page_title": f"Briefing · {document.title or doc_id}",
        },
    )


@router.get("/api/documents/{doc_id}/briefing")
async def briefing_api(
    request: Request,
    doc_id: str,
    refs: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """`refs=1` returns the full page payload; it is audited like the HTML page."""
    require_role(current_user, READER_ROLES)
    document = await _document_or_404(db, doc_id)
    paper_link = await _authorize_reader(db, current_user, doc_id)
    briefing = await _reader_briefing(db, current_user, document)
    if refs:
        context = await _context_for(db, current_user, document, briefing, paper_link)
        organizer = _is_organizer(current_user)
        context.update(
            title=document.title or doc_id,
            doc_type=document.doc_type,
            is_organizer=organizer,
            can_amend=current_user.role == Role.admin,
            approval_blocker=approval_blocker(briefing, current_user.username) if (briefing and organizer) else None,
            four_eyes=four_eyes_required(),
        )
        await _audit(db, request, current_user, "briefing_view", doc_id, f"state={briefing.state if briefing else 'none'} api")
        await db.commit()
    else:
        context = {"briefing": briefing_view(briefing), "current_version": await current_version_number(db, document)}
    return JSONResponse(context)


@router.get("/board/decisions", response_class=HTMLResponse)
async def decisions_page(
    request: Request,
    q: str = "",
    year: str = "",
    tasks: str = "",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, READER_ROLES)
    entries = await decision_register(db, current_user, q=q)
    years = sorted({(e["meeting_date"] or "")[:4] for e in entries if e["meeting_date"]}, reverse=True)
    if year:
        entries = [e for e in entries if (e["meeting_date"] or "").startswith(year)]
    if tasks == "open":
        entries = [e for e in entries if any(a["status"] in ("open", "in_progress") for a in e["actions"])]
    elif tasks == "done":
        entries = [e for e in entries if e["actions"] and all(a["status"] in ("done", "cancelled") for a in e["actions"])]
    elif tasks == "none":
        entries = [e for e in entries if not e["actions"]]
    return templates.TemplateResponse(
        request,
        "decision_register.html",
        {
            "user": current_user,
            "entries": entries,
            "q": q,
            "year": year,
            "tasks": tasks,
            "years": years,
            "page_title": "Decision register",
        },
    )


@router.get("/api/decisions")
async def decisions_api(
    q: str = "",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    require_role(current_user, READER_ROLES)
    entries = await decision_register(db, current_user, q=q)
    return JSONResponse({"decisions": entries, "count": len(entries)})


# --- Workflow --------------------------------------------------------------------------


@router.post("/documents/{doc_id}/briefing/generate", dependencies=[Depends(rate_limit("ai_job"))])
async def generate_briefing_route(
    request: Request,
    doc_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    document = await _document_or_404(db, doc_id)
    await assert_can_view(current_user, doc_id, db)
    try:
        briefing = await request_briefing(db, document, current_user.username)
    except BriefingWorkflowError as exc:
        await db.rollback()
        return _fail(doc_id, exc)
    await _audit(db, request, current_user, "briefing_generate", doc_id, f"version={briefing.version}")
    await db.commit()
    how = await _schedule(briefing.id, background_tasks)
    logger.info("Briefing %s for %s v%s scheduled (%s)", briefing.id, doc_id, briefing.version, how)
    return _back(doc_id, "briefing_generating")


@router.post("/documents/{doc_id}/briefing/sections/{key}")
async def edit_briefing_section(
    request: Request,
    doc_id: str,
    key: str,
    text: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    document = await _document_or_404(db, doc_id)
    briefing = await _current_briefing(db, document, for_update=True)
    try:
        changed = save_section(briefing, key, text, current_user.username, page_count=document.page_count)
    except BriefingWorkflowError as exc:
        return _fail(doc_id, exc)
    if not changed:
        return _back(doc_id, "briefing_unchanged", key)
    await _audit(db, request, current_user, "briefing_edit", doc_id, f"version={briefing.version} heading={HEADING_LABELS[key]}")
    await db.commit()
    return _back(doc_id, "briefing_saved", key)


@router.post("/documents/{doc_id}/briefing/review")
async def review_briefing(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    document = await _document_or_404(db, doc_id)
    briefing = await _current_briefing(db, document, for_update=True)
    try:
        mark_reviewed(briefing, current_user.username)
    except BriefingWorkflowError as exc:
        return _fail(doc_id, exc)
    await _audit(db, request, current_user, "briefing_review", doc_id, f"version={briefing.version}")
    await db.commit()
    return _back(doc_id, "briefing_reviewed")


@router.post("/documents/{doc_id}/briefing/approve")
async def approve_briefing(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    from ..ports.events import publish_event

    require_meeting_organizer(current_user)
    document = await _document_or_404(db, doc_id)
    briefing = await _current_briefing(db, document, for_update=True)
    try:
        digest = approve(briefing, current_user.username)
    except BriefingWorkflowError as exc:
        if exc.status_code == 403:
            await _audit(db, request, current_user, "briefing_approve_denied", doc_id, str(exc))
            await db.commit()
        return _fail(doc_id, exc)
    await _audit(db, request, current_user, "briefing_approve", doc_id, f"version={briefing.version} sha256={digest}")
    await db.commit()
    publish_event(
        "document.briefing_approved",
        {"doc_id": doc_id, "version": briefing.version, "approved_by": current_user.username, "sha256": digest},
    )
    return _back(doc_id, "briefing_approved")


@router.post("/documents/{doc_id}/briefing/return")
async def return_briefing(
    request: Request,
    doc_id: str,
    note: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    document = await _document_or_404(db, doc_id)
    briefing = await _current_briefing(db, document, for_update=True)
    try:
        return_to_draft(briefing, current_user.username, note)
    except BriefingWorkflowError as exc:
        return _fail(doc_id, exc)
    await _audit(db, request, current_user, "briefing_return", doc_id, f"version={briefing.version} note={note.strip()}")
    await db.commit()
    return _back(doc_id, "briefing_returned")


@router.post("/documents/{doc_id}/briefing/reopen")
async def reopen_briefing(
    request: Request,
    doc_id: str,
    reason: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    document = await _document_or_404(db, doc_id)
    briefing = await _current_briefing(db, document, for_update=True)
    if briefing.state != STATE_APPROVED:
        return _back(doc_id, "briefing_locked")
    previous = briefing.sha256
    try:
        reopen(briefing, current_user.username, reason)
    except BriefingWorkflowError as exc:
        return _fail(doc_id, exc)
    await _audit(
        db, request, current_user, "briefing_reopen", doc_id,
        f"version={briefing.version} previous_sha256={previous} reason={reason.strip()}",
    )
    await db.commit()
    return _back(doc_id, "briefing_reopened")
