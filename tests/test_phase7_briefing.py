"""Phase 7: Board-paper briefings, decision register, approval states, map-reduce for long papers."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

HEADINGS = (
    "subject",
    "background",
    "proposal",
    "financial_implications",
    "risks",
    "regulatory_considerations",
    "management_recommendation",
    "decision_required",
)

PAGES = [
    (1, "Board Memo No. 42. Subject: Capital injection for Sonali Exchange Co. Inc. in the United States."),
    (2, "Background: the subsidiary has reported operating losses for three consecutive years since 2023."),
    (3, "Proposal: management proposes a capital injection of USD 5 million to restore the capital ratio."),
    (4, "Financial implications: the injection reduces the parent bank's CRAR by 0.04 percentage points."),
    (5, "Risks: further losses may erode the injected capital if the remittance volume keeps declining."),
    (6, "Management recommends that the Board approve the injection subject to a turnaround plan."),
    (7, "Decision required: approval of the capital injection and the turnaround plan by the Board."),
]

GROUNDED = {
    "subject": [{"text": "Capital injection for Sonali Exchange", "page": 1, "quote": "Capital injection for Sonali Exchange Co. Inc."}],
    "background": [{"text": "Three years of losses", "page": 2, "quote": "operating losses for three consecutive years"}],
    "proposal": [{"text": "Inject USD 5 million", "page": 3, "quote": "capital injection of USD 5 million"}],
    "financial_implications": [{"text": "CRAR falls 0.04 pp", "page": 4, "quote": "reduces the parent bank's CRAR by 0.04 percentage points"}],
    "risks": [{"text": "Capital may erode", "page": 5, "quote": "further losses may erode the injected capital"}],
    "regulatory_considerations": [],
    "management_recommendation": [{"text": "Approve with a turnaround plan", "page": 6, "quote": "approve the injection subject to a turnaround plan"}],
    "decision_required": [{"text": "Approve injection and plan", "page": 7, "quote": "approval of the capital injection and the turnaround plan"}],
}


def _request():
    from starlette.requests import Request

    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("10.0.0.7", 1234)})


def _user(name, role="board_secretary"):
    from bcp_project.models import Role

    return SimpleNamespace(username=name, role=Role(role))


def _doc(doc_id, *, sensitivity="internal", title=None, **kw):
    from bcp_project.models import DocumentRecord

    fields = dict(
        doc_id=doc_id,
        doc_date=date(2026, 9, 1),
        doc_type="Board Memo",
        keywords=[],
        file_location=f"/tmp/{doc_id}.pdf",
        uploaded_by="sec",
        sensitivity=sensitivity,
        title=title or doc_id,
        page_count=len(PAGES),
    )
    fields.update(kw)
    return DocumentRecord(**fields)


def _grant(username, doc_id):
    from bcp_project.models import AccessMode, AccessRequestStatus, DocumentAccessRequest

    return DocumentAccessRequest(
        doc_id=doc_id,
        requester_username=username,
        purpose="board work",
        requested_mode=AccessMode.view_only,
        status=AccessRequestStatus.approved,
        expires_at=datetime.utcnow() + timedelta(days=7),
    )


class FakeModel:
    """Stands in for ``ai.chat_json``: map calls answer from the window text, reduce merges by id."""

    def __init__(self, map_answer=None):
        self.map_calls = []
        self.reduce_calls = 0
        self.map_answer = map_answer

    def __call__(self, task, system, user, **kw):
        assert task == "board_brief"
        if "candidate" in system:
            self.reduce_calls += 1
            out = {}
            for key in HEADINGS:
                block = re.search(rf"^{key}:\n((?:  .*\n?)*)", user, re.MULTILINE)
                count = len(re.findall(r"^  \d+\.", block.group(1), re.MULTILINE)) if block else 0
                out[key] = [{"text": f"merged {key}", "ids": list(range(1, count + 1))}] if count else []
            return out
        self.map_calls.append(user)
        if self.map_answer is not None:
            return self.map_answer(user)
        pages = {int(n) for n in re.findall(r"\[Page (\d+)\]", user)}
        return {key: [p for p in points if p["page"] in pages] for key, points in GROUNDED.items()}


@pytest.fixture
def model(monkeypatch):
    import bcp_project.ai as ai

    fake = FakeModel()
    monkeypatch.setattr(ai, "chat_json", fake)
    monkeypatch.setattr(ai, "last_call", lambda: {"provider": "fake", "model": "fake-1", "source": "test"})
    return fake


# --- Schema, citations, map-reduce ------------------------------------------------------


def test_briefing_has_all_eight_headings_with_citations_or_not_found(model):
    from bcp_project.services.briefing import BoardPaperSummary, generate_briefing
    from bcp_project.summary_extractor import DocumentSummary

    summary, provenance = generate_briefing(PAGES, sensitivity="internal", title="Memo 42")
    assert isinstance(summary, BoardPaperSummary) and isinstance(summary, DocumentSummary)
    texts = dict(PAGES)
    assert [key for key, _, _ in summary.sections()] == list(HEADINGS)
    for key, label, section in summary.sections():
        if key == "regulatory_considerations":
            assert section.not_found and section.points == []
            continue
        assert not section.not_found and section.points, label
        for point in section.points:
            assert point.page in texts and point.quote.casefold() in texts[point.page].casefold()
    assert provenance["model"] == "fake-1" and provenance["citations"]["dropped_ungrounded"] == 0


def test_ungrounded_points_are_dropped_and_wrong_pages_corrected(monkeypatch):
    import bcp_project.ai as ai
    from bcp_project.services.briefing import generate_briefing

    def answer(user):
        out = {key: [] for key in HEADINGS}
        out["proposal"] = [
            {"text": "Invented figure", "page": 3, "quote": "a capital injection of USD 50 million"},
            {"text": "Real point, wrong page", "page": 1, "quote": "capital injection of USD 5 million"},
        ]
        out["risks"] = [{"text": "Elided quote", "page": 5, "quote": "further losses may … the injected capital"}]
        return out

    monkeypatch.setattr(ai, "chat_json", FakeModel(answer))
    summary, provenance = generate_briefing(PAGES, sensitivity="internal")
    assert [(p.text, p.page) for p in summary.proposal.points] == [("Real point, wrong page", 3)]
    assert [p.page for p in summary.risks.points] == [5]
    assert summary.subject.not_found
    assert provenance["citations"] == {"proposed": 3, "dropped_ungrounded": 1}


def test_long_paper_is_read_whole_with_map_reduce(model, monkeypatch):
    from bcp_project.services.briefing import generate_briefing

    monkeypatch.setenv("BRIEFING_WINDOW_CHARS", "9000")
    filler = "Narrative of branch operations and remittance flows. " * 50
    pages = [(n, f"{filler} Section {n}.") for n in range(1, 41)]
    pages[-1] = (40, f"{filler} Decision required: approval of the capital injection and the turnaround plan by the Board.")
    answer_pages = {}

    def answer(user):
        numbers = [int(n) for n in re.findall(r"\[Page (\d+)\]", user)]
        answer_pages[min(numbers)] = numbers
        out = {key: [] for key in HEADINGS}
        if 40 in numbers:
            out["decision_required"] = [{"text": "Approve", "page": 40, "quote": "approval of the capital injection and the turnaround plan"}]
        return out

    model.map_answer = answer
    summary, provenance = generate_briefing(pages, sensitivity="internal")
    seen = sorted(n for numbers in answer_pages.values() for n in numbers)
    assert seen == list(range(1, 41))
    assert provenance["map_reduce"]["windows"] == provenance["map_reduce"]["used"] > 1
    assert provenance["map_reduce"]["reducer"] == "llm" and model.reduce_calls == 1
    assert [p.page for p in summary.decision_required.points] == [40]


def test_document_summary_uses_map_reduce_instead_of_truncation(monkeypatch):
    import bcp_project.ai as ai
    import bcp_project.summary_extractor as se
    from bcp_project.services.ingest import summarize_pages_with_provenance

    seen = []

    def fake_extract(text, *, sensitivity=None):
        seen.append(text)
        pages = re.findall(r"\[Page (\d+)\]", text)
        return se.DocumentSummary(core_info={"title": "Memo"}, searchable_keywords=[f"kw{p}" for p in pages[-1:]])

    monkeypatch.setattr(se, "extract_document_summary", fake_extract)
    monkeypatch.setattr(ai, "last_call", lambda: {"provider": "fake", "model": "m"})
    pages = [(n, ("x" * 4000) + f" end-of-page-{n}") for n in range(1, 31)]
    summary, provenance = summarize_pages_with_provenance(pages, "internal")
    joined = "".join(seen)
    assert len(seen) > 1 and all(f"end-of-page-{n}" in joined for n in range(1, 31))
    assert "Truncated" not in joined
    assert "kw30" in summary.searchable_keywords
    assert provenance["map_reduce"]["windows"] == len(seen)


# --- Editing and states -------------------------------------------------------------------


def _ready_briefing(**kw):
    from bcp_project.models import BoardBriefing
    from bcp_project.services.briefing import BoardPaperSummary, BriefingPoint, BriefingSection

    summary = BoardPaperSummary(
        proposal=BriefingSection(
            points=[BriefingPoint(text="Inject USD 5 million", page=3, quote="capital injection of USD 5 million")],
            not_found=False,
        )
    )
    fields = dict(doc_id="MEMO-42", version=1, state="ai_draft", status="ready", content=summary.model_dump(), requested_by="sec")
    fields.update(kw)
    return BoardBriefing(**fields)


def test_section_editing_requires_page_citations_and_keeps_evidence():
    from bcp_project.services.briefing import BriefingWorkflowError, load_summary, save_section

    b = _ready_briefing()
    with pytest.raises(BriefingWorkflowError) as missing:
        save_section(b, "proposal", "- Inject USD 5 million", "sec", page_count=7)
    assert missing.value.status_code == 400
    with pytest.raises(BriefingWorkflowError):
        save_section(b, "proposal", "- Inject [p. 9]", "sec", page_count=7)

    assert save_section(b, "proposal", "- Inject USD 5 million [p. 3]\n- Phased over two years [পৃ. ৪]", "sec", page_count=7)
    points = load_summary(b.content).proposal.points
    assert points[0].quote == "capital injection of USD 5 million" and points[0].source == "ai"
    assert (points[1].page, points[1].source, points[1].quote) == (4, "edited", None)
    assert save_section(b, "risks", "[not found]", "sec", page_count=7) is False
    assert b.source == "edited" and b.edited_by == "sec"


def test_states_four_eyes_and_locking(monkeypatch):
    from bcp_project.services.briefing import (
        STATE_LABELS,
        BriefingWorkflowError,
        approve,
        content_sha256,
        mark_reviewed,
        reopen,
        return_to_draft,
        save_section,
    )

    monkeypatch.setenv("BRIEFING_FOUR_EYES", "1")
    b = _ready_briefing()
    save_section(b, "subject", "- Capital injection [p. 1]", "editor", page_count=7)
    mark_reviewed(b, "gm")
    assert b.state == "management_reviewed"
    with pytest.raises(BriefingWorkflowError):
        save_section(b, "subject", "- Changed [p. 1]", "editor", page_count=7)
    for same in ("gm", "editor"):
        with pytest.raises(BriefingWorkflowError) as denied:
            approve(b, same)
        assert denied.value.status_code == 403
    return_to_draft(b, "sec", "Add the regulatory angle")
    assert b.state == "ai_draft" and "regulatory" in b.review_note
    mark_reviewed(b, "gm")
    digest = approve(b, "secretary")
    assert b.state == "approved" and digest == b.sha256 == content_sha256(b.content)
    with pytest.raises(BriefingWorkflowError):
        reopen(b, "admin", "")
    reopen(b, "admin", "Figures restated")
    assert b.state == "ai_draft" and b.sha256 is None and b.approved_by is None
    assert len(set(STATE_LABELS.values())) == 3


# --- Database: job, visibility, register, references --------------------------------------


@pytest_asyncio.fixture
async def maker(tmp_path):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from bcp_project.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'p7.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed_paper(db, doc_id="MEMO-42", **kw):
    from bcp_project.models import DocumentPage, DocumentVersion

    db.add(_doc(doc_id, title="Capital injection for Sonali Exchange", **kw))
    db.add(DocumentVersion(doc_id=doc_id, version=1, file_location="x", status="approved", created_by="sec"))
    for number, text in PAGES:
        db.add(DocumentPage(doc_id=doc_id, version=1, page_number=number, text=text))


@pytest.mark.asyncio
async def test_generate_review_approve_routes_and_member_visibility(maker, model, monkeypatch):
    from fastapi import BackgroundTasks
    from fastapi.exceptions import HTTPException

    from bcp_project.models import AuditLog, BoardBriefing
    from bcp_project.routers import briefings as routes
    from bcp_project.services.briefing import run_board_briefing

    monkeypatch.setenv("BRIEFING_FOUR_EYES", "1")
    monkeypatch.setattr(routes, "job_queue_enabled", lambda: False)
    published = []
    monkeypatch.setattr("bcp_project.ports.events.publish_event", lambda n, p: published.append(n) or True)
    monkeypatch.setattr(routes, "historical_references", _no_refs)

    async with maker() as db:
        await _seed_paper(db)
        db.add(_grant("member", "MEMO-42"))
        await db.commit()

    sec, gm, member = _user("sec"), _user("gm"), _user("member", "board_member")
    tasks = BackgroundTasks()
    async with maker() as db:
        resp = await routes.generate_briefing_route(_request(), "MEMO-42", tasks, current_user=sec, db=db)
    assert "briefing_generating" in resp.headers["location"] and len(tasks.tasks) == 1
    async with maker() as db:
        bid = (await db.execute(select(BoardBriefing.id))).scalar_one()
    assert await run_board_briefing(bid, session_factory=maker)

    async with maker() as db:
        data = json.loads((await routes.briefing_api(_request(), "MEMO-42", current_user=member, db=db)).body)
    assert data["briefing"] is None, "members must not see an AI draft"
    async with maker() as db:
        data = json.loads((await routes.briefing_api(_request(), "MEMO-42", current_user=sec, db=db)).body)
    draft = data["briefing"]
    assert draft["state"] == "ai_draft" and draft["state_label"].startswith("AI draft")
    assert [s["key"] for s in draft["sections"]] == list(HEADINGS)

    async with maker() as db:
        await routes.review_briefing(_request(), "MEMO-42", current_user=gm, db=db)
    async with maker() as db:
        denied = await routes.approve_briefing(_request(), "MEMO-42", current_user=gm, db=db)
    assert "briefing_four_eyes" in denied.headers["location"]
    async with maker() as db:
        ok = await routes.approve_briefing(_request(), "MEMO-42", current_user=sec, db=db)
    assert "briefing_approved" in ok.headers["location"] and published == ["document.briefing_approved"]

    async with maker() as db:
        data = json.loads((await routes.briefing_api(_request(), "MEMO-42", refs=True, current_user=member, db=db)).body)
    assert data["is_organizer"] is False and data["approval_blocker"] is None and data["can_amend"] is False
    approved = data["briefing"]
    assert approved["state"] == "approved" and approved["state_label"] != draft["state_label"]
    cite = next(s for s in approved["sections"] if s["key"] == "proposal")["points"][0]
    assert cite["url"] == "/view/MEMO-42?page=3"

    async with maker() as db:
        with pytest.raises(HTTPException) as no_access:
            await routes.briefing_api(_request(), "MEMO-42", current_user=_user("outsider", "board_member"), db=db)
        assert no_access.value.status_code == 403
        actions = [a for (a,) in (await db.execute(select(AuditLog.action))).all()]
    assert {"briefing_generate", "briefing_review", "briefing_approve_denied", "briefing_approve"} <= set(actions)


async def _no_refs(*a, **k):
    return []


@pytest.mark.asyncio
async def test_invited_member_reads_briefing_of_meeting_paper(maker, model, monkeypatch):
    from bcp_project.models import BoardBriefing, BoardMeeting, MeetingDocument, MeetingInvitation
    from bcp_project.routers import briefings as routes

    monkeypatch.setattr(routes, "historical_references", _no_refs)
    async with maker() as db:
        await _seed_paper(db)
        db.add(BoardMeeting(id=5, title="Board 5", scheduled_at=datetime(2026, 10, 20), created_by="sec"))
        db.add(MeetingDocument(id=9, meeting_id=5, title="Memo 42", file_location="x", uploaded_by="sec", archive_doc_id="MEMO-42"))
        db.add(MeetingInvitation(meeting_id=5, username="invitee"))
        b = _ready_briefing(state="approved", approved_by="sec", sha256="0" * 64)
        db.add(b)
        await db.commit()
    async with maker() as db:
        data = json.loads((await routes.briefing_api(_request(), "MEMO-42", refs=True, current_user=_user("invitee", "board_member"), db=db)).body)
    point = next(s for s in data["briefing"]["sections"] if s["key"] == "proposal")["points"][0]
    assert point["url"] == "/board/meetings/5/documents/9/view"


@pytest.mark.asyncio
async def test_egress_block_fails_cleanly(maker, monkeypatch):
    import bcp_project.ai as ai
    from bcp_project.models import BoardBriefing
    from bcp_project.services.briefing import run_board_briefing

    def blocked(*a, **k):
        raise ai.EgressBlocked("board_brief", "openrouter", "restricted")

    monkeypatch.setattr(ai, "chat_json", blocked)
    async with maker() as db:
        await _seed_paper(db, sensitivity="restricted")
        db.add(BoardBriefing(id=1, doc_id="MEMO-42", version=1, requested_by="sec"))
        await db.commit()
    assert await run_board_briefing(1, session_factory=maker) is False
    async with maker() as db:
        b = await db.get(BoardBriefing, 1)
    assert b.status == "failed" and "local model" in b.error and b.content is None


@pytest.mark.asyncio
async def test_restricted_paper_gives_no_history_to_unauthorized_reader(maker, monkeypatch):
    import bcp_project.services.document_query as dq
    from bcp_project.models import BoardMeeting, MeetingDecision
    from bcp_project.services.decision_register import decision_register, historical_references, related_decisions

    async def fake_search(db, *, acl, q, lang, filters, limit):
        rows = [
            {"doc_id": d, "doc_type": "Board Memo", "title": f"Title {d}", "snippet": "capital injection", "score": 0.9, "source": "chunk", "page": 2}
            for d in ("MEMO-42", "OLD-RESTRICTED", "OLD-OPEN", "OLD-UNGRANTED")
        ]
        return SimpleNamespace(results=rows, mode="hybrid", cached=False, reranked=False)

    monkeypatch.setattr(dq, "hybrid_search", fake_search)
    async with maker() as db:
        await _seed_paper(db)
        db.add(_doc("OLD-RESTRICTED", sensitivity="restricted", title="Earlier restricted injection"))
        db.add(_doc("OLD-OPEN", title="Earlier injection"))
        db.add(_doc("OLD-UNGRANTED", title="Ungranted injection"))
        for doc_id in ("MEMO-42", "OLD-OPEN"):
            db.add(_grant("member", doc_id))
        for mid, min_doc, sensitivity in ((1, "MIN-1", "restricted"), (2, "MIN-2", "internal"), (3, "MIN-3", "internal")):
            db.add(_doc(min_doc, sensitivity=sensitivity, doc_type="Board Minutes"))
            db.add(
                BoardMeeting(
                    id=mid, title=f"Board {mid}", scheduled_at=datetime(2025, mid, 1), created_by="sec",
                    minutes_state="approved" if mid < 3 else "in_review", minutes_doc_id=min_doc,
                )
            )
            db.add(MeetingDecision(meeting_id=mid, seq=1, text=f"Approved capital injection for Sonali Exchange (meeting {mid})", created_by="sec"))
        db.add(_grant("member", "MIN-2"))
        await db.commit()

    member, sec = _user("member", "board_member"), _user("sec")
    async with maker() as db:
        paper = (await db.execute(select(dq.DocumentRecord).where(dq.DocumentRecord.doc_id == "MEMO-42"))).scalar_one()
        refs = await historical_references(db, member, paper, "capital injection Sonali Exchange")
        assert [r["doc_id"] for r in refs] == ["OLD-OPEN"]
        assert [r["doc_id"] for r in await historical_references(db, sec, paper, "capital injection")] == [
            "OLD-RESTRICTED", "OLD-OPEN", "OLD-UNGRANTED"
        ]

        register = await decision_register(db, member)
        assert [e["meeting_id"] for e in register] == [2]
        assert [e["meeting_id"] for e in await decision_register(db, sec)] == [2, 1]
        related = await related_decisions(db, member, "capital injection for Sonali Exchange in the US")
        assert [e["minutes_doc_id"] for e in related] == ["MIN-2"]
        assert await decision_register(db, member, q="no such topic") == []
