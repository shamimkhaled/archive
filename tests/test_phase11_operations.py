"""Operations gaps: Prometheus metrics, retention and legal hold, model-route approval."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import func, select


@pytest.fixture
def metrics(monkeypatch):
    from bcp_project import observability

    monkeypatch.delenv("METRICS_MULTIPROC_DIR", raising=False)
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    observability.reset_metrics_for_tests()
    yield observability
    observability.reset_metrics_for_tests()


# --- metrics -----------------------------------------------------------------------------------


def test_labelled_counters_render_once_per_name(metrics):
    metrics.incr("job_runs", labels={"job_type": "document_ingest", "outcome": "ok"})
    metrics.incr("job_runs", labels={"job_type": "document_ingest", "outcome": "dead"}, amount=2)
    metrics.incr("auth_login_failures", amount=1234567)
    text = metrics.render_prometheus()
    assert text.count("# TYPE bcp_job_runs_total counter") == 1
    assert 'bcp_job_runs_total{job_type="document_ingest",outcome="dead"} 2' in text
    assert "bcp_auth_login_failures_total 1234567" in text


def test_summaries_are_exported_in_seconds(metrics):
    metrics.observe_ms("search.hybrid", 250.0)
    text = metrics.render_prometheus()
    assert 'bcp_search_hybrid_seconds{quantile="0.5"} 0.250000' in text
    assert "_ms" not in text


def test_multiprocess_dir_sums_sibling_processes(metrics, monkeypatch, tmp_path):
    monkeypatch.setenv("METRICS_MULTIPROC_DIR", str(tmp_path))
    metrics.incr("job_runs", labels={"job_type": "x", "outcome": "ok"}, amount=3)
    metrics.observe_histogram("job_duration", 1.0, {"job_type": "x"})
    sibling = {
        "counters": [["job_runs", [["job_type", "x"], ["outcome", "ok"]], 4]],
        "hist": [["job_duration", [["job_type", "x"]], [0] * len(metrics.BUCKETS) + [1], 600.0]],
        "timings": {},
    }
    (tmp_path / "1-1.json").write_text(json.dumps(sibling))
    text = metrics.render_prometheus()
    assert 'bcp_job_runs_total{job_type="x",outcome="ok"} 7' in text
    assert 'bcp_job_duration_seconds_count{job_type="x"} 2' in text
    assert any(p.name.startswith(metrics._PROCESS_ID) for p in tmp_path.iterdir())


def test_throttled_flush_is_written_later(metrics, monkeypatch, tmp_path):
    monkeypatch.setenv("METRICS_MULTIPROC_DIR", str(tmp_path))
    monkeypatch.setattr(metrics, "_FLUSH_INTERVAL_S", 0.2)
    metrics.flush_state()
    metrics.incr("auth_logins", labels={"method": "password"})
    metrics.flush_state()
    state_file = tmp_path / f"{metrics._PROCESS_ID}.json"
    assert '"auth_logins"' not in state_file.read_text()
    deadline = time.monotonic() + 3
    while '"auth_logins"' not in state_file.read_text() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert '"auth_logins"' in state_file.read_text()


def test_metrics_access_policy(metrics, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    assert metrics.metrics_access(None) is None
    monkeypatch.setenv("APP_ENV", "production")
    assert metrics.metrics_access(None) == 404
    monkeypatch.setenv("METRICS_TOKEN", "s3cret-token")
    assert metrics.metrics_access("Bearer wrong") == 401
    assert metrics.metrics_access("Bearer s3cret-token") is None


def test_worker_metrics_server_requires_token(metrics, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    assert metrics.start_metrics_server(0) is None  # no token in production: refuse to serve
    monkeypatch.setenv("METRICS_TOKEN", "worker-token")
    monkeypatch.setenv("METRICS_HOST", "127.0.0.1")
    metrics.incr("job_runs", labels={"job_type": "x", "outcome": "ok"})
    server = metrics.start_metrics_server(0)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/metrics"
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(url, timeout=5)
        assert denied.value.code == 401
        req = urllib.request.Request(url, headers={"Authorization": "Bearer worker-token"})
        body = urllib.request.urlopen(req, timeout=5).read().decode()
        assert 'bcp_job_runs_total{job_type="x",outcome="ok"} 1' in body
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_worker_records_outcome_and_duration(metrics, monkeypatch):
    from bcp_project.jobs import worker

    async def _noop(*_a, **_k):
        return None

    async def _boom(_job):
        raise RuntimeError("parse failed")

    async def _no_retry(*_a, **_k):
        return False

    for name in ("mark_running", "mark_succeeded", "mark_failed", "ack_job"):
        monkeypatch.setattr(worker, name, _noop)
    monkeypatch.setattr(worker, "dispatch_job", _noop)
    assert await worker.process_one({"id": "1", "type": "document_ingest"}, session_factory=None, max_attempts=3)
    monkeypatch.setattr(worker, "dispatch_job", _boom)
    monkeypatch.setattr(worker, "requeue_job", _no_retry)
    assert not await worker.process_one({"id": "2", "type": "document_ingest"}, session_factory=None, max_attempts=3)
    text = metrics.render_prometheus()
    assert 'bcp_job_runs_total{job_type="document_ingest",outcome="ok"} 1' in text
    assert 'bcp_job_runs_total{job_type="document_ingest",outcome="dead"} 1' in text
    assert 'bcp_job_duration_seconds_count{job_type="document_ingest"} 2' in text


# --- retention, legal hold, disposal ------------------------------------------------------------


@pytest_asyncio.fixture
async def maker(tmp_path):
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from bcp_project.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'p11.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed(db, doc_id="SB-LTR-1", *, doc_type="Letter", doc_date=date(2010, 2, 28), versions=2):
    from bcp_project.models import DocumentPage, DocumentRecord, DocumentVersion

    db.add(
        DocumentRecord(
            doc_id=doc_id,
            doc_date=doc_date,
            doc_type=doc_type,
            keywords=[],
            file_location=f"pdfs/{doc_id}/v{versions}.pdf",
            uploaded_by="uploader1",
            title=f"{doc_type} {doc_id}",
            current_version=versions,
        )
    )
    for n in range(1, versions + 1):
        db.add(
            DocumentVersion(
                doc_id=doc_id,
                version=n,
                sha256=f"{n:064d}",
                file_location=f"pdfs/{doc_id}/v{n}.pdf",
                original_location=f"originals/{doc_id}/v{n}.jpg" if n == 1 else None,
                created_by="uploader1",
            )
        )
        db.add(DocumentPage(doc_id=doc_id, version=n, page_number=1, text="text"))
    await db.commit()


async def _actions(db):
    from bcp_project.models import AuditLog

    return [a.action for a in (await db.execute(select(AuditLog).order_by(AuditLog.id))).scalars()]


def test_add_years_handles_leap_day():
    from bcp_project.services.retention import add_years

    assert add_years(date(2012, 2, 29), 7) == date(2019, 2, 28)
    assert add_years(date(2012, 2, 29), 8) == date(2020, 2, 29)


@pytest.mark.asyncio
async def test_rules_set_and_clear_retention_dates(maker):
    from bcp_project.models import DocumentRecord
    from bcp_project.services import retention

    async with maker() as db:
        await _seed(db, "L1", doc_type="Letter")
        await _seed(db, "M1", doc_type="Meeting Minutes")
        with pytest.raises(retention.RetentionError):
            await retention.set_rule(db, doc_type="Letter", retain_years=0, trigger="doc_date", authority=None, actor="admin1")
        await retention.set_rule(db, doc_type="  letter ", retain_years=10, trigger="doc_date", authority="RM policy 4.2", actor="admin1")
        await db.commit()
        docs = {d.doc_id: d for d in (await db.execute(select(DocumentRecord))).scalars()}
        assert docs["L1"].retention_until == date(2020, 2, 28)
        assert docs["M1"].retention_until is None  # no rule: permanent
        rule = (await retention.retention_overview(db))["rules"][0]
        assert rule["doc_type"] == "letter" and rule["authority"] == "RM policy 4.2"
        await retention.delete_rule(db, rule_id=rule["id"], actor="admin1")
        await db.commit()
        assert (await db.scalar(select(DocumentRecord.retention_until).where(DocumentRecord.doc_id == "L1"))) is None
        assert await _actions(db) == ["retention_rule_set", "retention_rule_delete"]


@pytest.mark.asyncio
async def test_disposal_needs_two_people_and_leaves_a_tombstone(maker):
    from bcp_project.models import DocumentPage, DocumentRecord, DocumentVersion
    from bcp_project.services import retention

    deleted_vectors, deleted_objects = [], []
    async with maker() as db:
        await _seed(db, "L1")
        await retention.set_rule(db, doc_type="Letter", retain_years=5, trigger="doc_date", authority=None, actor="admin1")
        due = await retention.due_for_disposal(db)
        assert [d["doc_id"] for d in due] == ["L1"] and due[0]["blockers"] == []

        row = await retention.propose_disposal(db, doc_id="L1", reason="Schedule 7, letters 5y", actor="sec1")
        await db.commit()
        with pytest.raises(retention.RetentionError, match="second person"):
            await retention.approve_disposal(db, disposal_id=row.id, actor="sec1")
        with pytest.raises(retention.RetentionError, match="already open"):
            await retention.propose_disposal(db, doc_id="L1", reason="again", actor="admin1")

        row = await retention.approve_disposal(
            db,
            disposal_id=row.id,
            actor="admin1",
            delete_vectors=deleted_vectors.append,
            delete_object=lambda loc: deleted_objects.append(loc) or 1,
        )
        assert row.status == "executed" and row.reviewed_by == "admin1"
        assert deleted_vectors == ["L1"]
        assert set(deleted_objects) == {"pdfs/L1/v1.pdf", "pdfs/L1/v2.pdf", "originals/L1/v1.jpg"}
        for model in (DocumentRecord, DocumentVersion, DocumentPage):
            assert await db.scalar(select(func.count()).select_from(model)) == 0
        assert [v["sha256"] for v in row.tombstone["versions"]] == [f"{1:064d}", f"{2:064d}"]
        assert row.tombstone["approved_by"] == "admin1" and row.tombstone["proposed_by"] == "sec1"
        assert "cannot be reused" in await retention.doc_id_conflict(db, "L1")
        assert (await _actions(db))[-3:] == ["disposal_propose", "disposal_approve", "document_disposed"]


@pytest.mark.asyncio
async def test_legal_hold_and_board_records_block_disposal(maker):
    from bcp_project.models import MeetingDocument
    from bcp_project.services import retention

    async with maker() as db:
        await _seed(db, "L1")
        await _seed(db, "L2")
        await retention.set_rule(db, doc_type="Letter", retain_years=5, trigger="doc_date", authority=None, actor="admin1")
        with pytest.raises(retention.RetentionError, match="reason"):
            await retention.set_legal_hold(db, doc_id="L1", hold=True, reason=" ", actor="sec1")
        await retention.set_legal_hold(db, doc_id="L1", hold=True, reason="Court order 12/2026", actor="sec1")
        db.add(MeetingDocument(meeting_id=1, title="paper", file_location="x", uploaded_by="sec1", archive_doc_id="L2"))
        await db.commit()

        due = {d["doc_id"]: d for d in await retention.due_for_disposal(db)}
        assert "L1" not in due  # held documents are not listed as due
        assert due["L2"]["blockers"] == ["part of a Board meeting record"]
        with pytest.raises(retention.RetentionError, match="legal hold"):
            await retention.propose_disposal(db, doc_id="L1", reason="x", actor="sec1")
        with pytest.raises(retention.RetentionError, match="Board meeting"):
            await retention.propose_disposal(db, doc_id="L2", reason="x", actor="sec1")

        await retention.set_legal_hold(db, doc_id="L1", hold=False, reason="Order lifted", actor="admin1")
        assert await retention.propose_disposal(db, doc_id="L1", reason="x", actor="sec1")
        assert "legal_hold_set" in await _actions(db) and "legal_hold_release" in await _actions(db)


@pytest.mark.asyncio
async def test_failed_disposal_keeps_the_record_and_can_be_retried(maker):
    from bcp_project.models import DocumentRecord
    from bcp_project.services import retention

    def locked(_location):
        raise RuntimeError("AccessDenied: object is WORM protected")

    async with maker() as db:
        await _seed(db, "L1")
        await retention.set_rule(db, doc_type="Letter", retain_years=5, trigger="doc_date", authority=None, actor="admin1")
        row = await retention.propose_disposal(db, doc_id="L1", reason="x", actor="sec1")
        await db.commit()
        row = await retention.approve_disposal(
            db, disposal_id=row.id, actor="admin1", delete_vectors=lambda _d: None, delete_object=locked
        )
        assert row.status == "approved" and "WORM" in row.last_error
        assert await db.scalar(select(func.count()).select_from(DocumentRecord)) == 1
        assert "disposal_failed" in await _actions(db)

        with pytest.raises(retention.RetentionError, match="proposer or the approver"):
            await retention.approve_disposal(db, disposal_id=row.id, actor="someone_else")
        await retention.set_legal_hold(db, doc_id="L1", hold=True, reason="Audit query", actor="admin1")
        await db.commit()
        with pytest.raises(retention.RetentionError, match="legal hold"):
            await retention.approve_disposal(db, disposal_id=row.id, actor="sec1", delete_vectors=lambda _d: None)
        await retention.set_legal_hold(db, doc_id="L1", hold=False, reason="Closed", actor="admin1")
        await db.commit()
        row = await retention.approve_disposal(
            db, disposal_id=row.id, actor="sec1", delete_vectors=lambda _d: None, delete_object=lambda _l: 1
        )
        assert row.status == "executed" and row.last_error is None


def test_delete_stored_object_removes_every_s3_version(monkeypatch):
    from bcp_project import aws_utils, intake

    calls = []

    class FakeS3:
        def list_object_versions(self, Bucket, Prefix):
            return {
                "Versions": [{"Key": Prefix, "VersionId": "a"}, {"Key": Prefix + ".bak", "VersionId": "x"}],
                "DeleteMarkers": [{"Key": Prefix, "VersionId": "m"}],
            }

        def delete_object(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(aws_utils, "get_s3_client", lambda: FakeS3())
    assert intake.delete_stored_object("s3://bucket/pdfs/L1/v1.pdf") == 2
    assert [c["VersionId"] for c in calls] == ["a", "m"]
    assert all(c["Key"] == "pdfs/L1/v1.pdf" and "BypassGovernanceRetention" not in c for c in calls)


def test_delete_stored_object_unlinks_read_only_local_file(tmp_path, monkeypatch):
    from bcp_project import intake

    target = tmp_path / "pdfs" / "a.pdf"
    target.parent.mkdir()
    target.write_bytes(b"%PDF")
    target.chmod(0o444)
    monkeypatch.setattr(intake, "local_path_for", lambda loc: tmp_path / loc)
    assert intake.delete_stored_object("pdfs/a.pdf") == 1
    assert not target.exists()
    assert intake.delete_stored_object("pdfs/a.pdf") == 0


@pytest.mark.asyncio
async def test_retention_page_renders_and_enforces_roles(maker):
    import httpx
    from fastapi import FastAPI

    from bcp_project.deps import get_current_user, get_db
    from bcp_project.models import Role, User
    from bcp_project.routers import admin_retention
    from bcp_project.services import retention

    async with maker() as db:
        await _seed(db, "L1")
        await retention.set_rule(db, doc_type="Letter", retain_years=5, trigger="doc_date", authority="RM 4.2", actor="admin1")
        await retention.propose_disposal(db, doc_id="L1", reason="Schedule 7", actor="sec1")
        await db.commit()

    user = User(username="admin1", hashed_password="x", role=Role.admin, is_active=True)

    async def _db():
        async with maker() as session:
            yield session

    app = FastAPI()
    app.include_router(admin_retention.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        page = await client.get("/admin/retention")
        assert page.status_code == 200
        assert "RM 4.2" in page.text and "Approve and dispose" in page.text
        user.role = Role.uploader
        assert (await client.get("/api/admin/retention")).status_code == 403
        user.role = Role.board_secretary
        denied = await client.post("/admin/retention/rules", data={"doc_type": "Memo", "retain_years": "3"})
        assert denied.status_code == 403  # only admins change the schedule


# --- model-route change approval ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_change_applies_directly_without_four_eyes(maker, monkeypatch):
    from bcp_project.ai import route_changes
    from bcp_project.models import AiRouteChange, ModelRegistryEntry

    monkeypatch.delenv("AI_ROUTE_FOUR_EYES", raising=False)
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://gpu.local:8000/v1")
    async with maker() as db:
        change = await route_changes.submit(
            db, kind="switch", task="chat.ask", provider="local", model="qwen2.5:14b-instruct", actor="admin1"
        )
        await db.commit()
        assert change.status == "applied"
        active = (await db.execute(select(ModelRegistryEntry).where(ModelRegistryEntry.is_active.is_(True)))).scalars().all()
        assert [(r.task, r.provider, r.model) for r in active] == [("chat.ask", "local", "qwen2.5:14b-instruct")]
        assert await db.scalar(select(func.count()).select_from(AiRouteChange)) == 0
        assert await _actions(db) == ["ai_route_switch"]


@pytest.mark.asyncio
async def test_route_change_needs_second_admin_when_four_eyes_on(maker, monkeypatch):
    from bcp_project.ai import route_changes
    from bcp_project.models import ModelRegistryEntry

    monkeypatch.setenv("AI_ROUTE_FOUR_EYES", "1")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://gpu.local:8000/v1")
    async with maker() as db:
        with pytest.raises(route_changes.RouteChangeError, match="Unknown task"):
            await route_changes.submit(db, kind="switch", task="chat.nope", provider="local", model="m", actor="admin1")
        change = await route_changes.submit(
            db, kind="switch", task="chat.ask", provider="local", model="qwen2.5:14b-instruct", notes="UAT", actor="admin1"
        )
        await db.commit()
        assert change.status == "pending"
        assert await db.scalar(select(func.count()).select_from(ModelRegistryEntry)) == 0
        with pytest.raises(route_changes.RouteChangeError, match="already waiting"):
            await route_changes.submit(db, kind="reset", task="chat.ask", actor="admin2")
        with pytest.raises(route_changes.RouteChangeError, match="second administrator"):
            await route_changes.approve(db, change_id=change.id, actor="admin1")

        await route_changes.approve(db, change_id=change.id, actor="admin2")
        await db.commit()
        entry = (await db.execute(select(ModelRegistryEntry))).scalar_one()
        assert entry.is_active and entry.activated_by == "admin1"
        assert "approved by admin2" in entry.notes
        assert await _actions(db) == ["ai_route_change_requested", "ai_route_change_approved", "ai_route_switch"]

        rollback = await route_changes.submit(db, kind="reset", task="chat.ask", actor="admin2")
        await route_changes.reject(db, change_id=rollback.id, actor="admin1", note="keep local for UAT")
        await db.commit()
        assert rollback.status == "rejected"
        assert (await db.execute(select(ModelRegistryEntry))).scalar_one().is_active
        listed = await route_changes.recent(db)
        assert [c["status"] for c in listed] == ["rejected", "applied"]


# --- client address behind proxies --------------------------------------------------------------


def _req(xff=None, peer="10.0.0.5"):
    from starlette.requests import Request

    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 1234)})


def test_client_ip_ignores_client_supplied_forwarded_entries(monkeypatch):
    from bcp_project.security import client_ip

    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    assert client_ip(_req("6.6.6.6, 203.0.113.9")) == "203.0.113.9"  # one proxy appended the real address
    assert client_ip(_req()) == "10.0.0.5"
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
    assert client_ip(_req("6.6.6.6, 203.0.113.9, 10.1.1.1")) == "203.0.113.9"
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "0")
    assert client_ip(_req("6.6.6.6")) == "10.0.0.5"


def test_login_lockout_cannot_be_dodged_by_rotating_forwarded_for(monkeypatch):
    from bcp_project.security import LoginRateLimiter

    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    limiter = LoginRateLimiter(max_attempts=3)
    for fake in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
        limiter.record_failure(_req(f"{fake}, 203.0.113.9"), "alice")
    assert limiter.is_blocked(_req("4.4.4.4, 203.0.113.9"), "alice")
