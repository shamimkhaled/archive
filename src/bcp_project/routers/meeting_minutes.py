"""Minutes review workflow, decisions and actions, recorded-file upload, and speaker naming."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from ..access_control import write_audit
from ..deps import _client_ip, get_current_user, get_db, require_meeting_organizer, require_role
from ..rate_limit import rate_limit
from ..diarization import apply_speaker_map, clean_speaker_map, speaker_labels
from ..dummy_transcription import TRANSCRIPTION_LIVE, TRANSCRIPTION_PROCESSING
from ..jobs.handlers import JOB_MEETING_TRANSCRIBE, run_meeting_transcribe
from ..jobs.queue import enqueue_job, job_queue_enabled
from ..meeting_audio import (
    RECORDING_STEM,
    clear_recording,
    meeting_audio_dir,
    recording_max_bytes,
    sniff_recording,
)
from ..minutes_draft import MINUTES_PENDING, meeting_sensitivity
from ..models import BoardMeeting, MeetingAction, MeetingDecision, MeetingRecord, Role, User
from ..services.minutes import (
    MinutesWorkflowError,
    action_payload,
    amend,
    approve,
    archive_approved_minutes,
    decision_payload,
    extract_decisions_and_actions,
    list_items,
    minutes_state,
    parse_due_date,
    reject,
    replace_ai_items,
    require_draft,
    set_action_status,
    submit_for_review,
    workflow_view,
)
from ..services.meeting_records import (
    KIND_MINUTES_APPROVED,
    archive_transcript,
    link_archive_document,
    list_records,
    record_payload,
    seal_minutes_draft,
)
from .meeting_common import _meeting_transcription_status

logger = logging.getLogger("bcp_project.routers.meeting_minutes")
router = APIRouter()

_ERROR_CODES = {403: "minutes_four_eyes", 400: "minutes_input_required", 409: "minutes_locked"}
UPLOAD_READ_BYTES = 1024 * 1024


def _back(meeting_id: int, code: str, anchor: str = "minutes") -> RedirectResponse:
    return RedirectResponse(url=f"/meetings/{meeting_id}?status={code}#{anchor}", status_code=status.HTTP_303_SEE_OTHER)


def _fail(meeting_id: int, exc: MinutesWorkflowError, anchor: str = "minutes") -> RedirectResponse:
    return _back(meeting_id, _ERROR_CODES.get(exc.status_code, "minutes_locked"), anchor)


async def _locked_meeting(db: AsyncSession, meeting_id: int) -> BoardMeeting:
    """Row-locked load so two reviewers cannot both pass a state check."""
    meeting = (
        await db.execute(select(BoardMeeting).where(BoardMeeting.id == meeting_id).with_for_update())
    ).scalar_one_or_none()
    if meeting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    return meeting


async def _audit(db: AsyncSession, request: Request, user: User, action: str, meeting_id: int, detail: str) -> None:
    await write_audit(
        db,
        username=user.username,
        action=action,
        resource_type="meeting",
        resource_id=str(meeting_id),
        detail=detail[:1000],
        ip_address=_client_ip(request),
        commit=False,
    )


# --- Review workflow -------------------------------------------------------------------

@router.post("/meetings/{meeting_id}/minutes/submit")
async def submit_minutes(
    request: Request,
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _locked_meeting(db, meeting_id)
    try:
        submit_for_review(meeting, current_user.username)
    except MinutesWorkflowError as exc:
        return _fail(meeting_id, exc)
    draft = await seal_minutes_draft(db, meeting, submitted_by=current_user.username)
    await _audit(
        db,
        request,
        current_user,
        "minutes_submit",
        meeting_id,
        f"version={meeting.minutes_version} draft_sha256={draft.sha256 if draft else '-'}",
    )
    await db.commit()
    return _back(meeting_id, "minutes_submitted")


@router.post("/meetings/{meeting_id}/minutes/reject")
async def reject_minutes(
    request: Request,
    meeting_id: int,
    note: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _locked_meeting(db, meeting_id)
    try:
        reject(meeting, current_user.username, note)
    except MinutesWorkflowError as exc:
        return _fail(meeting_id, exc)
    await _audit(db, request, current_user, "minutes_reject", meeting_id, f"version={meeting.minutes_version} note={note.strip()}")
    await db.commit()
    return _back(meeting_id, "minutes_returned")


@router.post("/meetings/{meeting_id}/minutes/approve")
async def approve_minutes(
    request: Request,
    meeting_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Lock the minutes, archive them as a write-once PDF version, and index the approved text."""
    from ..ports.events import publish_event
    from .ingest import dispatch_after_commit

    require_meeting_organizer(current_user)
    meeting = await _locked_meeting(db, meeting_id)
    try:
        digest = approve(meeting, current_user.username)
    except MinutesWorkflowError as exc:
        if exc.status_code == 403:
            await _audit(db, request, current_user, "minutes_approve_denied", meeting_id, str(exc))
            await db.commit()
        return _fail(meeting_id, exc)
    try:
        result = await archive_approved_minutes(db, meeting, approved_by=current_user.username)
        await link_archive_document(
            db,
            meeting_id,
            KIND_MINUTES_APPROVED,
            result,
            created_by=current_user.username,
            filename=f"minutes-v{int(meeting.minutes_version or 1)}.pdf",
        )
        transcript = await archive_transcript(db, meeting, approved_by=current_user.username)
    except Exception:
        logger.exception("Archiving approved minutes failed for meeting %s", meeting_id)
        await db.rollback()
        return _back(meeting_id, "minutes_archive_failed")
    await _audit(
        db,
        request,
        current_user,
        "minutes_approve",
        meeting_id,
        f"version={meeting.minutes_version} sha256={digest} doc_id={result.document.doc_id} "
        f"doc_version={result.version.version} pdf_sha256={result.version.sha256}"
        + (f" transcript_doc={transcript[0].document.doc_id} v{transcript[0].version.version}" if transcript else ""),
    )
    await db.commit()
    dispatch_after_commit(result, background_tasks)
    if transcript:
        dispatch_after_commit(transcript[0], background_tasks)
    publish_event(
        "meeting.minutes_approved",
        {
            "meeting_id": meeting_id,
            "minutes_version": meeting.minutes_version,
            "doc_id": result.document.doc_id,
            "doc_version": result.version.version,
            "sha256": digest,
            "approved_by": current_user.username,
        },
    )
    return _back(meeting_id, "minutes_approved")


@router.post("/meetings/{meeting_id}/minutes/amend")
async def amend_minutes(
    request: Request,
    meeting_id: int,
    reason: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    meeting = await _locked_meeting(db, meeting_id)
    try:
        version = amend(meeting, current_user.username, reason)
    except MinutesWorkflowError as exc:
        return _fail(meeting_id, exc)
    await _audit(db, request, current_user, "minutes_amend", meeting_id, f"new_version={version} reason={reason.strip()}")
    await db.commit()
    return _back(meeting_id, "minutes_amending")


@router.get("/api/meetings/{meeting_id}/minutes/workflow")
async def minutes_workflow_api(
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    require_meeting_organizer(current_user)
    meeting = await db.get(BoardMeeting, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    items = await list_items(db, meeting_id)
    return JSONResponse(
        {
            "workflow": workflow_view(meeting, current_user.username),
            "decisions": [decision_payload(d) for d in items["decisions"]],
            "actions": [action_payload(a) for a in items["actions"]],
        }
    )


# --- Decisions and actions -------------------------------------------------------------

def _clean(value: Optional[str], limit: int) -> Optional[str]:
    text = (value or "").strip()
    return text[:limit] if text else None


async def _decision_or_404(db: AsyncSession, meeting_id: int, decision_id: int) -> MeetingDecision:
    row = await db.get(MeetingDecision, decision_id)
    if row is None or row.meeting_id != meeting_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Decision not found")
    return row


async def _action_or_404(db: AsyncSession, meeting_id: int, action_id: int) -> MeetingAction:
    row = await db.get(MeetingAction, action_id)
    if row is None or row.meeting_id != meeting_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Action not found")
    return row


async def _decision_link(db: AsyncSession, meeting_id: int, raw: str) -> Optional[int]:
    if not (raw or "").strip():
        return None
    try:
        return (await _decision_or_404(db, meeting_id, int(raw))).id
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid decision") from exc


@router.post("/meetings/{meeting_id}/decisions")
async def add_decision(
    request: Request,
    meeting_id: int,
    text: str = Form(..., min_length=3, max_length=4000),
    agenda_item: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _locked_meeting(db, meeting_id)
    try:
        require_draft(meeting, "Decisions")
    except MinutesWorkflowError as exc:
        return _fail(meeting_id, exc, "decisions")
    seq = int(await db.scalar(select(func.max(MeetingDecision.seq)).where(MeetingDecision.meeting_id == meeting_id)) or 0) + 1
    row = MeetingDecision(
        meeting_id=meeting_id,
        seq=seq,
        agenda_item=_clean(agenda_item, 255),
        text=text.strip(),
        source="manual",
        created_by=current_user.username,
    )
    db.add(row)
    await db.flush()
    await _audit(db, request, current_user, "decision_add", meeting_id, f"decision_id={row.id} seq={seq}")
    await db.commit()
    return _back(meeting_id, "decision_saved", "decisions")


@router.post("/meetings/{meeting_id}/decisions/{decision_id}/edit")
async def edit_decision(
    request: Request,
    meeting_id: int,
    decision_id: int,
    text: str = Form(..., min_length=3, max_length=4000),
    agenda_item: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _locked_meeting(db, meeting_id)
    try:
        require_draft(meeting, "Decisions")
    except MinutesWorkflowError as exc:
        return _fail(meeting_id, exc, "decisions")
    row = await _decision_or_404(db, meeting_id, decision_id)
    if row.text != text.strip() and row.source == "ai":
        row.source = "edited"
    row.text = text.strip()
    row.agenda_item = _clean(agenda_item, 255)
    row.updated_by = current_user.username
    row.updated_at = datetime.utcnow()
    await _audit(db, request, current_user, "decision_edit", meeting_id, f"decision_id={decision_id}")
    await db.commit()
    return _back(meeting_id, "decision_saved", "decisions")


@router.post("/meetings/{meeting_id}/decisions/{decision_id}/delete")
async def delete_decision(
    request: Request,
    meeting_id: int,
    decision_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _locked_meeting(db, meeting_id)
    try:
        require_draft(meeting, "Decisions")
    except MinutesWorkflowError as exc:
        return _fail(meeting_id, exc, "decisions")
    row = await _decision_or_404(db, meeting_id, decision_id)
    await db.execute(update(MeetingAction).where(MeetingAction.decision_id == row.id).values(decision_id=None))
    await db.delete(row)
    await _audit(db, request, current_user, "decision_delete", meeting_id, f"decision_id={decision_id} text={row.text[:200]}")
    await db.commit()
    return _back(meeting_id, "decision_deleted", "decisions")


@router.post("/meetings/{meeting_id}/actions")
async def add_action(
    request: Request,
    meeting_id: int,
    description: str = Form(..., min_length=3, max_length=4000),
    owner: str = Form(""),
    division: str = Form(""),
    due_date: str = Form(""),
    decision_id: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _locked_meeting(db, meeting_id)
    try:
        require_draft(meeting, "Actions")
    except MinutesWorkflowError as exc:
        return _fail(meeting_id, exc, "decisions")
    row = MeetingAction(
        meeting_id=meeting_id,
        decision_id=await _decision_link(db, meeting_id, decision_id),
        description=description.strip(),
        owner=_clean(owner, 128),
        division=_clean(division, 128),
        due_date=parse_due_date(due_date),
        source="manual",
        created_by=current_user.username,
    )
    db.add(row)
    await db.flush()
    await _audit(db, request, current_user, "action_add", meeting_id, f"action_id={row.id} owner={row.owner or ''}")
    await db.commit()
    return _back(meeting_id, "action_saved", "decisions")


@router.post("/meetings/{meeting_id}/actions/{action_id}/edit")
async def edit_action(
    request: Request,
    meeting_id: int,
    action_id: int,
    description: str = Form(..., min_length=3, max_length=4000),
    owner: str = Form(""),
    division: str = Form(""),
    due_date: str = Form(""),
    decision_id: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _locked_meeting(db, meeting_id)
    try:
        require_draft(meeting, "Actions")
    except MinutesWorkflowError as exc:
        return _fail(meeting_id, exc, "decisions")
    row = await _action_or_404(db, meeting_id, action_id)
    if row.source == "ai" and row.description != description.strip():
        row.source = "edited"
    row.description = description.strip()
    row.owner = _clean(owner, 128)
    row.division = _clean(division, 128)
    row.due_date = parse_due_date(due_date)
    row.decision_id = await _decision_link(db, meeting_id, decision_id)
    row.updated_by = current_user.username
    row.updated_at = datetime.utcnow()
    await _audit(db, request, current_user, "action_edit", meeting_id, f"action_id={action_id}")
    await db.commit()
    return _back(meeting_id, "action_saved", "decisions")


@router.post("/meetings/{meeting_id}/actions/{action_id}/status")
async def update_action_status(
    request: Request,
    meeting_id: int,
    action_id: int,
    action_status: str = Form(..., alias="status"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Progress tracking stays open after approval; it does not change what was decided."""
    require_meeting_organizer(current_user)
    row = await _action_or_404(db, meeting_id, action_id)
    previous = row.status
    try:
        set_action_status(row, action_status, current_user.username)
    except MinutesWorkflowError as exc:
        return _fail(meeting_id, exc, "decisions")
    await _audit(db, request, current_user, "action_status", meeting_id, f"action_id={action_id} {previous}->{row.status}")
    await db.commit()
    return _back(meeting_id, "action_saved", "decisions")


@router.post("/meetings/{meeting_id}/actions/{action_id}/delete")
async def delete_action(
    request: Request,
    meeting_id: int,
    action_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _locked_meeting(db, meeting_id)
    try:
        require_draft(meeting, "Actions")
    except MinutesWorkflowError as exc:
        return _fail(meeting_id, exc, "decisions")
    row = await _action_or_404(db, meeting_id, action_id)
    await db.delete(row)
    await _audit(db, request, current_user, "action_delete", meeting_id, f"action_id={action_id} text={row.description[:200]}")
    await db.commit()
    return _back(meeting_id, "action_deleted", "decisions")


@router.post("/meetings/{meeting_id}/minutes/extract")
async def extract_minutes_items(
    request: Request,
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Suggest decisions and actions from the draft; each must quote the transcript."""
    require_meeting_organizer(current_user)
    meeting = await _locked_meeting(db, meeting_id)
    try:
        require_draft(meeting, "Decisions")
    except MinutesWorkflowError as exc:
        return _fail(meeting_id, exc, "decisions")
    extracted = await asyncio.to_thread(
        extract_decisions_and_actions,
        meeting.minutes_draft or "",
        list(meeting.transcription_json or []),
        sensitivity=meeting_sensitivity(),
    )
    counts = await replace_ai_items(db, meeting_id, extracted, actor=current_user.username)
    await _audit(
        db, request, current_user, "minutes_extract", meeting_id, f"decisions={counts['decisions']} actions={counts['actions']}"
    )
    await db.commit()
    return _back(meeting_id, "items_extracted", "decisions")


# --- Recorded-file upload and speakers -------------------------------------------------

def _safe_filename(name: Optional[str]) -> str:
    base = re.sub(r"[^\w.\- ()\u0980-\u09FF]+", "_", (name or "recording").split("/")[-1].split("\\")[-1]).strip()
    return (base or "recording")[:255]


@router.post("/meetings/{meeting_id}/recording", dependencies=[Depends(rate_limit("recording"))])
async def upload_meeting_recording(
    request: Request,
    meeting_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """A sitting recorded on another device: store it, then transcribe and draft minutes in the worker."""
    require_meeting_organizer(current_user)
    meeting = await _locked_meeting(db, meeting_id)
    if _meeting_transcription_status(meeting) in {TRANSCRIPTION_LIVE, TRANSCRIPTION_PROCESSING}:
        return _back(meeting_id, "transcription_already_live", "transcription")
    if minutes_state(meeting) != "draft":
        return _back(meeting_id, "minutes_locked")

    limit = recording_max_bytes()
    head = await file.read(64)
    extension = sniff_recording(head)
    if extension is None:
        return _back(meeting_id, "recording_unsupported", "transcription")
    partial = meeting_audio_dir(meeting_id) / f"{RECORDING_STEM}-upload.part"
    digest = hashlib.sha256(head)
    size = len(head)
    try:
        with partial.open("wb") as out:
            out.write(head)
            while True:
                chunk = await file.read(UPLOAD_READ_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ValueError("too large")
                digest.update(chunk)
                out.write(chunk)
    except ValueError:
        partial.unlink(missing_ok=True)
        return _back(meeting_id, "recording_too_large", "transcription")
    clear_recording(meeting_id)
    partial.rename(meeting_audio_dir(meeting_id) / f"{RECORDING_STEM}{extension}")

    now = datetime.utcnow()
    meeting.recording_filename = _safe_filename(file.filename)
    meeting.recording_sha256 = digest.hexdigest()
    meeting.transcription_status = TRANSCRIPTION_PROCESSING
    meeting.transcription_started_at = meeting.scheduled_at or now
    meeting.transcription_stopped_at = now
    meeting.transcription_started_by = current_user.username
    meeting.transcription_json = [
        {"id": 1, "speaker": "System", "text": "Transcribing the uploaded recording…", "dummy": False}
    ]
    flag_modified(meeting, "transcription_json")
    meeting.diarization_status = None
    meeting.minutes_status = MINUTES_PENDING
    await _audit(
        db,
        request,
        current_user,
        "recording_upload",
        meeting_id,
        f"name={meeting.recording_filename} bytes={size} type={extension} sha256={meeting.recording_sha256}",
    )
    await db.commit()

    queued = False
    if job_queue_enabled():
        queued = bool(await enqueue_job(JOB_MEETING_TRANSCRIBE, {"meeting_id": meeting_id, "source": "recording"}))
    if not queued:
        background_tasks.add_task(run_meeting_transcribe, meeting_id, "recording")
    return _back(meeting_id, "recording_processing", "transcription")


@router.post("/meetings/{meeting_id}/speakers")
async def update_speaker_names(
    request: Request,
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Name diarized speakers ("Speaker 1" -> "Chairman"). The transcript is minutes evidence,
    so names are fixed once the minutes leave draft."""
    require_meeting_organizer(current_user)
    meeting = await _locked_meeting(db, meeting_id)
    try:
        require_draft(meeting, "Speaker names")
    except MinutesWorkflowError as exc:
        return _fail(meeting_id, exc, "transcription")
    form = await request.form()
    segments = list(meeting.transcription_json or [])
    labels = [s["label"] for s in speaker_labels(segments)]
    raw: dict[str, Any] = {k[len("name_"):]: v for k, v in form.items() if k.startswith("name_")}
    mapping = clean_speaker_map(raw, labels)
    meeting.speaker_map = mapping
    meeting.transcription_json = apply_speaker_map(segments, mapping)
    flag_modified(meeting, "speaker_map")
    flag_modified(meeting, "transcription_json")
    await _audit(db, request, current_user, "speaker_map", meeting_id, f"named={len(mapping)} of {len(labels)}")
    await db.commit()
    return _back(meeting_id, "speakers_saved", "transcription")


# --- Sealed meeting records --------------------------------------------------------------

@router.get("/api/meetings/{meeting_id}/records")
async def api_meeting_records(
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Recording, transcript, draft-minutes versions, and approved minutes, each with its SHA-256."""
    require_meeting_organizer(current_user)
    if await db.get(BoardMeeting, meeting_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    return {"records": [record_payload(row) for row in await list_records(db, meeting_id)]}


@router.get("/meetings/{meeting_id}/records/{record_id}/file")
async def download_meeting_record(
    request: Request,
    meeting_id: int,
    record_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Stream a sealed record (recordings can be hours of video, so never read it into memory)."""
    from .. import deps
    from ..aws_utils import get_s3_client

    require_meeting_organizer(current_user)
    row = await db.get(MeetingRecord, record_id)
    if row is None or row.meeting_id != meeting_id or not row.file_location:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")
    name = (row.filename or f"{row.kind}-v{row.version}").encode("ascii", "replace").decode().replace("?", "_")
    headers = {
        "Content-Disposition": f'attachment; filename="{_safe_filename(name)}"',
        "X-Content-SHA256": row.sha256,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    media_type = row.content_type or "application/octet-stream"
    location = row.file_location.strip()
    if location.startswith("s3://"):
        bucket, _, key = location[len("s3://"):].partition("/")
        try:
            body = await asyncio.to_thread(lambda: get_s3_client().get_object(Bucket=bucket, Key=key)["Body"])
        except Exception as exc:
            logger.exception("S3 get_object failed for meeting record %s", row.id)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to fetch the record from storage") from exc
        response: Response = StreamingResponse(body.iter_chunks(UPLOAD_READ_BYTES), media_type=media_type, headers=headers)
    else:
        path = Path(location)
        if not path.is_absolute():
            path = deps.UPLOAD_DIR / location
        path = path.resolve()
        if not path.is_relative_to(deps.UPLOAD_DIR.resolve()) or not path.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record file missing from storage")
        response = FileResponse(path, media_type=media_type, headers=headers)
    await _audit(db, request, current_user, "meeting_record_download", meeting_id, f"kind={row.kind} v{row.version} sha256={row.sha256}")
    await db.commit()
    return response
