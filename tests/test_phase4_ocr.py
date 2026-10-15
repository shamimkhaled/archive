"""Phase 4: intake validation, write-once storage, OCR confidence, verification gate, versions."""
from __future__ import annotations

import io
import os
import shutil
import stat
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from bcp_project import intake
from bcp_project.intake import IntakeRejected, WriteOnceViolation

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


def text_image(lines, *, size=(1400, 500), fmt="PNG") -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.load_default(size=44)
    except TypeError:
        font = ImageFont.load_default()
    img = Image.new("L", size, 255)
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((60, 60 + i * 80), line, fill=0, font=font)
    buffer = io.BytesIO()
    img.save(buffer, format=fmt, dpi=(300, 300))
    return buffer.getvalue()


def noise_image() -> bytes:
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(7)
    arr = (rng.random((500, 1400)) > 0.5).astype("uint8") * 255
    buffer = io.BytesIO()
    Image.fromarray(arr, mode="L").save(buffer, format="PNG", dpi=(300, 300))
    return buffer.getvalue()


def text_pdf(text: str) -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buffer.getvalue()


@pytest.fixture
def local_storage(tmp_path, monkeypatch):
    import bcp_project.deps as deps

    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("AV_SCAN", raising=False)
    monkeypatch.setattr(deps, "UPLOAD_DIR", tmp_path)
    return tmp_path


# --- Intake validation -------------------------------------------------------


def test_sniff_content_type_by_magic_bytes():
    assert intake.sniff_content_type(b"%PDF-1.7\n...") == intake.PDF
    assert intake.sniff_content_type(PNG_MAGIC + b"rest") == intake.PNG
    assert intake.sniff_content_type(b"\xff\xd8\xff\xe0rest") == intake.JPEG
    assert intake.sniff_content_type(b"II*\x00rest") == intake.TIFF
    assert intake.sniff_content_type(b"MM\x00*rest") == intake.TIFF
    assert intake.sniff_content_type(b"PK\x03\x04 not a pdf") is None


def test_validate_upload_rejects_disguised_and_oversized(monkeypatch):
    monkeypatch.delenv("AV_SCAN", raising=False)
    with pytest.raises(IntakeRejected):
        intake.validate_upload(b"MZ\x90\x00 executable renamed to .pdf")
    with pytest.raises(IntakeRejected):
        intake.validate_upload(b"")
    monkeypatch.setattr(intake, "max_upload_bytes", lambda: 10)
    with pytest.raises(IntakeRejected):
        intake.validate_upload(b"%PDF-1.7 " + b"x" * 50)


def test_validate_upload_accepts_png_and_hashes():
    data = text_image(["Sonali Bank"])
    upload = intake.validate_upload(data)
    assert upload.content_type == intake.PNG
    assert upload.is_image
    assert upload.extension == ".png"
    assert upload.sha256 == intake.sha256_hex(data) and len(upload.sha256) == 64


def test_av_scan_fails_closed_when_scanner_unreachable(monkeypatch):
    monkeypatch.setenv("AV_SCAN", "clamav")
    monkeypatch.setenv("CLAMAV_HOST", "127.0.0.1")
    monkeypatch.setenv("CLAMAV_PORT", "9")
    monkeypatch.setenv("CLAMAV_TIMEOUT", "1")
    monkeypatch.delenv("AV_SCAN_FAIL_OPEN", raising=False)
    with pytest.raises(IntakeRejected):
        intake.validate_upload(b"%PDF-1.7\n%%EOF")


def test_image_to_pdf_rendition():
    pdf = intake.image_to_pdf(text_image(["page"]))
    assert pdf.startswith(b"%PDF")


# --- Write-once storage ------------------------------------------------------


def test_store_write_once_local(local_storage):
    data = b"%PDF-1.7 original"
    digest = intake.sha256_hex(data)
    key = intake.object_key("originals", "SB/2026 001", 1, digest, ".pdf")
    assert "/" not in key.split("/")[1]
    location = intake.store_write_once(data, key, content_type=intake.PDF, sha256=digest)
    path = local_storage / location
    assert path.read_bytes() == data
    assert not (path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    # Same bytes again is an idempotent retry; different bytes is refused.
    assert intake.store_write_once(data, key, content_type=intake.PDF, sha256=digest) == location
    other = b"%PDF-1.7 tampered"
    with pytest.raises(WriteOnceViolation):
        intake.store_write_once(other, key, content_type=intake.PDF, sha256=intake.sha256_hex(other))
    assert path.read_bytes() == data


def test_store_write_once_s3_uses_conditional_put(monkeypatch):
    from botocore.exceptions import ClientError

    puts = []

    class FakeS3:
        def put_object(self, **params):
            puts.append(params)
            if len(puts) > 1:
                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")

        def head_object(self, **params):
            return {"Metadata": {"sha256": "different"}}

    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AWS_BUCKET_NAME", "archive")
    monkeypatch.setenv("AWS_PREFIX", "pdfs")
    monkeypatch.setenv("S3_OBJECT_LOCK_DAYS", "30")
    monkeypatch.setattr("bcp_project.aws_utils.get_s3_client", lambda: FakeS3())
    location = intake.store_write_once(b"a", "originals/D/v1-x.pdf", content_type=intake.PDF, sha256="abc")
    assert location == "s3://archive/pdfs/originals/D/v1-x.pdf"
    assert puts[0]["IfNoneMatch"] == "*"
    assert puts[0]["Metadata"] == {"sha256": "abc"}
    assert puts[0]["ObjectLockMode"] == "GOVERNANCE"
    with pytest.raises(WriteOnceViolation):
        intake.store_write_once(b"b", "originals/D/v1-x.pdf", content_type=intake.PDF, sha256="abc")


# --- OCR confidence ----------------------------------------------------------


@needs_tesseract
def test_ocr_confidence_separates_clean_text_from_noise(monkeypatch):
    from bcp_project import ocr

    monkeypatch.setenv("OCR_LANGUAGES", "eng")
    clean = ocr.extract_image(text_image(["SONALI BANK BOARD MEETING", "Agenda item four: annual budget"]))
    assert len(clean) == 1 and clean[0].source == "ocr"
    assert "SONALI" in clean[0].text.upper()
    assert clean[0].confidence is not None and clean[0].confidence >= 75
    assert ocr.summarize_confidence(clean)["needs_review"] is False

    noisy = ocr.extract_image(noise_image())
    summary = ocr.summarize_confidence(noisy)
    assert summary["needs_review"] is True


def test_text_layer_pdf_needs_no_review():
    pytest.importorskip("reportlab")
    from bcp_project import ocr

    pages = ocr.extract_pdf(text_pdf("Board resolution approving the annual budget for 2026"))
    assert pages[0].source == "text"
    assert ocr.summarize_confidence(pages) == {"mean": None, "min": None, "ocr_pages": 0, "needs_review": False}


def test_review_thresholds_from_env(monkeypatch):
    from bcp_project import ocr

    monkeypatch.setenv("OCR_REVIEW_THRESHOLD", "90")
    monkeypatch.setenv("OCR_PAGE_REVIEW_THRESHOLD", "50")
    pages = [ocr.PageText(1, "x" * 100, 85.0, "ocr"), ocr.PageText(2, "y" * 100, 92.0, "ocr")]
    assert ocr.summarize_confidence(pages)["mean"] == 88.5
    assert ocr.summarize_confidence(pages)["needs_review"] is True
    monkeypatch.setenv("OCR_REVIEW_THRESHOLD", "80")
    assert ocr.summarize_confidence(pages)["needs_review"] is False
    monkeypatch.setenv("OCR_PAGE_REVIEW_THRESHOLD", "86")
    assert ocr.summarize_confidence(pages)["needs_review"] is True


def test_preprocess_deskews_and_binarizes():
    from PIL import Image

    from bcp_project import ocr

    img = Image.open(io.BytesIO(text_image(["Deskew me please"]))).rotate(3, expand=True, fillcolor=255)
    out = ocr.preprocess(img)
    assert set(out.getdata()) <= {0, 255}
    assert out.width >= img.width


def test_metadata_rules_fallback_extracts_reference(monkeypatch):
    from bcp_project.services import metadata_suggest

    monkeypatch.setenv("AI_EGRESS", "internal_only")
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    got = metadata_suggest.suggest_metadata("Ref No: SB/HO/2026/114\nSubject: budget", sensitivity="internal")
    assert got["_source"]["kind"] == "rules"
    assert got["reference_number"] == "SB/HO/2026/114"
    bangla = metadata_suggest.regex_reference("স্মারক নং: সোব/প্রকা/২০২৬/১২")
    assert bangla.startswith("সোব")


# --- Ingest state machine, review gate, versions -----------------------------


@pytest_asyncio.fixture
async def maker(tmp_path):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from bcp_project.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'p4.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def offline_pipeline(monkeypatch, local_storage):
    """No network: AI blocked by egress policy, Qdrant/cache/events stubbed."""
    import bcp_project.cache as cache
    import bcp_project.ports.events as events
    import bcp_project.qdrant_store as qs

    monkeypatch.setenv("AI_EGRESS", "internal_only")
    monkeypatch.setenv("OCR_LANGUAGES", "eng")
    for name in ("LOCAL_LLM_BASE_URL", "LOCAL_EMBED_BASE_URL", "JOB_STREAM_ENABLED", "JOB_QUEUE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    qdrant = SimpleNamespace(create_collections=lambda: None, deleted=[])
    qdrant.delete_document_chunks = lambda doc_id: qdrant.deleted.append(doc_id)
    monkeypatch.setattr(qs, "make_qdrant_indexer", lambda: qdrant)
    monkeypatch.setattr(cache, "bump_search_cache_version", AsyncMock())
    published = []
    monkeypatch.setattr(events, "publish_event", lambda name, payload: published.append((name, payload)))
    monkeypatch.setattr("bcp_project.jobs.queue.job_queue_enabled", lambda: False)
    monkeypatch.setattr("bcp_project.services.document_versions.job_queue_enabled", lambda: False)
    return SimpleNamespace(qdrant=qdrant, published=published)


async def _intake(maker, data, doc_id="SB-2026-900", **kw):
    from bcp_project.services.document_versions import intake_new_document

    async with maker() as db:
        result = await intake_new_document(
            db,
            upload=intake.validate_upload(data),
            doc_id=doc_id,
            doc_date=date(2026, 1, 5),
            doc_type="Circular",
            keywords=[],
            sensitivity=kw.get("sensitivity", "internal"),
            uploaded_by=kw.get("uploaded_by", "alice"),
            metadata={"title": "Budget circular", "department": ""},
        )
        await db.commit()
        return result


@needs_tesseract
@pytest.mark.asyncio
async def test_low_confidence_scan_waits_for_review_and_stays_hidden(maker, offline_pipeline):
    from bcp_project.access_control import assert_can_view, hidden_restricted_doc_ids
    from bcp_project.models import DocumentRecord, DocumentVersion, Role
    from bcp_project.services.ingest import run_document_ingest

    result = await _intake(maker, noise_image())
    assert result.version.original_location and result.version.file_location != result.version.original_location
    assert result.job_id is None

    outcome = await run_document_ingest("SB-2026-900", version=1, session_factory=maker)
    assert outcome == "needs_review"
    assert any(name == "document.needs_review" for name, _ in offline_pipeline.published)

    async with maker() as db:
        doc = (await db.execute(select(DocumentRecord))).scalar_one()
        ver = (await db.execute(select(DocumentVersion))).scalar_one()
        assert doc.ingest_status == "needs_review"
        assert doc.verification_status == "pending"
        assert doc.title == "Budget circular" and doc.department is None
        assert ver.status == "pending_review"

        # Invisible to search for everyone, and a board member cannot open it by URL.
        admin = SimpleNamespace(username="root", role=Role.admin)
        member = SimpleNamespace(username="bm", role=Role.board_member)
        assert await hidden_restricted_doc_ids(db, admin, ["SB-2026-900"]) == {"SB-2026-900"}
        assert await hidden_restricted_doc_ids(db, member, ["SB-2026-900"]) == {"SB-2026-900"}
        with pytest.raises(Exception) as excinfo:
            await assert_can_view(member, "SB-2026-900", db)
        assert getattr(excinfo.value, "status_code", None) == 404

    # Re-running the job does not re-parse a version waiting on a person.
    assert await run_document_ingest("SB-2026-900", version=1, session_factory=maker) == "needs_review"

    # Reviewer corrects the text and approves: the version indexes from the corrected text.
    from bcp_project.services.ingest import load_pages

    async with maker() as db:
        pages = await load_pages(db, "SB-2026-900", 1)
        pages[0].text = "Corrected budget circular text"
        ver = (await db.execute(select(DocumentVersion))).scalar_one()
        ver.status = "reviewed"
        await db.commit()
    assert await run_document_ingest("SB-2026-900", version=1, session_factory=maker) == "indexed"
    async with maker() as db:
        doc = (await db.execute(select(DocumentRecord))).scalar_one()
        assert doc.verification_status == "approved"
        assert doc.ingest_status == "indexed"
        assert doc.summary_json["_ai"]["summary"] == "skipped_egress_policy"
        assert await hidden_restricted_doc_ids(db, SimpleNamespace(username="root", role=Role.admin), ["SB-2026-900"]) == set()
    assert offline_pipeline.qdrant.deleted == ["SB-2026-900"]


@needs_tesseract
@pytest.mark.asyncio
async def test_clean_scan_indexes_directly_and_new_version_supersedes(maker, offline_pipeline):
    from bcp_project.models import DocumentRecord, DocumentVersion
    from bcp_project.services.document_versions import intake_new_version
    from bcp_project.services.ingest import run_document_ingest

    first = text_image(["SONALI BANK CIRCULAR", "Ref No: SB/HO/2026/114"])
    await _intake(maker, first, doc_id="SB-2026-901")
    assert await run_document_ingest("SB-2026-901", session_factory=maker) == "indexed"

    async with maker() as db:
        doc = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == "SB-2026-901"))).scalar_one()
        assert doc.verification_status == "not_required"
        assert doc.suggested_metadata["reference_number"].startswith("SB/HO/2026/114")
        with pytest.raises(ValueError):
            await intake_new_version(db, document=doc, upload=intake.validate_upload(first), uploaded_by="alice")
        second = text_image(["SONALI BANK CIRCULAR AMENDED", "Ref No: SB/HO/2026/115"])
        result = await intake_new_version(db, document=doc, upload=intake.validate_upload(second), uploaded_by="bob")
        await db.commit()
        assert result.version.version == 2

    assert await run_document_ingest("SB-2026-901", version=2, session_factory=maker) == "indexed"
    async with maker() as db:
        doc = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == "SB-2026-901"))).scalar_one()
        versions = {
            v.version: v.status
            for v in (await db.execute(select(DocumentVersion).where(DocumentVersion.doc_id == "SB-2026-901"))).scalars()
        }
        assert versions == {1: "superseded", 2: "approved"}
        assert doc.current_version == 2
        assert doc.sha256 == intake.sha256_hex(second)


@pytest.mark.asyncio
async def test_failed_ingest_records_error(maker, offline_pipeline, monkeypatch):
    from bcp_project.models import DocumentRecord
    from bcp_project.services.ingest import run_document_ingest

    monkeypatch.setattr("bcp_project.ocr.extract_pages", lambda data, ct: (_ for _ in ()).throw(RuntimeError("boom")))
    await _intake(maker, b"%PDF-1.4\n%%EOF", doc_id="SB-2026-902")
    with pytest.raises(RuntimeError):
        await run_document_ingest("SB-2026-902", session_factory=maker)
    async with maker() as db:
        doc = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == "SB-2026-902"))).scalar_one()
        assert doc.ingest_status == "failed"
        assert "boom" in doc.ingest_error


def test_four_eyes_defaults(monkeypatch):
    from bcp_project.routers import review

    ver = SimpleNamespace(created_by="alice")
    alice = SimpleNamespace(username="alice")
    monkeypatch.delenv("OCR_REVIEW_FOUR_EYES", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    assert review.four_eyes_required() and review._four_eyes_problem(alice, ver)
    monkeypatch.setenv("APP_ENV", "development")
    assert not review.four_eyes_required()
    monkeypatch.setenv("OCR_REVIEW_FOUR_EYES", "1")
    assert review._four_eyes_problem(alice, ver)
    assert review._four_eyes_problem(SimpleNamespace(username="bob"), ver) is None


@pytest.mark.asyncio
async def test_verify_endpoint_detects_tampering(maker, local_storage, monkeypatch):
    from bcp_project.models import DocumentVersion, Role
    from bcp_project.routers import ingest as ingest_router

    data = b"%PDF-1.7 the original"
    digest = intake.sha256_hex(data)
    key = intake.object_key("originals", "SB-2026-903", 1, digest, ".pdf")
    intake.store_write_once(data, key, content_type=intake.PDF, sha256=digest)
    monkeypatch.setattr(ingest_router, "write_audit", AsyncMock())
    async with maker() as db:
        db.add(DocumentVersion(doc_id="SB-2026-903", version=1, sha256=digest, file_location=key, created_by="a", status="approved"))
        await db.commit()
        user = SimpleNamespace(username="root", role=Role.admin)
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        ok = await ingest_router.verify_document_version(request, "SB-2026-903", 1, current_user=user, db=db)
        assert ok["match"] is True

        path = local_storage / key
        os.chmod(path, 0o644)
        path.write_bytes(b"%PDF-1.7 altered on disk")
        bad = await ingest_router.verify_document_version(request, "SB-2026-903", 1, current_user=user, db=db)
        assert bad["match"] is False and bad["recorded_sha256"] == digest


# --- Metadata classification and review routing -----------------------------------------


def _ai_suggestion(**fields):
    base = {"title": "", "reference_number": "", "department": "", "subject": "", "category": "", "doc_date": "", "doc_type": "", "keywords": []}
    return {**base, **fields, "_source": {"kind": "ai", "provider": "stub", "model": "m"}}


def test_metadata_review_reasons():
    from bcp_project.services.metadata_suggest import metadata_review_reasons

    doc = SimpleNamespace(title="Budget circular", doc_type="Circular", doc_date=date(2026, 1, 5))
    assert metadata_review_reasons(doc, _ai_suggestion(doc_type="circular", doc_date="2026-01-05")) == []
    assert metadata_review_reasons(doc, _ai_suggestion(doc_type="Office Order")) == ["doc_type_mismatch"]
    assert metadata_review_reasons(doc, _ai_suggestion(doc_date="2025-12-01")) == ["doc_date_mismatch"]
    rules = {"_source": {"kind": "rules", "reason": "APIStatusError"}}
    assert metadata_review_reasons(doc, rules) == ["ai_unavailable"]
    assert metadata_review_reasons(doc, {"_source": {"kind": "rules", "reason": "EgressBlocked"}}) == []
    untitled = SimpleNamespace(title=None, doc_type="Other", doc_date=date(2026, 1, 5))
    assert metadata_review_reasons(untitled, _ai_suggestion(doc_type="Memo")) == ["missing_title"]


def test_metadata_review_can_be_disabled(monkeypatch):
    from bcp_project.services.metadata_suggest import metadata_review_reasons

    monkeypatch.setenv("METADATA_REVIEW", "off")
    doc = SimpleNamespace(title=None, doc_type="Circular", doc_date=date(2026, 1, 5))
    assert metadata_review_reasons(doc, {"_source": {"kind": "rules", "reason": "X"}}) == []


def test_apply_suggestions_fills_only_empty_fields_and_classifies_other():
    from bcp_project.services.metadata_suggest import apply_suggestions

    doc = SimpleNamespace(
        title="Uploader title", reference_number=None, department=None, subject=None, category=None, doc_type="Other", keywords=[]
    )
    applied = apply_suggestions(
        doc, _ai_suggestion(title="AI title", department="Treasury", doc_type="Office Order", keywords=["budget", "বাজেট"])
    )
    assert doc.title == "Uploader title"
    assert doc.department == "Treasury" and doc.doc_type == "Office Order" and doc.keywords == ["budget", "বাজেট"]
    assert set(applied) == {"department", "doc_type", "keywords"}


@pytest.mark.asyncio
async def test_clean_text_pdf_auto_applies_metadata(maker, offline_pipeline, monkeypatch):
    from bcp_project.models import DocumentRecord
    from bcp_project.services.ingest import run_document_ingest

    monkeypatch.setenv("METADATA_FIELD_MIN_CONFIDENCE", "0.7")
    monkeypatch.setattr(
        "bcp_project.services.metadata_suggest.suggest_metadata",
        lambda text, sensitivity=None: _ai_suggestion(department="Finance", subject="Budget", doc_type="Circular", keywords=["budget"]),
    )
    await _intake(maker, text_pdf("Budget circular for 2026"), doc_id="SB-2026-910")
    assert await run_document_ingest("SB-2026-910", session_factory=maker) == "indexed"
    async with maker() as db:
        doc = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == "SB-2026-910"))).scalar_one()
        assert doc.department == "Finance" and doc.subject == "Budget"
        assert doc.metadata_confirmed_by is None
        assert "department" in doc.suggested_metadata["_applied"]
        assert doc.metadata_check_pending is True


@pytest.mark.asyncio
async def test_type_mismatch_sends_clean_pdf_to_review(maker, offline_pipeline, monkeypatch):
    from bcp_project.models import DocumentRecord
    from bcp_project.services.ingest import run_document_ingest

    monkeypatch.setattr(
        "bcp_project.services.metadata_suggest.suggest_metadata",
        lambda text, sensitivity=None: _ai_suggestion(doc_type="Board Paper"),
    )
    await _intake(maker, text_pdf("Memorandum to the Board"), doc_id="SB-2026-911")
    assert await run_document_ingest("SB-2026-911", session_factory=maker) == "needs_review"
    async with maker() as db:
        doc = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == "SB-2026-911"))).scalar_one()
        assert doc.suggested_metadata["_review_reasons"] == ["doc_type_mismatch"]
        assert doc.department is None
        assert doc.metadata_check_pending is False


def test_detect_language():
    from bcp_project.services.metadata_suggest import detect_language

    assert detect_language("Budget circular for the financial year 2026") == "en"
    assert detect_language("২০২৬ অর্থবছরের বাজেট বিষয়ক পরিপত্র জারি করা হলো") == "bn"
    assert detect_language("Budget circular বাজেট বিষয়ক পরিপত্র for the financial year") == "mixed"
    assert detect_language("Ref 12") is None


def test_score_fields_per_field_confidence(monkeypatch):
    from bcp_project.services.metadata_suggest import low_confidence_fields, score_fields

    monkeypatch.setenv("METADATA_FIELD_MIN_CONFIDENCE", "0.7")

    text = "Ref No: SB/HO/2026/114\nBudget circular for the financial year 2026\nIssued by the Finance Division"
    suggested = {
        **_ai_suggestion(
            reference_number="SB/HO/2026/114",
            subject="Budget circular for the financial year 2026",
            department="Treasury",
            doc_date="2026-01-05",
            doc_type="Circular",
        ),
        "_model_confidence": {"department": 0.5},
    }
    doc = SimpleNamespace(
        title="Budget circular", reference_number=None, department=None, subject=None, category=None,
        doc_type="Other", doc_date=date(2026, 1, 5), language=None,
    )
    scores = score_fields(text, suggested, doc)
    assert scores == {
        "title": 1.0,
        "reference_number": 0.95,
        "department": 0.5,
        "subject": 0.9,
        "doc_date": 1.0,
        "doc_type": 0.75,
        "language": 0.95,
    }
    assert low_confidence_fields(scores) == ["department"]
    assert low_confidence_fields(scores, threshold=0.8) == ["department", "doc_type"]

    blurry = score_fields(text, suggested, doc, ocr_mean=40)
    assert blurry["subject"] == 0.74 and blurry["title"] == 1.0
    invented = score_fields(text, {**suggested, "reference_number": "SB/HO/2026/999"}, doc)
    assert invented["reference_number"] == 0.4
    rules = score_fields(text, {"subject": "Budget", "_source": {"kind": "rules"}}, doc)
    assert rules["subject"] == 0.65


@pytest.mark.asyncio
async def test_low_field_confidence_sends_clean_pdf_to_review_and_reviewer_confirms(maker, offline_pipeline, monkeypatch):
    import httpx
    from fastapi import FastAPI

    from bcp_project.deps import get_current_user, get_db
    from bcp_project.models import AuditLog, DocumentRecord, Role, User
    from bcp_project.routers import review
    from bcp_project.services.ingest import run_document_ingest
    from bcp_project.services.taxonomy import document_tags

    suggestion = {
        **_ai_suggestion(department="Treasury", subject="Budget", keywords=["Budget"]),
        "_model_confidence": {"department": 0.3},
    }
    monkeypatch.setattr("bcp_project.services.metadata_suggest.suggest_metadata", lambda text, sensitivity=None: suggestion)
    await _intake(maker, text_pdf("Budget circular for the financial year 2026 issued by the Head Office"), doc_id="SB-2026-912")
    assert await run_document_ingest("SB-2026-912", session_factory=maker) == "needs_review"
    async with maker() as db:
        doc = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == "SB-2026-912"))).scalar_one()
        assert doc.suggested_metadata["_review_reasons"] == ["low_metadata_confidence"]
        assert doc.suggested_metadata["_low_fields"] == ["department"]
        assert doc.suggested_metadata["language"] == "en" and doc.metadata_confidence["language"] == 0.95
        assert doc.department is None, "nothing is applied before a person confirms it"

    reindexed = []

    async def run_inline(doc_id, version):
        reindexed.append((doc_id, version))

    monkeypatch.setattr(review, "_run_inline", run_inline)

    async def _db():
        async with maker() as session:
            yield session

    app = FastAPI()
    app.include_router(review.router)
    app.dependency_overrides[get_current_user] = lambda: User(id=3, username="sec1", hashed_password="x", role=Role.board_secretary, is_active=True)
    app.dependency_overrides[get_db] = _db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        page = await client.get("/review/SB-2026-912")
        assert page.status_code == 200 and "is-low-confidence" in page.text
        resp = await client.post(
            "/review/SB-2026-912/v/1/approve",
            data={"department": "Finance Division", "subject": "বাজেট ২০২৬", "language": "bn", "tags": "Budget, বাজেট"},
        )
        assert resp.status_code == 303 and resp.headers["location"] == "/review?status=approved"
    assert reindexed == [("SB-2026-912", 1)]

    async with maker() as db:
        doc = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == "SB-2026-912"))).scalar_one()
        assert (doc.department, doc.subject, doc.language) == ("Finance Division", "বাজেট ২০২৬", "bn")
        assert doc.metadata_confirmed_by == "sec1" and doc.keywords == ["Budget", "বাজেট"]
        tags = await document_tags(db, "SB-2026-912")
        assert [(t["slug"], t["source"], t["confirmed"]) for t in tags] == [("budget", "reviewer", True), ("বাজেট", "reviewer", True)]
        detail = (await db.execute(select(AuditLog.detail).where(AuditLog.action == "metadata_confirm"))).scalar_one()
        assert "low=department" in detail and "low_corrected=department" in detail


def test_bangla_suspect_words_count_latin_junk_on_bangla_pages_only(monkeypatch):
    from bcp_project.ocr import bangla_suspect_words

    monkeypatch.delenv("OCR_BANGLA_SUSPECT_WORD_CONF", raising=False)
    bangla = "সোনালী ব্যাংক পিএলসি প্রধান কার্যালয় অফিস আদেশ নম্বর"
    words = [
        {"text": "সোনালী", "conf": 30.0},
        {"text": "WIS", "conf": 41.0},
        {"text": "5502", "conf": 22.0},
        {"text": "(@12", "conf": 70.0},
        {"text": "১0৮০০", "conf": 10.0},
    ]
    assert bangla_suspect_words(words, bangla) == 2
    assert bangla_suspect_words(words, "Sonali Bank PLC head office order WIS 5502") == 0
    monkeypatch.setenv("OCR_BANGLA_SUSPECT_WORD_CONF", "80")
    assert bangla_suspect_words(words, bangla) == 3


def test_suspect_bangla_page_routes_to_review_despite_high_confidence(monkeypatch):
    from bcp_project.ocr import PageText, summarize_confidence

    for name in ("OCR_BANGLA_SUSPECT_WORDS", "OCR_BANGLA_PAGE_REVIEW_THRESHOLD", "OCR_REVIEW_THRESHOLD", "OCR_PAGE_REVIEW_THRESHOLD"):
        monkeypatch.delenv(name, raising=False)
    text = "সোনালী ব্যাংক পিএলসি প্রধান কার্যালয় অফিস আদেশ " * 5
    clean = PageText(1, text, 91.0, "ocr", {"bangla_suspect_words": 0})
    suspect = PageText(2, text, 91.0, "ocr", {"bangla_suspect_words": 1})
    assert summarize_confidence([clean])["needs_review"] is False
    summary = summarize_confidence([clean, suspect])
    assert summary["needs_review"] is True and summary["bangla_suspect_pages"] == [2]
    monkeypatch.setenv("OCR_BANGLA_SUSPECT_WORDS", "0")
    assert summarize_confidence([clean, suspect])["needs_review"] is False
