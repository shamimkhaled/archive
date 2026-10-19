"""Phase 6: minutes review workflow, approved-only archive, decisions/actions, recordings, diarization."""
from __future__ import annotations

import io
import re
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

TRANSCRIPT = [
    {"id": 1, "speaker": "Chair", "text": "The board approves the revised loan policy for FY27.", "t0": 0.0, "t1": 4.0},
    {"id": 2, "speaker": "CFO", "text": "Finance will circulate the branch report by 30 November.", "t0": 4.0, "t1": 9.0},
    {"id": 3, "speaker": "Chair", "text": "বোর্ড শাখা পর্যালোচনা অনুমোদন করেছে।", "t0": 9.0, "t1": 12.0},
]
APPROVED_BODY = (
    "# Minutes — Board meeting 12\n\n"
    "## Decisions\n- The board approved the revised loan policy for FY27.\n- বোর্ড শাখা পর্যালোচনা অনুমোদন করেছে।\n\n"
    "## Actions\n| Action | Owner | Due |\n|---|---|---|\n| Circulate branch report | CFO | 2026-11-30 |\n"
)


def _meeting(**kw):
    from bcp_project.models import BoardMeeting

    fields = dict(
        id=12,
        title="Board meeting 12",
        scheduled_at=datetime(2026, 10, 1, 10, 0),
        location="Head office",
        created_by="sec",
        minutes_status="ready",
        minutes_draft="# Draft minutes\n\nThe board approved the loan policy.",
        minutes_source="llm",
        minutes_state="draft",
        minutes_version=1,
        transcription_json=list(TRANSCRIPT),
    )
    fields.update(kw)
    return BoardMeeting(**fields)


def _request():
    from starlette.requests import Request

    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("10.0.0.5", 1234)})


def _user(name, role="board_secretary"):
    from bcp_project.models import Role

    return SimpleNamespace(username=name, role=Role(role))


# --- PDF rendering ---------------------------------------------------------------------

def test_minutes_pdf_maps_every_word_to_a_page_and_keeps_bangla():
    from pypdf import PdfReader

    from bcp_project.minutes_pdf import render_minutes_pdf

    words = " ".join(f"w{i}" for i in range(1500))
    rows = "\n".join(f"| Item {i} | Owner {i} | 2026-11-{i % 28 + 1:02d} |" for i in range(60))
    body = f"{APPROVED_BODY}\n| A | B | C |\n|---|---|---|\n{rows}\n\nLong record {words}\n\nশেষ অনুচ্ছেদ **সমাপ্ত**"
    pdf, pages = render_minutes_pdf(
        title="Board 12",
        body_md=body,
        meeting_date=datetime(2026, 10, 1, 10),
        location="HQ",
        version=2,
        approved_by="chair",
        approved_at=datetime(2026, 10, 2, 9),
        sha256="a" * 64,
    )
    assert pdf.startswith(b"%PDF") and len(pages) == len(PdfReader(io.BytesIO(pdf)).pages) > 2
    text = " ".join(pages)
    assert set(re.findall(r"\bw\d+\b", text)) == {f"w{i}" for i in range(1500)}
    assert "Item 59 | Owner 59" in text
    assert "বোর্ড শাখা পর্যালোচনা অনুমোদন করেছে।" in pages[0]
    assert "শেষ অনুচ্ছেদ সমাপ্ত" in text
    assert "Circulate branch report | CFO | 2026-11-30" in pages[0]


def test_minutes_pdf_mixed_script_words_render_and_fallback(monkeypatch):
    import bcp_project.minutes_pdf as mp

    body = "- ঋণ(loan) নীতি; FY27-এর বাজেট ok।"
    kwargs = dict(title="T", body_md=body, meeting_date=None, location=None, version=1,
                  approved_by="x", approved_at=datetime(2026, 1, 1), sha256="0" * 64)
    _, pages = mp.render_minutes_pdf(**kwargs)
    assert "ঋণ(loan) নীতি; FY27-এর বাজেট ok।" in pages[0]

    real_render = mp._render

    def shaped_fails(*, shape, **kw):
        if shape:
            raise AttributeError("shaping bug")
        return real_render(shape=False, **kw)

    monkeypatch.setattr(mp, "_can_shape", lambda: True)
    monkeypatch.setattr(mp, "_render", shaped_fails)
    pdf, pages = mp.render_minutes_pdf(**kwargs)
    assert pdf.startswith(b"%PDF") and "FY27-এর" in pages[0]


def test_minutes_pdf_escapes_markup():
    from bcp_project.minutes_pdf import render_minutes_pdf

    _, pages = render_minutes_pdf(
        title="T", body_md="Board <script>alert(1)</script> & co", meeting_date=None, location=None,
        version=1, approved_by="x", approved_at=datetime(2026, 1, 1), sha256="0" * 64,
    )
    assert "<script>alert(1)</script> & co" in pages[0]


def test_minutes_pdf_bank_template_letterhead_and_signatures(monkeypatch):
    from bcp_project.minutes_pdf import render_minutes_pdf
    from bcp_project.minutes_template import load_minutes_template

    monkeypatch.setenv("MINUTES_TEMPLATE_LANG", "en")
    monkeypatch.setenv("MINUTES_SHOW_SIGNATURES", "1")
    monkeypatch.setenv("MINUTES_SHOW_LOGO", "1")
    monkeypatch.setenv("MINUTES_HEADER_EN", "Sonali Bank PLC — Test Minutes")
    monkeypatch.setenv("MINUTES_DOC_NUMBER_FMT", "MIN-{meeting_id}")
    tmpl = load_minutes_template()
    assert tmpl.lang == "en" and tmpl.header_en.startswith("Sonali Bank")
    pdf, pages = render_minutes_pdf(
        title="Board 99",
        body_md="# Decisions\n\n- Approve the policy.",
        meeting_date=datetime(2026, 10, 1, 10),
        location="HQ",
        version=3,
        approved_by="chair",
        approved_at=datetime(2026, 10, 2, 9),
        sha256="b" * 64,
        meeting_id=99,
        template=tmpl,
    )
    assert pdf.startswith(b"%PDF")
    text = " ".join(pages)
    assert "Sonali Bank PLC — Test Minutes" in text
    assert "Document no. MIN-99" in text
    assert "Chairperson" in text
    assert "Approved by chair" in text


def test_minutes_template_bn_numbering(monkeypatch):
    from bcp_project.minutes_template import format_doc_number, load_minutes_template

    monkeypatch.setenv("MINUTES_TEMPLATE_LANG", "bn")
    monkeypatch.setenv("MINUTES_DOC_NUMBER_FMT", "SB-{year}-MIN-{meeting_id}")
    tmpl = load_minutes_template()
    assert tmpl.prefer_bn and "সোনালী" in tmpl.header_bn
    assert format_doc_number(meeting_id=7, version=1, meeting_date_year=2026) == "SB-2026-MIN-7"


# --- Workflow --------------------------------------------------------------------------

def test_edit_marks_source_edited_and_lock_blocks_changes(monkeypatch):
    from bcp_project.services import minutes as wf

    monkeypatch.setenv("MINUTES_FOUR_EYES", "0")
    meeting = _meeting()
    assert wf.save_edit(meeting, meeting.minutes_draft, "sec") is False
    assert meeting.minutes_source == "llm" and meeting.minutes_edited_by is None
    assert wf.save_edit(meeting, "Changed text", "sec") is True
    assert meeting.minutes_source == "edited" and meeting.minutes_edited_by == "sec"

    wf.submit_for_review(meeting, "sec")
    assert meeting.minutes_state == "in_review"
    with pytest.raises(wf.MinutesWorkflowError):
        wf.save_edit(meeting, "Sneaky change", "sec")
    with pytest.raises(wf.MinutesWorkflowError):
        wf.submit_for_review(meeting, "sec")

    with pytest.raises(wf.MinutesWorkflowError) as missing_note:
        wf.reject(meeting, "chair", " ")
    assert missing_note.value.status_code == 400
    wf.reject(meeting, "chair", "Add the attendance list")
    assert meeting.minutes_state == "draft" and "attendance" in meeting.minutes_review_note

    with pytest.raises(wf.MinutesWorkflowError):
        wf.approve(meeting, "chair")
    wf.submit_for_review(meeting, "sec")
    digest = wf.approve(meeting, "sec")
    assert meeting.minutes_state == "approved" and digest == wf.body_sha256("Changed text")
    with pytest.raises(wf.MinutesWorkflowError):
        wf.save_edit(meeting, "after approval", "sec")


def test_four_eyes_rule(monkeypatch):
    from bcp_project.services import minutes as wf

    monkeypatch.setenv("MINUTES_FOUR_EYES", "1")
    meeting = _meeting()
    wf.save_edit(meeting, "Edited by alice", "alice")
    wf.submit_for_review(meeting, "bob")
    for blocked in ("alice", "bob"):
        with pytest.raises(wf.MinutesWorkflowError) as exc:
            wf.approve(meeting, blocked)
        assert exc.value.status_code == 403
    assert wf.workflow_view(meeting, "bob")["approve_blocker"].startswith("Four-eyes")
    wf.approve(meeting, "carol")
    assert meeting.minutes_approved_by == "carol"

    monkeypatch.delenv("MINUTES_FOUR_EYES")
    monkeypatch.setenv("APP_ENV", "production")
    assert wf.four_eyes_required() is True
    monkeypatch.setenv("APP_ENV", "development")
    assert wf.four_eyes_required() is False


def test_amend_reopens_as_next_version(monkeypatch):
    from bcp_project.services import minutes as wf

    monkeypatch.setenv("MINUTES_FOUR_EYES", "0")
    meeting = _meeting()
    wf.submit_for_review(meeting, "sec")
    wf.approve(meeting, "chair")
    with pytest.raises(wf.MinutesWorkflowError):
        wf.amend(meeting, "admin", "")
    assert wf.amend(meeting, "admin", "Correct the date") == 2
    assert meeting.minutes_state == "draft" and meeting.minutes_submitted_by is None


# --- Approval archives and indexes only approved text ----------------------------------

@pytest_asyncio.fixture
async def maker(tmp_path):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from bcp_project.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'p6.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def offline(monkeypatch, tmp_path):
    import bcp_project.cache as cache
    import bcp_project.deps as deps
    import bcp_project.ports.events as events
    import bcp_project.qdrant_store as qs
    import bcp_project.services.ingest as ingest

    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("AI_EGRESS", "internal_only")
    monkeypatch.setenv("MINUTES_FOUR_EYES", "1")
    monkeypatch.delenv("AV_SCAN", raising=False)
    for name in ("LOCAL_LLM_BASE_URL", "LOCAL_EMBED_BASE_URL", "JOB_STREAM_ENABLED", "JOB_QUEUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(deps, "UPLOAD_DIR", tmp_path)
    qdrant = SimpleNamespace(create_collections=lambda: None, delete_document_chunks=lambda doc_id: None)
    monkeypatch.setattr(qs, "make_qdrant_indexer", lambda: qdrant)
    monkeypatch.setattr(ingest, "index_summary_vector", lambda *a, **k: True)
    indexed = []
    monkeypatch.setattr(
        "bcp_project.jobs.handlers.run_chunk_index",
        lambda doc_id, page_payloads, sensitivity=None: indexed.append((doc_id, page_payloads)) or len(page_payloads),
    )
    monkeypatch.setattr(cache, "bump_search_cache_version", AsyncMock())
    published = []
    monkeypatch.setattr(events, "publish_event", lambda name, payload: published.append((name, payload)) or True)
    monkeypatch.setattr("bcp_project.jobs.queue.job_queue_enabled", lambda: False)
    monkeypatch.setattr("bcp_project.services.document_versions.job_queue_enabled", lambda: False)
    return SimpleNamespace(indexed=indexed, published=published)


@pytest.mark.asyncio
async def test_approval_route_archives_and_indexes_only_approved_minutes(maker, offline):
    from fastapi import BackgroundTasks

    from bcp_project.models import AuditLog, BoardMeeting, DocumentPage, DocumentRecord, DocumentVersion
    from bcp_project.routers import meeting_minutes as routes
    from bcp_project.routers import meetings as meeting_routes
    from bcp_project.services.ingest import run_document_ingest

    async with maker() as db:
        db.add(_meeting(minutes_draft="Unreviewed draft text"))
        await db.commit()

    sec, chair = _user("sec"), _user("chair")
    async with maker() as db:
        await meeting_routes.save_meeting_minutes(_request(), 12, current_user=sec, db=db, body_md=APPROVED_BODY)
    async with maker() as db:
        resp = await routes.submit_minutes(_request(), 12, current_user=sec, db=db)
        assert "minutes_submitted" in resp.headers["location"]
    async with maker() as db:
        denied = await routes.approve_minutes(_request(), 12, BackgroundTasks(), current_user=sec, db=db)
        assert "minutes_four_eyes" in denied.headers["location"]
        assert (await db.execute(select(DocumentRecord))).first() is None

    tasks = BackgroundTasks()
    async with maker() as db:
        resp = await routes.approve_minutes(_request(), 12, tasks, current_user=chair, db=db)
        assert "minutes_approved" in resp.headers["location"]
    # Minutes and the transcript (TRN-12) are both archived and dispatched for indexing.
    assert len(tasks.tasks) == 2 and ("meeting.minutes_approved" in [n for n, _ in offline.published])

    # The staged ingest skips OCR (version is reviewed) and indexes the approved text.
    assert await run_document_ingest("MIN-12", version=1, session_factory=maker) == "indexed"
    async with maker() as db:
        meeting = await db.get(BoardMeeting, 12)
        doc = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == "MIN-12"))).scalar_one()
        ver = (await db.execute(select(DocumentVersion).where(DocumentVersion.doc_id == "MIN-12"))).scalar_one()
        pages = (
            await db.execute(select(DocumentPage).where(DocumentPage.doc_id == "MIN-12").order_by(DocumentPage.page_number))
        ).scalars().all()
        audit = [a.action for a in (await db.execute(select(AuditLog).order_by(AuditLog.id))).scalars()]
    assert meeting.minutes_state == "approved" and meeting.minutes_doc_id == "MIN-12"
    assert meeting.minutes_source == "edited" and meeting.minutes_edited_by == "sec"
    assert doc.doc_type == "Board Minutes" and doc.meeting_id == 12 and doc.sensitivity == "confidential"
    assert doc.verification_status != "pending" and doc.ingest_status == "indexed"
    assert ver.status == "approved" and ver.reviewed_by == "chair" and ver.content_type == "application/pdf"
    assert all(p.source == "approved" for p in pages)
    indexed_text = " ".join(p["text"] for _, payloads in offline.indexed for p in payloads)
    assert "revised loan policy" in indexed_text and "Unreviewed draft text" not in indexed_text
    assert all(p["metadata"]["text_source"] == "approved" for _, payloads in offline.indexed for p in payloads)
    assert audit == ["minutes_save", "minutes_submit", "minutes_approve_denied", "minutes_approve"]

    # Amendment: the approved v1 stays current until v2 is approved and indexed.
    admin = _user("root", "admin")
    async with maker() as db:
        await routes.amend_minutes(_request(), 12, reason="Correct due date", current_user=admin, db=db)
    async with maker() as db:
        locked = await routes.approve_minutes(_request(), 12, BackgroundTasks(), current_user=chair, db=db)
        assert "minutes_locked" in locked.headers["location"]
        await meeting_routes.save_meeting_minutes(
            _request(), 12, current_user=sec, db=db, body_md=APPROVED_BODY.replace("2026-11-30", "2026-12-15")
        )
    async with maker() as db:
        await routes.submit_minutes(_request(), 12, current_user=sec, db=db)
    async with maker() as db:
        await routes.approve_minutes(_request(), 12, BackgroundTasks(), current_user=chair, db=db)
    async with maker() as db:
        assert (await db.get(DocumentRecord, doc.id)).current_version == 1
    assert await run_document_ingest("MIN-12", version=2, session_factory=maker) == "indexed"
    async with maker() as db:
        versions = {
            v.version: v.status
            for v in (await db.execute(select(DocumentVersion).where(DocumentVersion.doc_id == "MIN-12"))).scalars()
        }
        assert versions == {1: "superseded", 2: "approved"}
        assert (await db.get(BoardMeeting, 12)).minutes_version == 2


@pytest.mark.asyncio
async def test_meeting_records_seal_recording_transcript_and_every_minutes_version(maker, offline, tmp_path):
    import hashlib
    import os
    import stat
    from pathlib import Path

    from fastapi import BackgroundTasks

    from bcp_project.models import AuditLog, BoardMeeting, DocumentRecord, MeetingRecord
    from bcp_project.routers import meeting_minutes as routes
    from bcp_project.routers import meetings as meeting_routes
    from bcp_project.services.meeting_records import seal_recording

    audio = tmp_path / "board-12.wav"
    audio.write_bytes(b"RIFF" + os.urandom(4096))
    async with maker() as db:
        db.add(_meeting())
        await db.flush()
        first = await seal_recording(db, 12, audio, created_by="sec", filename="সভা-১২.wav")
        assert (await seal_recording(db, 12, audio, created_by="sec")).id == first.id
        await db.commit()

    sec, chair = _user("sec"), _user("chair")
    async with maker() as db:
        await meeting_routes.save_meeting_minutes(_request(), 12, current_user=sec, db=db, body_md=APPROVED_BODY)
    async with maker() as db:
        await routes.submit_minutes(_request(), 12, current_user=sec, db=db)
    async with maker() as db:
        await routes.approve_minutes(_request(), 12, BackgroundTasks(), current_user=chair, db=db)

    async with maker() as db:
        payload = await routes.api_meeting_records(12, current_user=sec, db=db)
        rows = (await db.execute(select(MeetingRecord))).scalars().all()
        transcript_doc = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == "TRN-12"))).scalar_one()
        saved_draft = (await db.get(BoardMeeting, 12)).minutes_draft
    kinds = {r["kind"] for r in payload["records"]}
    assert kinds == {"recording", "transcript", "minutes_draft", "minutes_approved"}
    assert transcript_doc.meeting_id == 12 and transcript_doc.doc_type == "Meeting Transcript"
    assert {r["archive_doc_id"] for r in payload["records"] if r["kind"] in ("transcript", "minutes_approved")} == {"TRN-12", "MIN-12"}

    for row in rows:
        stored = (tmp_path / row.file_location) if not os.path.isabs(row.file_location) else Path(row.file_location)
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == row.sha256
        assert not stored.stat().st_mode & stat.S_IWUSR
    draft = next(r for r in rows if r.kind == "minutes_draft")
    assert hashlib.sha256(saved_draft.encode()).hexdigest() == draft.sha256 and "revised loan policy" in saved_draft

    from bcp_project.intake import WriteOnceViolation, store_write_once

    with pytest.raises(WriteOnceViolation):
        store_write_once(b"overwrite", draft.file_location, content_type="text/plain", sha256="0" * 64)

    recording = next(r for r in rows if r.kind == "recording")
    async with maker() as db:
        resp = await routes.download_meeting_record(_request(), 12, recording.id, current_user=sec, db=db)
        audit = (await db.execute(select(AuditLog.action))).scalars().all()
    assert resp.headers["x-content-sha256"] == hashlib.sha256(audio.read_bytes()).hexdigest()
    assert 'filename="' in resp.headers["content-disposition"] and "meeting_record_download" in audit
    with pytest.raises(Exception) as missing:
        async with maker() as db:
            await routes.download_meeting_record(_request(), 99, recording.id, current_user=sec, db=db)
    assert getattr(missing.value, "status_code", None) == 404

    # Unchanged transcript is not re-archived on amendment; the new draft is sealed as v2.
    async with maker() as db:
        await routes.amend_minutes(_request(), 12, reason="Fix", current_user=_user("root", "admin"), db=db)
    async with maker() as db:
        await meeting_routes.save_meeting_minutes(_request(), 12, current_user=sec, db=db, body_md=APPROVED_BODY + "\nAmended.")
    async with maker() as db:
        await routes.submit_minutes(_request(), 12, current_user=sec, db=db)
    async with maker() as db:
        await routes.approve_minutes(_request(), 12, BackgroundTasks(), current_user=chair, db=db)
    async with maker() as db:
        counts = {}
        for row in (await db.execute(select(MeetingRecord))).scalars():
            counts[row.kind] = counts.get(row.kind, 0) + 1
    assert counts == {"recording": 1, "transcript": 1, "minutes_draft": 2, "minutes_approved": 2}


@pytest.mark.asyncio
async def test_late_minutes_job_does_not_overwrite_locked_minutes(maker, monkeypatch):
    import bcp_project.jobs.handlers as handlers
    from bcp_project.models import BoardMeeting

    async with maker() as db:
        db.add(_meeting(minutes_state="in_review", minutes_draft="Locked text"))
        await db.commit()
    monkeypatch.setattr(handlers, "get_session", maker)
    monkeypatch.setattr(handlers, "generate_minutes_markdown", lambda **kw: pytest.fail("must not regenerate"))
    assert await handlers.run_meeting_minutes(12) is False
    async with maker() as db:
        assert (await db.get(BoardMeeting, 12)).minutes_draft == "Locked text"


# --- Decisions and actions -------------------------------------------------------------

def test_extraction_keeps_only_items_quoted_from_the_transcript(monkeypatch):
    import bcp_project.ai as ai
    from bcp_project.services.minutes import extract_decisions_and_actions

    def fake_chat_json(task, system, user, **kw):
        assert task == "minutes" and "exact quote" in system
        return {
            "decisions": [
                {"text": "Approve revised loan policy", "agenda_item": "Credit", "evidence": "approves the revised loan policy for FY27"},
                {"text": "Close 40 branches", "evidence": "we will close forty branches"},
                {"text": "শাখা পর্যালোচনা অনুমোদন", "evidence": "“বোর্ড শাখা পর্যালোচনা অনুমোদন করেছে।”"},
            ],
            "actions": [
                {"description": "Circulate branch report", "owner": "CFO", "division": " Finance Division ", "due_date": "2026-11-30",
                 "evidence": "Finance will circulate the branch report", "decision_index": 2},
                {"description": "Invented action", "owner": "MD", "evidence": "nobody said this"},
                {"description": "Linked to dropped", "evidence": "circulate the branch report by 30 November", "decision_index": 1},
            ],
        }

    monkeypatch.setattr(ai, "chat_json", fake_chat_json)
    out = extract_decisions_and_actions("draft", TRANSCRIPT)
    assert [d["text"] for d in out["decisions"]] == ["Approve revised loan policy", "শাখা পর্যালোচনা অনুমোদন"]
    assert [a["description"] for a in out["actions"]] == ["Circulate branch report", "Linked to dropped"]
    assert out["actions"][0]["due_date"] == date(2026, 11, 30) and out["actions"][0]["decision_index"] == 1
    assert out["actions"][1]["decision_index"] is None
    assert out["actions"][0]["division"] == "Finance Division" and out["actions"][1]["division"] is None

    def blocked(*a, **k):
        raise ai.EgressBlocked("internal_only")

    monkeypatch.setattr(ai, "chat_json", blocked)
    assert extract_decisions_and_actions("draft", TRANSCRIPT) == {"decisions": [], "actions": []}


@pytest.mark.asyncio
async def test_replacing_ai_items_never_touches_manual_ones(db_session):
    from bcp_project.models import MeetingAction, MeetingDecision
    from bcp_project.services.minutes import list_items, replace_ai_items

    db_session.add(_meeting())
    await db_session.flush()
    manual = MeetingDecision(meeting_id=12, seq=1, text="Manual decision", source="manual", created_by="sec")
    db_session.add(manual)
    await db_session.flush()
    first = {
        "decisions": [{"text": "AI decision", "evidence": "q"}],
        "actions": [{"description": "AI action", "division": "ICT Division", "decision_index": 0}],
    }
    await replace_ai_items(db_session, 12, first)
    assert (await list_items(db_session, 12))["actions"][0].division == "ICT Division"
    ai_decision = (await db_session.execute(select(MeetingDecision).where(MeetingDecision.source == "ai"))).scalar_one()
    db_session.add(MeetingAction(meeting_id=12, decision_id=ai_decision.id, description="Manual follow-up", created_by="sec"))
    await db_session.flush()

    await replace_ai_items(db_session, 12, {"decisions": [{"text": "Fresh AI decision"}], "actions": []})
    items = await list_items(db_session, 12)
    assert [(d.text, d.seq) for d in items["decisions"]] == [("Manual decision", 1), ("Fresh AI decision", 2)]
    assert [(a.description, a.decision_id) for a in items["actions"]] == [("Manual follow-up", None)]


@pytest.mark.asyncio
async def test_decisions_lock_with_minutes_but_action_status_stays_open(maker, offline):
    from bcp_project.models import AuditLog, MeetingAction, MeetingDecision
    from bcp_project.routers import meeting_minutes as routes
    from bcp_project.services.minutes import action_payload

    sec = _user("sec")
    async with maker() as db:
        db.add(_meeting())
        await db.commit()
    async with maker() as db:
        await routes.add_decision(_request(), 12, text="Approve the FY27 budget", agenda_item="Budget", current_user=sec, db=db)
    async with maker() as db:
        decision = (await db.execute(select(MeetingDecision))).scalar_one()
        await routes.add_action(
            _request(), 12, description="Publish budget", owner="CFO", division="Finance Division", due_date="2026-12-01",
            decision_id=str(decision.id), current_user=sec, db=db,
        )
    async with maker() as db:
        payload = action_payload((await db.execute(select(MeetingAction))).scalar_one())
        assert payload["division"] == "Finance Division" and payload["owner"] == "CFO"
    async with maker() as db:
        await routes.submit_minutes(_request(), 12, current_user=sec, db=db)
    async with maker() as db:
        blocked = await routes.add_decision(_request(), 12, text="Late addition", agenda_item="", current_user=sec, db=db)
        assert "minutes_locked" in blocked.headers["location"]
        action = (await db.execute(select(MeetingAction))).scalar_one()
        assert action.due_date == date(2026, 12, 1) and action.decision_id == decision.id
        await routes.update_action_status(_request(), 12, action.id, action_status="done", current_user=sec, db=db)
    async with maker() as db:
        action = (await db.execute(select(MeetingAction))).scalar_one()
        assert action.status == "done" and action.completed_at is not None
        assert len((await db.execute(select(MeetingDecision))).scalars().all()) == 1
        actions = [a.action for a in (await db.execute(select(AuditLog))).scalars()]
        assert actions == ["decision_add", "action_add", "minutes_submit", "action_status"]


# --- Recorded-file upload and diarization ----------------------------------------------

def test_sniff_recording_by_magic_bytes():
    from bcp_project.meeting_audio import sniff_recording

    assert sniff_recording(b"RIFF\x00\x00\x00\x00WAVEfmt ") == ".wav"
    assert sniff_recording(b"ID3\x04\x00") == ".mp3"
    assert sniff_recording(b"\xff\xfb\x90\x00") == ".mp3"
    assert sniff_recording(b"\x00\x00\x00\x20ftypM4A \x00") == ".m4a"
    assert sniff_recording(b"\x00\x00\x00\x20ftypisom\x00") == ".mp4"
    assert sniff_recording(b"\x1a\x45\xdf\xa3\x01") == ".webm"
    assert sniff_recording(b"OggS\x00") == ".ogg"
    assert sniff_recording(b"%PDF-1.7") is None
    assert sniff_recording(b"MZ\x90\x00") is None


def test_recording_kind_splits_video_from_audio():
    from bcp_project.services.meeting_records import KIND_RECORDING, KIND_VIDEO, recording_kind

    assert recording_kind(".wav") == KIND_RECORDING
    assert recording_kind(".mp3") == KIND_RECORDING
    assert recording_kind(".mp4") == KIND_VIDEO
    assert recording_kind(".webm") == KIND_VIDEO
    assert recording_kind(".mkv") == KIND_VIDEO


def test_unified_minutes_diff_marks_changes():
    from bcp_project.services.meeting_records import unified_minutes_diff

    same = unified_minutes_diff(sealed_draft="A\nB", current="A\nB", from_label="draft", to_label="approved")
    assert same["changed"] is False and same["unified"] == ""
    changed = unified_minutes_diff(sealed_draft="A\nB", current="A\nC", from_label="draft", to_label="approved")
    assert changed["changed"] is True and "-B" in changed["unified"] and "+C" in changed["unified"]


@pytest.mark.asyncio
async def test_recording_upload_stores_hashes_and_queues_transcription(maker, offline, monkeypatch):
    import hashlib

    from fastapi import BackgroundTasks
    from starlette.datastructures import UploadFile

    from bcp_project.meeting_audio import recording_path
    from bcp_project.models import AuditLog, BoardMeeting
    from bcp_project.routers import meeting_minutes as routes

    monkeypatch.setattr(routes, "job_queue_enabled", lambda: False)
    data = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 3000
    async with maker() as db:
        db.add(_meeting(minutes_status="off", minutes_draft=None, transcription_json=[]))
        await db.commit()

    sec = _user("sec")
    async with maker() as db:
        bad = await routes.upload_meeting_recording(
            _request(), 12, BackgroundTasks(), file=UploadFile(io.BytesIO(b"MZ" + b"\x00" * 100), filename="a.exe"),
            current_user=sec, db=db,
        )
        assert "recording_unsupported" in bad.headers["location"]

    monkeypatch.setenv("MEETING_RECORDING_MAX_MB", "0.001")
    async with maker() as db:
        big = await routes.upload_meeting_recording(
            _request(), 12, BackgroundTasks(), file=UploadFile(io.BytesIO(data), filename="big.wav"), current_user=sec, db=db
        )
        assert "recording_too_large" in big.headers["location"]
        assert recording_path(12) is None

    monkeypatch.setenv("MEETING_RECORDING_MAX_MB", "5")
    tasks = BackgroundTasks()
    async with maker() as db:
        ok = await routes.upload_meeting_recording(
            _request(), 12, tasks, file=UploadFile(io.BytesIO(data), filename="../../board sitting.wav"), current_user=sec, db=db
        )
        assert "recording_processing" in ok.headers["location"]
    assert recording_path(12).name == "recording.wav" and recording_path(12).read_bytes() == data
    assert len(tasks.tasks) == 1 and tasks.tasks[0].args == (12, "recording")
    async with maker() as db:
        meeting = await db.get(BoardMeeting, 12)
        assert meeting.recording_sha256 == hashlib.sha256(data).hexdigest()
        assert meeting.recording_filename == "board sitting.wav"
        assert meeting.transcription_status == "processing" and meeting.minutes_status == "pending"
        audit = (await db.execute(select(AuditLog))).scalar_one()
        assert audit.action == "recording_upload" and meeting.recording_sha256 in audit.detail


def test_assign_speakers_by_greatest_overlap_and_rename():
    from bcp_project.diarization import apply_speaker_map, assign_speakers, clean_speaker_map, speaker_labels

    segments = [
        {"speaker": "Transcript", "text": "Opening", "t0": 0.0, "t1": 4.0},
        {"speaker": "Transcript", "text": "Report", "t0": 4.0, "t1": 10.0},
        {"speaker": "System", "text": "note"},
        {"speaker": "Transcript", "text": "Mostly B", "t0": 9.0, "t1": 13.0},
    ]
    turns = [
        {"start": 0.0, "end": 4.5, "speaker": "SPEAKER_01"},
        {"start": 4.5, "end": 10.0, "speaker": "SPEAKER_00"},
        {"start": 10.0, "end": 14.0, "speaker": "SPEAKER_01"},
    ]
    out = assign_speakers(segments, turns)
    assert [s["speaker"] for s in out] == ["Speaker 1", "Speaker 2", "System", "Speaker 1"]
    assert out[1]["speaker_label"] == "SPEAKER_00" and "speaker_label" not in out[2]
    labels = speaker_labels(out)
    assert [(l["label"], l["segments"]) for l in labels] == [("SPEAKER_01", 2), ("SPEAKER_00", 1)]
    mapping = clean_speaker_map({"SPEAKER_01": " Chairman ", "SPEAKER_99": "Ghost", "SPEAKER_00": ""}, [l["label"] for l in labels])
    assert mapping == {"SPEAKER_01": "Chairman"}
    renamed = apply_speaker_map(out, mapping)
    assert [s["speaker"] for s in renamed] == ["Chairman", "Speaker 2", "System", "Chairman"]


def test_diarization_failure_keeps_transcript(monkeypatch, tmp_path):
    import bcp_project.diarization as diarization
    from bcp_project.jobs.handlers import _diarize_segments

    monkeypatch.setenv("DIARIZATION_PROVIDER", "http")
    monkeypatch.delenv("DIARIZATION_URL", raising=False)
    segments = [{"speaker": "Transcript", "text": "x", "t0": 0, "t1": 1}]
    assert _diarize_segments(1, tmp_path / "a.wav", segments, {}) == (segments, "failed")
    monkeypatch.setattr(diarization, "diarize", lambda path: [{"start": 0, "end": 1, "speaker": "S0"}])
    out, state = _diarize_segments(1, tmp_path / "a.wav", segments, {"S0": "MD"})
    assert state == "done" and out[0]["speaker"] == "MD"


def test_invitee_transcription_view_hides_draft_minutes():
    from bcp_project.routers.meeting_common import _transcription_view

    meeting = _meeting(minutes_draft="Confidential draft", transcription_status="stopped")
    hidden = _transcription_view(meeting, include_segments=False)["minutes"]
    assert "body_md" not in hidden and hidden["state"] == "draft" and hidden["doc_id"] is None
    assert _transcription_view(meeting, include_segments=True)["minutes"]["body_md"] == "Confidential draft"
