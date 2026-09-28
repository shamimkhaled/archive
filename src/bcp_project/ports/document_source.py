"""Document source adapters — local inbox and optional SFTP pull."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bcp_project.ports.document_source")


class LocalInboxDocumentSource:
    """Pull PDFs from a local/network-mounted inbox directory (bank file drop)."""

    def __init__(self, inbox_dir: Optional[str] = None, processed_dir: Optional[str] = None) -> None:
        self.inbox = Path(inbox_dir or os.getenv("DOCUMENT_INBOX_DIR", "inbox_drop"))
        self.processed = Path(
            processed_dir or os.getenv("DOCUMENT_INBOX_PROCESSED_DIR", str(self.inbox / "processed"))
        )
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.processed.mkdir(parents=True, exist_ok=True)

    def list_pending(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for path in sorted(self.inbox.glob("*.pdf"))[: max(1, int(limit))]:
            items.append(
                {
                    "id": path.name,
                    "name": path.name,
                    "size": path.stat().st_size,
                    "source": "local_inbox",
                    "path": str(path),
                }
            )
        return items

    def fetch_bytes(self, item_id: str) -> bytes:
        safe = Path(item_id).name
        path = (self.inbox / safe).resolve()
        path.relative_to(self.inbox.resolve())
        if not path.exists():
            raise FileNotFoundError(f"Inbox item not found: {safe}")
        return path.read_bytes()

    def mark_processed(self, item_id: str) -> None:
        safe = Path(item_id).name
        src = (self.inbox / safe).resolve()
        src.relative_to(self.inbox.resolve())
        dest = self.processed / safe
        if src.exists():
            shutil.move(str(src), str(dest))


class SftpDocumentSource:
    """SFTP inbox pull. Requires optional `paramiko` package when enabled."""

    def __init__(self) -> None:
        self.host = os.getenv("SFTP_HOST", "")
        self.port = int(os.getenv("SFTP_PORT", "22"))
        self.username = os.getenv("SFTP_USERNAME", "")
        self.password = os.getenv("SFTP_PASSWORD", "")
        self.remote_dir = os.getenv("SFTP_REMOTE_DIR", "/inbox")
        self.processed_dir = os.getenv("SFTP_PROCESSED_DIR", "/inbox/processed")

    def _client(self):
        try:
            import paramiko  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "SFTP document source requires paramiko. pip install paramiko"
            ) from exc
        if not self.host or not self.username:
            raise RuntimeError("SFTP_HOST and SFTP_USERNAME are required")
        transport = paramiko.Transport((self.host, self.port))
        transport.connect(username=self.username, password=self.password or None)
        return paramiko.SFTPClient.from_transport(transport), transport

    def list_pending(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        sftp, transport = self._client()
        try:
            entries = sftp.listdir_attr(self.remote_dir)
            pdfs = [e for e in entries if e.filename.lower().endswith(".pdf")]
            items = []
            for entry in pdfs[: max(1, int(limit))]:
                items.append(
                    {
                        "id": entry.filename,
                        "name": entry.filename,
                        "size": int(getattr(entry, "st_size", 0) or 0),
                        "source": "sftp",
                    }
                )
            return items
        finally:
            sftp.close()
            transport.close()

    def fetch_bytes(self, item_id: str) -> bytes:
        safe = Path(item_id).name
        sftp, transport = self._client()
        try:
            remote = f"{self.remote_dir.rstrip('/')}/{safe}"
            with sftp.open(remote, "rb") as fh:
                return fh.read()
        finally:
            sftp.close()
            transport.close()

    def mark_processed(self, item_id: str) -> None:
        safe = Path(item_id).name
        sftp, transport = self._client()
        try:
            src = f"{self.remote_dir.rstrip('/')}/{safe}"
            dest_dir = self.processed_dir.rstrip("/")
            try:
                sftp.listdir(dest_dir)
            except IOError:
                sftp.mkdir(dest_dir)
            dest = f"{dest_dir}/{safe}"
            sftp.rename(src, dest)
        finally:
            sftp.close()
            transport.close()


_source = None


def get_document_source():
    global _source
    if _source is None:
        mode = (os.getenv("DOCUMENT_SOURCE") or "local").strip().lower()
        if mode == "sftp":
            _source = SftpDocumentSource()
        else:
            _source = LocalInboxDocumentSource()
    return _source


def health_check() -> Dict[str, Any]:
    mode = (os.getenv("DOCUMENT_SOURCE") or "local").strip().lower()
    try:
        src = get_document_source()
        pending = src.list_pending(limit=1)
        return {"ok": True, "mode": mode, "pending_sample": len(pending)}
    except Exception as exc:
        return {"ok": False, "mode": mode, "error": str(exc)[:200]}


def reset_document_source_for_tests() -> None:
    global _source
    _source = None
