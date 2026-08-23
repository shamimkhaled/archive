from datetime import datetime
from enum import Enum
from typing import List, Optional

from sqlalchemy import Boolean, Column, Date, DateTime, Enum as SQLEnum, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class BoardMeeting(Base):
    __tablename__ = "board_meetings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
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
