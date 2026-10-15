"""Board-paper briefings, board paper packages, and the decision register.

Organizers (admin, board secretary) generate, edit, review, and approve briefings. Board members
see only approved briefings, and only for papers they may open. A briefing's subject is either one
paper or a package (a memo with its annexures); both share the same workflow handlers. In a
package, points citing a member the reader cannot open are hidden. Historical references and
prior decisions are computed per reader at request time, so a restricted paper never surfaces to
someone who cannot open it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import assert_can_view, write_audit
from ..deps import _client_ip, get_current_user, get_db, require_meeting_organizer, require_role, templates
from ..jobs.queue import enqueue_job, job_queue_enabled
from ..models import (
    BoardBriefing,
    BoardMeeting,
    BoardPaperPackage,
    DocumentRecord,
    MeetingDocument,
    MeetingInvitation,
    Role,
    User,
)
from ..rate_limit import rate_limit
from ..services import board_packages as packages
from ..services.briefing import (
    ATTENTION_KEYS,
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
    get_package_briefing,
    latest_briefing,
    latest_package_briefing,
    load_summary,
    mark_reviewed,
    reopen,
    request_briefing,
    request_package_briefing,
    return_to_draft,
    run_board_briefing,
    save_section,
)
from ..services.decision_register import decision_register, historical_references, related_decisions
from ..services.search import citation_url

logger = logging.getLogger("bcp_project.routers.briefings")
router = APIRouter()

READER_ROLES = [Role.admin, Role.board_secretary, Role.board_member]
_ERROR_CODES = {400: "briefing_input", 403: "briefing_four_eyes", 404: "briefing_failed", 409: "briefing_locked", 422: "briefing_failed"}


@dataclass
class Subject:
    """What a briefing is about: one paper, or a package led by its memo (``document``)."""

    kind: str
    base: str
    title: str
    document: Optional[DocumentRecord]
    package: Optional[BoardPaperPackage] = None

    @property
    def resource_id(self) -> str:
        if self.package is not None:
            return f"package:{self.package.id}"
        return self.document.doc_id

    @property
    def poll_url(self) -> str:
        return f"/api{self.base}"


def _is_organizer(user: User) -> bool:
    return user.role in (Role.admin, Role.board_secretary)


def _back(base: str, code: str, anchor: str = "") -> RedirectResponse:
    suffix = f"#{anchor}" if anchor else ""
    return RedirectResponse(url=f"{base}?status={code}{suffix}", status_code=status.HTTP_303_SEE_OTHER)


def _fail(base: str, exc: BriefingWorkflowError) -> RedirectResponse:
    return _back(base, _ERROR_CODES.get(exc.status_code, "briefing_locked"))


async def _audit(db: AsyncSession, request: Request, user: User, action: str, resource_id: str, detail: str) -> None:
    await write_audit(
        db,
        username=user.username,
        action=action,
        resource_type="briefing",
        resource_id=resource_id,
        detail=detail[:1000],
        ip_address=_client_ip(request),
        commit=False,
    )


async def _document_or_404(db: AsyncSession, doc_id: str) -> DocumentRecord:
    document = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == doc_id))).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


async def _document_subject(db: AsyncSession, doc_id: str) -> Subject:
    document = await _document_or_404(db, doc_id)
    return Subject("document", f"/documents/{doc_id}/briefing", document.title or doc_id, document)


async def _package_or_404(db: AsyncSession, package_id: int, *, for_update: bool = False) -> BoardPaperPackage:
    package = await packages.get_package(db, package_id, for_update=for_update)
    if package is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found")
    return package


async def _package_subject(db: AsyncSession, package_id: int) -> Subject:
    package = await _package_or_404(db, package_id)
    memo = None
    if package.memo_doc_id:
        memo = (
            await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == package.memo_doc_id))
        ).scalar_one_or_none()
    return Subject("package", f"/board/packages/{package.id}/briefing", package.title, memo, package)


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


async def _member_access(db: AsyncSession, user: User, doc_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Per member: ``{"ok": bool, "link": meeting viewer link or None}``."""
    access: Dict[str, Dict[str, Any]] = {}
    for doc_id in doc_ids:
        try:
            access[doc_id] = {"ok": True, "link": await _authorize_reader(db, user, doc_id)}
        except HTTPException:
            access[doc_id] = {"ok": False, "link": None}
    return access


async def _authorize_subject(db: AsyncSession, user: User, subject: Subject) -> Dict[str, Dict[str, Any]]:
    """Readers must be able to open the paper (or the package memo); raises 403 otherwise."""
    if subject.package is None:
        link = await _authorize_reader(db, user, subject.document.doc_id)
        return {subject.document.doc_id: {"ok": True, "link": link}}
    items = await packages.package_items(db, subject.package.id)
    access = await _member_access(db, user, [i.doc_id for i in items])
    memo_id = subject.package.memo_doc_id
    if not _is_organizer(user) and (not memo_id or not access.get(memo_id, {}).get("ok")):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot open this package's memo")
    return access


async def _current_briefing(db: AsyncSession, subject: Subject, *, for_update: bool = False) -> BoardBriefing:
    if subject.package is not None:
        briefing = await get_package_briefing(db, subject.package.id, subject.package.revision, for_update=for_update)
    else:
        version = await current_version_number(db, subject.document)
        briefing = await get_briefing(db, subject.document.doc_id, version, for_update=for_update)
    if briefing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No briefing for the current version")
    return briefing


async def _reader_briefing(db: AsyncSession, user: User, subject: Subject) -> Optional[BoardBriefing]:
    """Organizers see the current version's briefing in any state; others only an approved one."""
    if subject.package is not None:
        package = subject.package
        if _is_organizer(user):
            return await get_package_briefing(db, package.id, package.revision) or await latest_package_briefing(db, package.id)
        return await latest_package_briefing(db, package.id, approved_only=True)
    document = subject.document
    if _is_organizer(user):
        version = await current_version_number(db, document)
        return await get_briefing(db, document.doc_id, version) or await latest_briefing(db, document.doc_id)
    return await latest_briefing(db, document.doc_id, approved_only=True)


def _apply_point_links(view: Dict[str, Any], subject: Subject, access: Dict[str, Dict[str, Any]]) -> int:
    """Give each point a link to the cited page (highlighting the quote); drop points the reader can't open."""
    memo_id = subject.document.doc_id if subject.document is not None else None
    hidden = 0
    for section in view["sections"]:
        kept = []
        for point in section["points"]:
            doc_id = point.get("doc_id") or memo_id
            rights = access.get(doc_id) if doc_id else None
            if subject.package is not None and not (rights and rights["ok"]):
                continue
            link = rights["link"] if rights else None
            point["url"] = link or citation_url(doc_id, point.get("page"), point.get("quote"))
            kept.append(point)
        section["hidden"] = len(section["points"]) - len(kept)
        section["points"] = kept
        hidden += section["hidden"]
    return hidden


def _attention(view: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Risks, financial and regulatory points for the key-risks strip at the top of the briefing."""
    out = []
    for section in view["sections"]:
        if section["key"] in ATTENTION_KEYS and section["points"]:
            out.append({"key": section["key"], "label": section["label"], "points": section["points"]})
    return out


async def _context_for(
    db: AsyncSession,
    user: User,
    subject: Subject,
    briefing: Optional[BoardBriefing],
    access: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    view = briefing_view(briefing)
    references, decisions, attention = [], [], []
    hidden = 0
    if view and briefing.status == STATUS_READY and briefing.content:
        summary = load_summary(briefing.content)
        query = " ".join(filter(None, [subject.title, summary.headline_text()]))
        if subject.document is not None:
            references = await historical_references(db, user, subject.document, query)
        decisions = await related_decisions(db, user, query)
        hidden = _apply_point_links(view, subject, access)
        attention = _attention(view)
    members: List[Dict[str, Any]] = []
    if subject.package is not None:
        snapshot = await packages.package_snapshot(db, subject.package)
        member_ids = {m["doc_id"] for m in snapshot}
        references = [r for r in references if r["doc_id"] not in member_ids]
        current_version = subject.package.revision
        stale = packages.is_stale(briefing, subject.package, snapshot)
        for m in snapshot:
            rights = access.get(m["doc_id"]) or {"ok": False, "link": None}
            members.append(
                {
                    "label": m["label"],
                    "role": m["role"],
                    "role_label": packages.ROLE_LABELS.get(m["role"], m["role"]),
                    "doc_id": m["doc_id"] if rights["ok"] else None,
                    "title": m["title"] if rights["ok"] else None,
                    "version": m["version"],
                    "url": (rights["link"] or f"/view/{m['doc_id']}") if rights["ok"] else None,
                }
            )
        memo_rights = access.get(subject.package.memo_doc_id or "") or {}
        paper_url = memo_rights.get("link") or (f"/view/{subject.package.memo_doc_id}" if subject.package.memo_doc_id else None)
    else:
        current_version = await current_version_number(db, subject.document)
        stale = bool(view and view["version"] != current_version)
        link = (access.get(subject.document.doc_id) or {}).get("link")
        paper_url = link or f"/view/{subject.document.doc_id}"
    return {
        "briefing": view,
        "references": references,
        "decisions": decisions,
        "attention": attention,
        "hidden_points": hidden,
        "members": members,
        "current_version": current_version,
        "stale": stale,
        "paper_url": paper_url,
    }


async def _schedule(briefing_id: int, background_tasks: BackgroundTasks) -> str:
    if job_queue_enabled():
        job_id = await enqueue_job(JOB_BOARD_BRIEFING, {"briefing_id": int(briefing_id)})
        if job_id:
            return f"job={job_id}"
    background_tasks.add_task(run_board_briefing, int(briefing_id))
    return "background"


# --- Shared page handlers ---------------------------------------------------------------


def _page_extras(user: User, briefing: Optional[BoardBriefing], subject: Subject) -> Dict[str, Any]:
    organizer = _is_organizer(user)
    return {
        "is_organizer": organizer,
        "can_amend": user.role == Role.admin,
        "approval_blocker": approval_blocker(briefing, user.username) if (briefing and organizer) else None,
        "four_eyes": four_eyes_required(),
        "subject_kind": subject.kind,
        "base": subject.base,
        "poll_url": subject.poll_url,
        "subject_title": subject.title,
        "package": subject.package,
    }


async def _render_page(request: Request, user: User, db: AsyncSession, subject: Subject) -> Any:
    require_role(user, READER_ROLES)
    try:
        access = await _authorize_subject(db, user, subject)
    except HTTPException:
        document = subject.document
        return templates.TemplateResponse(
            request,
            "access_denied.html",
            {
                "user": user,
                "doc_id": document.doc_id if document else None,
                "doc_type": document.doc_type if document else None,
                "page_title": "Access required",
            },
            status_code=403,
        )
    briefing = await _reader_briefing(db, user, subject)
    context = await _context_for(db, user, subject, briefing, access)
    await _audit(db, request, user, "briefing_view", subject.resource_id, f"state={briefing.state if briefing else 'none'}")
    await db.commit()
    return templates.TemplateResponse(
        request,
        "briefing.html",
        {
            **context,
            **_page_extras(user, briefing, subject),
            "user": user,
            "document": subject.document,
            "status": request.query_params.get("status") or "",
            "page_title": f"Briefing · {subject.title}",
        },
    )


async def _render_api(request: Request, user: User, db: AsyncSession, subject: Subject, refs: bool) -> JSONResponse:
    require_role(user, READER_ROLES)
    access = await _authorize_subject(db, user, subject)
    briefing = await _reader_briefing(db, user, subject)
    if refs:
        context = await _context_for(db, user, subject, briefing, access)
        extras = _page_extras(user, briefing, subject)
        context.update(
            title=subject.title,
            doc_type=subject.document.doc_type if subject.document else None,
            is_organizer=extras["is_organizer"],
            can_amend=extras["can_amend"],
            approval_blocker=extras["approval_blocker"],
            four_eyes=extras["four_eyes"],
        )
        await _audit(db, request, user, "briefing_view", subject.resource_id, f"state={briefing.state if briefing else 'none'} api")
        await db.commit()
    else:
        view = briefing_view(briefing)
        if view and subject.package is not None and not _is_organizer(user):
            _apply_point_links(view, subject, access)
        current = subject.package.revision if subject.package is not None else await current_version_number(db, subject.document)
        context = {"briefing": view, "current_version": current}
    return JSONResponse(context)


async def _generate(
    request: Request, user: User, db: AsyncSession, subject: Subject, background_tasks: BackgroundTasks
) -> RedirectResponse:
    require_meeting_organizer(user)
    try:
        if subject.package is not None:
            package = await _package_or_404(db, subject.package.id, for_update=True)
            items = await packages.package_items(db, package.id)
            for item in items:
                await assert_can_view(user, item.doc_id, db)
            snapshot = await packages.package_snapshot(db, package)
            if not snapshot:
                raise BriefingWorkflowError("Add the memo and its annexures first.", status_code=400)
            existing = await get_package_briefing(db, package.id, package.revision, for_update=True)
            await packages.prepare_revision(db, package, snapshot, existing)
            briefing = await request_package_briefing(db, package, snapshot, user.username)
            detail = f"package_revision={briefing.package_revision} members={','.join(m['doc_id'] for m in snapshot)}"
        else:
            await assert_can_view(user, subject.document.doc_id, db)
            briefing = await request_briefing(db, subject.document, user.username)
            detail = f"version={briefing.version}"
    except BriefingWorkflowError as exc:
        await db.rollback()
        return _fail(subject.base, exc)
    await _audit(db, request, user, "briefing_generate", subject.resource_id, detail)
    await db.commit()
    how = await _schedule(briefing.id, background_tasks)
    logger.info("Briefing %s for %s scheduled (%s)", briefing.id, subject.resource_id, how)
    return _back(subject.base, "briefing_generating")


def _revision_note(briefing: BoardBriefing) -> str:
    if briefing.package_id:
        return f"package_revision={briefing.package_revision}"
    return f"version={briefing.version}"


async def _edit_section(
    request: Request, user: User, db: AsyncSession, subject: Subject, key: str, text: str
) -> RedirectResponse:
    require_meeting_organizer(user)
    briefing = await _current_briefing(db, subject, for_update=True)
    page_count = subject.document.page_count if subject.document is not None else None
    try:
        changed = save_section(briefing, key, text, user.username, page_count=page_count)
    except BriefingWorkflowError as exc:
        return _fail(subject.base, exc)
    if not changed:
        return _back(subject.base, "briefing_unchanged", key)
    await _audit(db, request, user, "briefing_edit", subject.resource_id, f"{_revision_note(briefing)} heading={HEADING_LABELS[key]}")
    await db.commit()
    return _back(subject.base, "briefing_saved", key)


async def _review(request: Request, user: User, db: AsyncSession, subject: Subject) -> RedirectResponse:
    require_meeting_organizer(user)
    briefing = await _current_briefing(db, subject, for_update=True)
    try:
        mark_reviewed(briefing, user.username)
    except BriefingWorkflowError as exc:
        return _fail(subject.base, exc)
    await _audit(db, request, user, "briefing_review", subject.resource_id, _revision_note(briefing))
    await db.commit()
    return _back(subject.base, "briefing_reviewed")


async def _approve(request: Request, user: User, db: AsyncSession, subject: Subject) -> RedirectResponse:
    from ..ports.events import publish_event

    require_meeting_organizer(user)
    briefing = await _current_briefing(db, subject, for_update=True)
    try:
        digest = approve(briefing, user.username)
    except BriefingWorkflowError as exc:
        if exc.status_code == 403:
            await _audit(db, request, user, "briefing_approve_denied", subject.resource_id, str(exc))
            await db.commit()
        return _fail(subject.base, exc)
    await _audit(db, request, user, "briefing_approve", subject.resource_id, f"{_revision_note(briefing)} sha256={digest}")
    await db.commit()
    payload = {"doc_id": briefing.doc_id, "version": briefing.version, "approved_by": user.username, "sha256": digest}
    if briefing.package_id:
        payload.update(package_id=briefing.package_id, package_revision=briefing.package_revision)
    publish_event("document.briefing_approved", payload)
    return _back(subject.base, "briefing_approved")


async def _return(request: Request, user: User, db: AsyncSession, subject: Subject, note: str) -> RedirectResponse:
    require_meeting_organizer(user)
    briefing = await _current_briefing(db, subject, for_update=True)
    try:
        return_to_draft(briefing, user.username, note)
    except BriefingWorkflowError as exc:
        return _fail(subject.base, exc)
    await _audit(db, request, user, "briefing_return", subject.resource_id, f"{_revision_note(briefing)} note={note.strip()}")
    await db.commit()
    return _back(subject.base, "briefing_returned")


async def _reopen(request: Request, user: User, db: AsyncSession, subject: Subject, reason: str) -> RedirectResponse:
    require_role(user, [Role.admin])
    briefing = await _current_briefing(db, subject, for_update=True)
    if briefing.state != STATE_APPROVED:
        return _back(subject.base, "briefing_locked")
    previous = briefing.sha256
    try:
        reopen(briefing, user.username, reason)
    except BriefingWorkflowError as exc:
        return _fail(subject.base, exc)
    await _audit(
        db, request, user, "briefing_reopen", subject.resource_id,
        f"{_revision_note(briefing)} previous_sha256={previous} reason={reason.strip()}",
    )
    await db.commit()
    return _back(subject.base, "briefing_reopened")


# --- Single-paper briefing routes ---------------------------------------------------------


@router.get("/documents/{doc_id}/briefing", response_class=HTMLResponse)
async def briefing_page(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, READER_ROLES)
    return await _render_page(request, current_user, db, await _document_subject(db, doc_id))


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
    return await _render_api(request, current_user, db, await _document_subject(db, doc_id), refs)


@router.post("/documents/{doc_id}/briefing/generate", dependencies=[Depends(rate_limit("ai_job"))])
async def generate_briefing_route(
    request: Request,
    doc_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    return await _generate(request, current_user, db, await _document_subject(db, doc_id), background_tasks)


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
    return await _edit_section(request, current_user, db, await _document_subject(db, doc_id), key, text)


@router.post("/documents/{doc_id}/briefing/review")
async def review_briefing(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    return await _review(request, current_user, db, await _document_subject(db, doc_id))


@router.post("/documents/{doc_id}/briefing/approve")
async def approve_briefing(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    return await _approve(request, current_user, db, await _document_subject(db, doc_id))


@router.post("/documents/{doc_id}/briefing/return")
async def return_briefing(
    request: Request,
    doc_id: str,
    note: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    return await _return(request, current_user, db, await _document_subject(db, doc_id), note)


@router.post("/documents/{doc_id}/briefing/reopen")
async def reopen_briefing(
    request: Request,
    doc_id: str,
    reason: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    return await _reopen(request, current_user, db, await _document_subject(db, doc_id), reason)


# --- Package briefing routes --------------------------------------------------------------


@router.get("/board/packages/{package_id}/briefing", response_class=HTMLResponse)
async def package_briefing_page(
    request: Request,
    package_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, READER_ROLES)
    return await _render_page(request, current_user, db, await _package_subject(db, package_id))


@router.get("/api/board/packages/{package_id}/briefing")
async def package_briefing_api(
    request: Request,
    package_id: int,
    refs: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    require_role(current_user, READER_ROLES)
    return await _render_api(request, current_user, db, await _package_subject(db, package_id), refs)


@router.post("/board/packages/{package_id}/briefing/generate", dependencies=[Depends(rate_limit("ai_job"))])
async def package_generate_route(
    request: Request,
    package_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    return await _generate(request, current_user, db, await _package_subject(db, package_id), background_tasks)


@router.post("/board/packages/{package_id}/briefing/sections/{key}")
async def package_edit_section(
    request: Request,
    package_id: int,
    key: str,
    text: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    return await _edit_section(request, current_user, db, await _package_subject(db, package_id), key, text)


@router.post("/board/packages/{package_id}/briefing/review")
async def package_review(
    request: Request,
    package_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    return await _review(request, current_user, db, await _package_subject(db, package_id))


@router.post("/board/packages/{package_id}/briefing/approve")
async def package_approve(
    request: Request,
    package_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    return await _approve(request, current_user, db, await _package_subject(db, package_id))


@router.post("/board/packages/{package_id}/briefing/return")
async def package_return(
    request: Request,
    package_id: int,
    note: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    return await _return(request, current_user, db, await _package_subject(db, package_id), note)


@router.post("/board/packages/{package_id}/briefing/reopen")
async def package_reopen(
    request: Request,
    package_id: int,
    reason: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    return await _reopen(request, current_user, db, await _package_subject(db, package_id), reason)


# --- Package management -------------------------------------------------------------------


def _package_back(package_id: int, code: str) -> RedirectResponse:
    return RedirectResponse(url=f"/board/packages/{package_id}?status={code}", status_code=status.HTTP_303_SEE_OTHER)


async def _package_audit(db: AsyncSession, request: Request, user: User, action: str, package: BoardPaperPackage, detail: str) -> None:
    await write_audit(
        db,
        username=user.username,
        action=action,
        resource_type="board_package",
        resource_id=str(package.id),
        detail=f"revision={package.revision} {detail}"[:1000],
        ip_address=_client_ip(request),
    )


async def _meeting_choices(db: AsyncSession) -> List[BoardMeeting]:
    rows = await db.execute(select(BoardMeeting).order_by(BoardMeeting.scheduled_at.desc()).limit(50))
    return list(rows.scalars().all())


@router.get("/board/packages", response_class=HTMLResponse)
async def packages_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_meeting_organizer(current_user)
    return templates.TemplateResponse(
        request,
        "board_packages.html",
        {
            "user": current_user,
            "page_title": "Board paper packages",
            "rows": await packages.list_packages(db),
            "status": request.query_params.get("status") or "",
        },
    )


@router.get("/board/packages/new", response_class=HTMLResponse)
async def new_package_page(
    request: Request,
    meeting_id: Optional[int] = None,
    memo: str = "",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_meeting_organizer(current_user)
    return templates.TemplateResponse(
        request,
        "board_package_form.html",
        {
            "user": current_user,
            "page_title": "New board paper package",
            "meetings": await _meeting_choices(db),
            "meeting_id": meeting_id,
            "memo": memo,
            "error": request.query_params.get("error") or "",
        },
    )


@router.post("/board/packages")
async def create_package_route(
    request: Request,
    title: str = Form(""),
    meeting_id: str = Form(""),
    memo_doc_id: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = int(meeting_id) if meeting_id.strip().isdigit() else None
    try:
        if memo_doc_id.strip():
            await assert_can_view(current_user, memo_doc_id.strip(), db)
        package = await packages.create_package(
            db, title=title, username=current_user.username, meeting_id=meeting, memo_doc_id=memo_doc_id
        )
    except BriefingWorkflowError as exc:
        await db.rollback()
        from urllib.parse import quote

        return RedirectResponse(url=f"/board/packages/new?error={quote(str(exc))}", status_code=status.HTTP_303_SEE_OTHER)
    await _package_audit(db, request, current_user, "board_package_create", package, f"memo={package.memo_doc_id or ''}")
    await db.commit()
    return _package_back(package.id, "created")


@router.get("/board/packages/{package_id}", response_class=HTMLResponse)
async def package_detail_page(
    request: Request,
    package_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_meeting_organizer(current_user)
    package = await _package_or_404(db, package_id)
    items = await packages.package_items(db, package.id)
    docs = await packages.member_documents(db, items)
    briefing = await get_package_briefing(db, package.id, package.revision) or await latest_package_briefing(db, package.id)
    snapshot = await packages.package_snapshot(db, package)
    meeting = await db.get(BoardMeeting, package.meeting_id) if package.meeting_id else None
    return templates.TemplateResponse(
        request,
        "board_package_detail.html",
        {
            "user": current_user,
            "page_title": package.title,
            "package": package,
            "items": items,
            "docs": docs,
            "role_labels": packages.ROLE_LABELS,
            "roles": packages.ROLES,
            "briefing": briefing,
            "stale": packages.is_stale(briefing, package, snapshot),
            "meeting": meeting,
            "meetings": await _meeting_choices(db),
            "pack": await packages.meeting_pack_candidates(db, package),
            "suggestions": await packages.suggest_annexures(db, package),
            "status": request.query_params.get("status") or "",
            "error": request.query_params.get("error") or "",
        },
    )


async def _package_change(request: Request, user: User, db: AsyncSession, package_id: int, action: str, change) -> RedirectResponse:
    require_meeting_organizer(user)
    package = await _package_or_404(db, package_id, for_update=True)
    try:
        detail = await change(package)
    except BriefingWorkflowError as exc:
        await db.rollback()
        from urllib.parse import quote

        return RedirectResponse(
            url=f"/board/packages/{package_id}?error={quote(str(exc))}", status_code=status.HTTP_303_SEE_OTHER
        )
    await _package_audit(db, request, user, action, package, detail)
    await db.commit()
    return _package_back(package_id, "saved")


@router.post("/board/packages/{package_id}")
async def update_package_route(
    request: Request,
    package_id: int,
    title: str = Form(""),
    meeting_id: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    async def change(package: BoardPaperPackage) -> str:
        cleaned = " ".join(title.split())[:255]
        if not cleaned:
            raise BriefingWorkflowError("Give the package a title.", status_code=400)
        package.title = cleaned
        meeting = int(meeting_id) if meeting_id.strip().isdigit() else None
        if meeting is not None and await db.get(BoardMeeting, meeting) is None:
            raise BriefingWorkflowError("That meeting does not exist.", status_code=400)
        package.meeting_id = meeting
        return f"title={cleaned} meeting={meeting or ''}"

    return await _package_change(request, current_user, db, package_id, "board_package_update", change)


@router.post("/board/packages/{package_id}/members")
async def add_package_member(
    request: Request,
    package_id: int,
    doc_id: str = Form(""),
    role: str = Form("annexure"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    async def change(package: BoardPaperPackage) -> str:
        await assert_can_view(current_user, doc_id.strip(), db)
        item = await packages.add_member(db, package, doc_id, role, current_user.username)
        return f"add={item.doc_id} role={item.role} label={item.label}"

    return await _package_change(request, current_user, db, package_id, "board_package_member_add", change)


@router.post("/board/packages/{package_id}/members/{doc_id}/remove")
async def remove_package_member(
    request: Request,
    package_id: int,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    async def change(package: BoardPaperPackage) -> str:
        await packages.remove_member(db, package, doc_id)
        return f"remove={doc_id}"

    return await _package_change(request, current_user, db, package_id, "board_package_member_remove", change)


@router.post("/board/packages/{package_id}/members/{doc_id}/move")
async def move_package_member(
    request: Request,
    package_id: int,
    doc_id: str,
    direction: str = Form("up"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    async def change(package: BoardPaperPackage) -> str:
        await packages.move_member(db, package, doc_id, "up" if direction == "up" else "down")
        return f"move={doc_id} {direction}"

    return await _package_change(request, current_user, db, package_id, "board_package_member_move", change)


# --- Decision register ------------------------------------------------------------------


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
