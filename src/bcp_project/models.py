from datetime import date, datetime
from enum import Enum
from typing import List, Optional

from sqlalchemy import BigInteger, Boolean, Column, false, Date, DateTime, Enum as SQLEnum, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Role(str, Enum):
    admin = "admin"
    board_secretary = "board_secretary"
    uploader = "uploader"
    board_member = "board_member"


class MeetingStatus(str, Enum):
    scheduled = "scheduled"
    completed = "completed"
    cancelled = "cancelled"


class AccessMode(str, Enum):
    view_only = "view_only"
    download = "download"


class AccessRequestStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"
    revoked = "revoked"


class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"
    expired = "expired"


class Sensitivity(str, Enum):
    """Document classification. Stored as text; drives AI egress and search visibility."""

    public = "public"
    internal = "internal"
    confidential = "confidential"
    restricted = "restricted"


def parse_sensitivity(value: Optional[str]) -> str:
    cleaned = (value or "").strip().lower()
    try:
        return Sensitivity(cleaned).value
    except ValueError:
        return Sensitivity.internal.value


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    hashed_password: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[Role] = mapped_column(SQLEnum(Role), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DocumentRecord(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    doc_date: Mapped[Date] = mapped_column(Date, nullable=False)
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False)
    keywords: Mapped[List[str]] = mapped_column(JSON, nullable=False)
    summary_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    file_location: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    sensitivity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Sensitivity.internal.value, server_default=Sensitivity.internal.value
    )
    ingest_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="indexed", server_default="indexed"
    )
    ingest_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ingest_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    page_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    reference_number: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    department: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    subject: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    ocr_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    verification_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="not_required", server_default="not_required"
    )
    suggested_metadata: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    metadata_confirmed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    metadata_confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    content_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    meeting_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retention_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    legal_hold_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    legal_hold_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    legal_hold_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_documents_doc_type", "doc_type"),
        Index("ix_documents_retention_until", "retention_until"),
        Index("ix_documents_doc_date", "doc_date"),
        Index("ix_documents_sensitivity", "sensitivity"),
        Index("ix_documents_ingest_status", "ingest_status"),
        Index("ix_documents_reference_number", "reference_number"),
        Index("ix_documents_department", "department"),
        Index("ix_documents_category", "category"),
        Index("ix_documents_verification_status", "verification_status"),
        Index("ix_documents_meeting_id", "meeting_id"),
    )


class DocumentVersion(Base):
    """Immutable record of each uploaded file. ``file_location`` is the PDF used for
    viewing; ``original_location`` is the untouched upload when it was an image."""

    __tablename__ = "document_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    file_location: Mapped[str] = mapped_column(Text, nullable=False)
    original_location: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="approved", server_default="approved")
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=datetime.utcnow)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_document_versions_doc_version", "doc_id", "version", unique=True),
        Index("ix_document_versions_sha256", "sha256"),
        Index("ix_document_versions_status", "status"),
    )


class DocumentPage(Base):
    """Extracted (or reviewer-corrected) text for one page of one version."""

    __tablename__ = "document_pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    ocr_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="text", server_default="text")
    edited_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    edited_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ix_document_pages_doc_version_page", "doc_id", "version", "page_number", unique=True),)


class JobRun(Base):
    """One row per background job; the stream carries work, this carries history."""

    __tablename__ = "job_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    queue: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    resource_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    worker: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_job_runs_status", "status"),
        Index("ix_job_runs_resource", "resource_type", "resource_id"),
        Index("ix_job_runs_created_at", "created_at"),
    )


class ModelRegistryEntry(Base):
    """One activation of an AI route for a task. The newest active row overrides env."""

    __tablename__ = "model_registry"
    __table_args__ = (Index("ix_model_registry_task_active", "task", "is_active"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task: Mapped[str] = mapped_column(String(48), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    params: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    activated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=datetime.utcnow)
    deactivated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class OutboxEvent(Base):
    """Written in the same transaction as the domain change; the relay publishes it."""

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ix_outbox_events_unpublished", "published_at", "id"),)


class UserMfa(Base):
    """TOTP enrolment. The shared secret is encrypted at rest (see mfa.py)."""

    __tablename__ = "user_mfa"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_used_step: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    recovery_code_hashes: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class DocumentAccessRequest(Base):
    __tablename__ = "document_access_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    requester_username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    requested_mode: Mapped[AccessMode] = mapped_column(SQLEnum(AccessMode), nullable=False)
    status: Mapped[AccessRequestStatus] = mapped_column(
        SQLEnum(AccessRequestStatus), default=AccessRequestStatus.pending, nullable=False, index=True
    )
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    actor_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    tool_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    # Hash chain, filled in id order by the sealer; Postgres forbids any other UPDATE or DELETE.
    prev_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    row_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class IntegrationCursor(Base):
    """High-water mark for outbound integrations such as the SIEM forwarder."""

    __tablename__ = "integration_cursors"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ServiceClient(Base):
    """Machine / agent identity for /api/v1 and MCP (API key auth)."""

    __tablename__ = "service_clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_agent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class ApprovalRequest(Base):
    """Human-in-the-loop gate for mutating agent/tool actions."""

    __tablename__ = "approval_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    requester: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False, default="agent")
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    arguments_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        SQLEnum(ApprovalStatus), default=ApprovalStatus.pending, nullable=False, index=True
    )
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class BoardMeeting(Base):
    __tablename__ = "board_meetings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    location: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    agenda: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[MeetingStatus] = mapped_column(SQLEnum(MeetingStatus), default=MeetingStatus.scheduled)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    attendance_open: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attendance_opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    attendance_closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    transcription_status: Mapped[str] = mapped_column(String(16), default="off", nullable=False)
    transcription_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    transcription_stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    transcription_started_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    transcription_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    minutes_status: Mapped[str] = mapped_column(String(16), default="off", nullable=False)
    minutes_draft: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    minutes_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    minutes_generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Review lifecycle, separate from minutes_status (generation progress).
    minutes_state: Mapped[str] = mapped_column(String(16), default="draft", server_default="draft", nullable=False, index=True)
    minutes_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    minutes_edited_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    minutes_edited_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    minutes_submitted_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    minutes_submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    minutes_approved_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    minutes_approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    minutes_review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    minutes_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    minutes_doc_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    recording_filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    recording_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    speaker_map: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    diarization_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)


class MeetingDecision(Base):
    __tablename__ = "meeting_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("board_meetings.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    agenda_item: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="manual", server_default="manual", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class MeetingAction(Base):
    __tablename__ = "meeting_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("board_meetings.id"), nullable=False, index=True)
    decision_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("meeting_decisions.id"), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    division: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open", server_default="open", nullable=False, index=True)
    evidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="manual", server_default="manual", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class BoardBriefing(Base):
    """Board-paper briefing for one document version: eight cited headings and a review state."""

    __tablename__ = "board_briefings"
    __table_args__ = (UniqueConstraint("doc_id", "version", name="uq_board_briefing_doc_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Review lifecycle: ai_draft -> management_reviewed -> approved.
    state: Mapped[str] = mapped_column(String(24), default="ai_draft", server_default="ai_draft", nullable=False, index=True)
    # Generation progress: pending -> generating -> ready | failed.
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending", nullable=False)
    content: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="ai", server_default="ai", nullable=False)
    provenance: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    edited_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    edited_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class NotificationEvent(Base):
    """In-app notification log for invites, reminders, and generated events."""

    __tablename__ = "notification_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="")
    meeting_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("board_meetings.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class MeetingDocument(Base):
    __tablename__ = "meeting_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("board_meetings.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    file_location: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    archive_doc_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)


class MeetingRecord(Base):
    """One sealed artifact of a meeting: recording, transcript, a draft-minutes version, or approved minutes.

    Bytes are stored write-once; ``sha256`` is the hash of exactly those bytes. Transcript and
    approved minutes are also archive documents (``archive_doc_id``) so they are searchable.
    """

    __tablename__ = "meeting_records"
    __table_args__ = (UniqueConstraint("meeting_id", "kind", "version", name="uq_meeting_record_kind_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("board_meetings.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    file_location: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    archive_doc_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MeetingInvitation(Base):
    __tablename__ = "meeting_invitations"
    __table_args__ = (UniqueConstraint("meeting_id", "username", name="uq_meeting_invitation"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("board_meetings.id"), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    invited_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    invitation_email_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    reminder_48h_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    reminder_24h_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class MeetingAttendance(Base):
    __tablename__ = "meeting_attendance"
    __table_args__ = (UniqueConstraint("meeting_id", "username", name="uq_meeting_attendance"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(Integer, ForeignKey("board_meetings.id"), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    signed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    signature_file: Mapped[str] = mapped_column(Text, nullable=False)


class RetentionRule(Base):
    """How long one document type is kept. No rule, or ``retain_years`` NULL, means permanent."""

    __tablename__ = "retention_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    retain_years: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="doc_date", server_default="doc_date")
    authority: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class DocumentDisposal(Base):
    """Four-eyes disposal of a document past retention. After execution the row is the tombstone."""

    __tablename__ = "document_disposals"
    __table_args__ = (Index("ix_document_disposals_status", "status", "proposed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed", server_default="proposed")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    doc_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    doc_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    sensitivity: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    retention_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    tombstone: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class AiRouteChange(Base):
    """A model-route change waiting for a second administrator (AI_ROUTE_FOUR_EYES=1)."""

    __tablename__ = "ai_route_changes"
    __table_args__ = (Index("ix_ai_route_changes_status", "status", "requested_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    task: Mapped[str] = mapped_column(String(48), nullable=False)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    source_entry_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
