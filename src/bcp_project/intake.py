"""Upload intake: file-signature validation, antivirus hook, hashing, write-once storage.

Accepted: PDF, PNG, JPEG, TIFF. The browser-supplied Content-Type is ignored; the
first bytes decide. Images are kept as uploaded (the original) and also rendered
to a PDF so the viewer, watermarking, and download paths stay PDF-only.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("bcp_project.intake")

PDF = "application/pdf"
PNG = "image/png"
JPEG = "image/jpeg"
TIFF = "image/tiff"
IMAGE_TYPES = frozenset({PNG, JPEG, TIFF})
EXTENSIONS = {PDF: ".pdf", PNG: ".png", JPEG: ".jpg", TIFF: ".tif"}


class IntakeRejected(ValueError):
    """The upload is not acceptable (type, size, or malware)."""


def max_upload_bytes() -> int:
    return int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024


def sniff_content_type(data: bytes) -> Optional[str]:
    head = data[:8]
    if head.startswith(b"%PDF-"):
        return PDF
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return TIFF
    return None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scan_for_malware(data: bytes) -> None:
    """``AV_SCAN=clamav`` streams the bytes to clamd (CLAMAV_HOST, CLAMAV_PORT).

    ``AV_SCAN_FAIL_OPEN=1`` accepts the file when clamd is unreachable; the default
    rejects it, which is the safer choice for a bank archive.
    """
    mode = (os.getenv("AV_SCAN") or "off").strip().lower()
    if mode in {"", "off", "none", "0"}:
        return
    if mode != "clamav":
        raise RuntimeError(f"Unknown AV_SCAN mode {mode!r}")
    host = os.getenv("CLAMAV_HOST", "clamav")
    port = int(os.getenv("CLAMAV_PORT", "3310"))
    try:
        verdict = _clamd_instream(host, port, data)
    except OSError as exc:
        if os.getenv("AV_SCAN_FAIL_OPEN", "0") == "1":
            logger.warning("clamd unreachable (%s); accepting upload because AV_SCAN_FAIL_OPEN=1", exc)
            return
        raise IntakeRejected("The virus scanner is unavailable; try again shortly") from exc
    if "FOUND" in verdict:
        raise IntakeRejected(f"The file was rejected by the virus scanner ({verdict.split(':', 1)[-1].strip()})")


def _clamd_instream(host: str, port: int, data: bytes, chunk: int = 64 * 1024) -> str:
    with socket.create_connection((host, port), timeout=float(os.getenv("CLAMAV_TIMEOUT", "30"))) as sock:
        sock.sendall(b"zINSTREAM\0")
        for start in range(0, len(data), chunk):
            piece = data[start : start + chunk]
            sock.sendall(struct.pack("!L", len(piece)) + piece)
        sock.sendall(struct.pack("!L", 0))
        reply = b""
        while not reply.endswith(b"\0"):
            part = sock.recv(4096)
            if not part:
                break
            reply += part
    return reply.rstrip(b"\0").decode(errors="replace")


@dataclass
class ValidatedUpload:
    data: bytes
    content_type: str
    sha256: str
    extension: str

    @property
    def is_image(self) -> bool:
        return self.content_type in IMAGE_TYPES


def validate_upload(data: bytes) -> ValidatedUpload:
    if not data:
        raise IntakeRejected("The file is empty")
    if len(data) > max_upload_bytes():
        raise IntakeRejected("File exceeds the upload limit")
    content_type = sniff_content_type(data)
    if content_type is None:
        raise IntakeRejected("Only PDF, PNG, JPEG, and TIFF files are accepted")
    if content_type in IMAGE_TYPES:
        _check_image(data)
    scan_for_malware(data)
    return ValidatedUpload(data=data, content_type=content_type, sha256=sha256_hex(data), extension=EXTENSIONS[content_type])


def _check_image(data: bytes) -> None:
    from PIL import Image

    max_pixels = int(os.getenv("MAX_IMAGE_PIXELS", str(120_000_000)))
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            img.verify()
    except Exception as exc:
        raise IntakeRejected("The image file is corrupt or unsupported") from exc
    if width * height > max_pixels:
        raise IntakeRejected("The image is too large to process")


def image_to_pdf(data: bytes) -> bytes:
    """Render every frame (multi-page TIFF included) into one PDF at its native resolution."""
    from PIL import Image, ImageSequence

    pages = []
    with Image.open(io.BytesIO(data)) as img:
        dpi = img.info.get("dpi", (300, 300))[0] or 300
        for frame in ImageSequence.Iterator(img):
            pages.append(frame.convert("RGB"))
    if not pages:
        raise IntakeRejected("The image has no pages")
    out = io.BytesIO()
    pages[0].save(out, "PDF", resolution=float(dpi), save_all=True, append_images=pages[1:])
    return out.getvalue()


def object_key(kind: str, doc_id: str, version: int, sha256: str, extension: str) -> str:
    safe_id = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in doc_id)
    return f"{kind}/{safe_id}/v{version}-{sha256[:16]}{extension}"


class WriteOnceViolation(RuntimeError):
    pass


def store_write_once(data: bytes, key: str, *, content_type: str, sha256: str) -> str:
    """Store bytes at ``key``; never overwrite. Returns the file_location for the record."""
    from .aws_utils import assert_production_storage_ready, use_local_storage

    assert_production_storage_ready()
    if use_local_storage():
        from .deps import UPLOAD_DIR

        path = (UPLOAD_DIR / key).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "xb") as fh:
                fh.write(data)
        except FileExistsError as exc:
            if sha256_hex(path.read_bytes()) == sha256:
                return key
            raise WriteOnceViolation(f"{key} already exists with different content") from exc
        try:
            os.chmod(path, 0o444)
        except OSError:
            pass
        return key
    return _put_s3_once(data, key, content_type=content_type, sha256=sha256)


def _put_s3_once(data: bytes, key: str, *, content_type: str, sha256: str) -> str:
    from datetime import datetime, timedelta, timezone

    from botocore.exceptions import ClientError

    from .aws_utils import get_s3_client

    bucket = os.getenv("AWS_BUCKET_NAME") or ""
    prefix = os.getenv("AWS_PREFIX", "pdfs").rstrip("/")
    remote_key = f"{prefix}/{key}"
    client = get_s3_client()
    params = {
        "Bucket": bucket,
        "Key": remote_key,
        "Body": data,
        "ContentType": content_type,
        "Metadata": {"sha256": sha256},
        "IfNoneMatch": "*",
    }
    lock_days = int(os.getenv("S3_OBJECT_LOCK_DAYS", "0") or 0)
    if lock_days > 0:
        params["ObjectLockMode"] = (os.getenv("S3_OBJECT_LOCK_MODE") or "GOVERNANCE").upper()
        params["ObjectLockRetainUntilDate"] = datetime.now(timezone.utc) + timedelta(days=lock_days)
    try:
        client.put_object(**params)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"PreconditionFailed", "412", "ConditionalRequestConflict"}:
            head = client.head_object(Bucket=bucket, Key=remote_key)
            if (head.get("Metadata") or {}).get("sha256") == sha256:
                return f"s3://{bucket}/{remote_key}"
            raise WriteOnceViolation(f"{remote_key} already exists with different content") from exc
        if code in {"NotImplemented", "InvalidArgument"} and "IfNoneMatch" in str(exc):
            params.pop("IfNoneMatch")
            try:
                client.head_object(Bucket=bucket, Key=remote_key)
                raise WriteOnceViolation(f"{remote_key} already exists")
            except ClientError:
                client.put_object(**params)
        else:
            raise RuntimeError(f"Failed to store {remote_key}: {exc}") from exc
    return f"s3://{bucket}/{remote_key}"


def delete_stored_object(location: str) -> int:
    """Permanently delete an original for approved disposal. Returns objects removed.

    Removes every S3 version and delete marker of the key (a plain DELETE on a
    versioned bucket only hides the object). Never bypasses Object Lock: a locked
    version makes this raise, and disposal stays pending until the lock expires.
    """
    if not location.startswith("s3://"):
        path = local_path_for(location)
        if path is None or not path.exists():
            return 0
        os.chmod(path, 0o644)
        path.unlink()
        return 1

    from botocore.exceptions import ClientError

    from .aws_utils import get_s3_client

    bucket, _, key = location[len("s3://") :].partition("/")
    client = get_s3_client()
    try:
        listing = client.list_object_versions(Bucket=bucket, Prefix=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"NotImplemented", "501"}:
            raise
        client.delete_object(Bucket=bucket, Key=key)
        return 1
    versions = [
        item["VersionId"]
        for group in ("Versions", "DeleteMarkers")
        for item in listing.get(group) or []
        if item.get("Key") == key
    ]
    for version_id in versions:
        client.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
    return len(versions)


def local_path_for(location: str) -> Optional[Path]:
    from .deps import UPLOAD_DIR

    if location.startswith("s3://"):
        return None
    path = Path(location)
    return path if path.is_absolute() else UPLOAD_DIR / location
