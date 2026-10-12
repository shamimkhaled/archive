"""Forward the audit log to the bank SIEM as CEF or JSON over syslog (UDP/TCP/TLS) or HTTP.

Rows go out in id order from a cursor in ``integration_cursors``; the cursor moves only after the
SIEM accepted the batch, so delivery is at-least-once. By default only sealed rows are sent (the
contiguous sealed prefix), so every event carries its chain hash and the SIEM copy can prove the
database copy was not edited later.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import ssl
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditLog, IntegrationCursor
from .audit_chain import audit_row_payload

logger = logging.getLogger("bcp_project.services.siem")

SIEM_CURSOR = "siem"
VENDOR, PRODUCT = "Sonali Bank PLC", "Board Archive"
SYSLOG_FACILITY_AUDIT = 13
APP_NAME = "sonali-archive"

_HIGH = ("denied", "failed", "locked", "blocked", "four_eyes", "invalid", "rejected", "broken")
_MEDIUM = (
    "delete", "reopen", "amend", "revoke", "role", "approve", "reject", "export", "download", "fallback", "provisioned",
)


def siem_enabled() -> bool:
    return (os.getenv("SIEM_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    return default if not raw else raw in {"1", "true", "yes", "on"}


def severity(action: str) -> int:
    """CEF severity 0-10."""
    name = (action or "").lower()
    if any(word in name for word in _HIGH):
        return 7
    if any(word in name for word in _MEDIUM):
        return 5
    return 3


def _cef_header(value: Any) -> str:
    return str(value if value is not None else "").replace("\\", "\\\\").replace("|", "\\|")


def _cef_ext(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("\\", "\\\\").replace("=", "\\=").replace("\r", "\\r").replace("\n", "\\n")


def to_cef(row: AuditLog, version: str = "1.0") -> str:
    p = audit_row_payload(row)
    created = row.created_at.replace(tzinfo=timezone.utc) if row.created_at else datetime.now(timezone.utc)
    ext = {
        "rt": int(created.timestamp() * 1000),
        "externalId": p["id"],
        "suser": p["username"],
        "src": p["ip_address"],
        "act": p["action"],
        "cs1Label": "resourceType",
        "cs1": p["resource_type"],
        "cs2Label": "resourceId",
        "cs2": p["resource_id"],
        "cs3Label": "correlationId",
        "cs3": p["correlation_id"],
        "cs4Label": "rowHash",
        "cs4": row.row_hash,
        "cs5Label": "prevHash",
        "cs5": row.prev_hash,
        "cs6Label": "actorType",
        "cs6": p["actor_type"],
        "msg": (p["detail"] or "")[:1000],
    }
    extension = " ".join(f"{key}={_cef_ext(value)}" for key, value in ext.items() if value not in (None, ""))
    header = "|".join(
        _cef_header(v)
        for v in ("CEF:0", VENDOR, PRODUCT, version, p["action"], p["action"], severity(p["action"]))
    )
    return f"{header}|{extension}"


def to_json(row: AuditLog) -> str:
    payload = audit_row_payload(row)
    payload.update(row_hash=row.row_hash, prev_hash=row.prev_hash, severity=severity(row.action), product=PRODUCT)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def syslog_frame(message: str, row: AuditLog, *, hostname: Optional[str] = None) -> str:
    """RFC 5424 line; syslog severity warning for high-severity events, else informational."""
    level = 4 if severity(row.action) >= 7 else 6
    pri = SYSLOG_FACILITY_AUDIT * 8 + level
    stamp = (row.created_at or datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    host = hostname or socket.gethostname()
    return f"<{pri}>1 {stamp} {host} {APP_NAME} - audit - {message}"


class SyslogSender:
    def __init__(self, host: str, port: int, transport: str = "udp", *, ca_file: Optional[str] = None, timeout: float = 10.0):
        self.host, self.port, self.transport = host, int(port), transport
        self.ca_file, self.timeout = ca_file, timeout

    def send(self, lines: Sequence[str]) -> None:
        if self.transport == "udp":
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                for line in lines:
                    sock.sendto(line.encode("utf-8")[:65000], (self.host, self.port))
            return
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.transport == "tls":
            context = ssl.create_default_context(cafile=self.ca_file)
            raw = context.wrap_socket(raw, server_hostname=self.host)
        with raw:
            for line in lines:
                data = line.encode("utf-8")
                raw.sendall(f"{len(data)} ".encode("ascii") + data)  # RFC 6587 octet counting


class HttpSender:
    """POSTs a batch as NDJSON (Splunk HEC, Elastic, or a bank collector)."""

    def __init__(self, url: str, token: str = "", scheme: str = "Bearer", timeout: float = 15.0):
        self.url, self.token, self.scheme, self.timeout = url, token, scheme, timeout

    def send(self, lines: Sequence[str]) -> None:
        headers = {"Content-Type": "application/x-ndjson"}
        if self.token:
            headers["Authorization"] = f"{self.scheme} {self.token}".strip()
        req = Request(self.url, data="\n".join(lines).encode("utf-8"), headers=headers, method="POST")
        with urlopen(req, timeout=self.timeout) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"SIEM HTTP collector returned {resp.status}")


def sender_from_env() -> Any:
    transport = (os.getenv("SIEM_TRANSPORT") or "udp").strip().lower()
    if transport == "http":
        url = os.getenv("SIEM_HTTP_URL") or ""
        if not url:
            raise RuntimeError("SIEM_TRANSPORT=http needs SIEM_HTTP_URL")
        return HttpSender(url, os.getenv("SIEM_HTTP_TOKEN") or "", os.getenv("SIEM_HTTP_AUTH_SCHEME") or "Bearer")
    host = os.getenv("SIEM_HOST") or ""
    if not host:
        raise RuntimeError("SIEM_HOST is not set")
    default_port = 6514 if transport == "tls" else 514
    return SyslogSender(host, int(os.getenv("SIEM_PORT") or default_port), transport, ca_file=os.getenv("SIEM_CA_FILE") or None)


def format_rows(rows: Sequence[AuditLog], fmt: str, *, syslog: bool) -> List[str]:
    render: Callable[[AuditLog], str] = to_json if fmt == "json" else to_cef
    lines = [render(row) for row in rows]
    return [syslog_frame(line, row) for line, row in zip(lines, rows)] if syslog else lines


async def forward_audit(db: AsyncSession, *, sender: Any = None, limit: int = 500) -> Dict[str, Any]:
    import asyncio

    cursor = await db.get(IntegrationCursor, SIEM_CURSOR)
    if cursor is None:
        cursor = IntegrationCursor(name=SIEM_CURSOR, last_id=int(os.getenv("SIEM_START_ID") or 0))
        db.add(cursor)
        await db.flush()
    rows = (
        await db.execute(select(AuditLog).where(AuditLog.id > cursor.last_id).order_by(AuditLog.id).limit(limit))
    ).scalars().all()
    if _env_flag("SIEM_REQUIRE_SEALED", True):
        sealed: List[AuditLog] = []
        for row in rows:
            if row.row_hash is None:
                break
            sealed.append(row)
        rows = sealed
    if not rows:
        await db.commit()
        return {"sent": 0, "last_id": cursor.last_id}
    sender = sender or sender_from_env()
    fmt = (os.getenv("SIEM_FORMAT") or "cef").strip().lower()
    lines = format_rows(rows, fmt, syslog=not isinstance(sender, HttpSender))
    await asyncio.to_thread(sender.send, lines)
    cursor.last_id = rows[-1].id
    cursor.updated_at = datetime.utcnow()
    await db.commit()
    return {"sent": len(rows), "last_id": cursor.last_id}
