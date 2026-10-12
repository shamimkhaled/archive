"""Streaming authenticated encryption for backup files.

File layout::

    MAGIC | u32 header length | header JSON | record*
    record = u32 ciphertext length | 12-byte nonce | ciphertext (with 16-byte GCM tag)

Every record is AES-256-GCM with associated data = SHA-256(MAGIC + header) || u64 index || u8 last.
Binding the header digest, the record index, and the "last record" flag into each tag means a
changed header, a reordered or dropped record, or a file cut short all fail to decrypt. An empty
input still produces one (empty) last record, so a zero-length file is never accepted.

The key is 32 random bytes, base64 in ``BACKUP_ENCRYPTION_KEY``. It lives in the bank's vault (or
an HSM-wrapped secret), never next to the backups; ``key_id`` in the header says which key a file
needs without revealing it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
from datetime import datetime, timezone
from typing import Any, BinaryIO, Dict, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"BCPBAK\x01\n"
CHUNK_SIZE = 4 * 1024 * 1024
_LEN = struct.Struct(">I")
_AAD = struct.Struct(">QB")
NONCE_BYTES = 12
TAG_BYTES = 16
MAX_HEADER = 64 * 1024


class BackupIntegrityError(Exception):
    """The file was altered, truncated, reordered, or needs a different key."""


def generate_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def parse_key(value: str) -> bytes:
    text = (value or "").strip()
    text += "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(text) if ("-" in text or "_" in text) else base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("BACKUP_ENCRYPTION_KEY must be base64 for 32 random bytes") from exc
    if len(raw) != 32:
        raise ValueError(f"BACKUP_ENCRYPTION_KEY must decode to 32 bytes, got {len(raw)}")
    return raw


def load_key(env: str = "BACKUP_ENCRYPTION_KEY") -> bytes:
    value = os.getenv(env)
    if not value:
        raise RuntimeError(f"{env} is not set. Generate one with: python scripts/dr_backup.py keygen")
    return parse_key(value)


def key_id(key: bytes) -> str:
    return hashlib.sha256(b"bcp-backup-key-id:" + key).hexdigest()[:16]


def _read_exact(src: BinaryIO, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = src.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def encrypt_stream(
    src: BinaryIO, dst: BinaryIO, key: bytes, *, name: str, chunk_size: int = CHUNK_SIZE, extra: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Encrypt ``src`` into ``dst``; returns plaintext size and SHA-256 for the manifest."""
    header = json.dumps(
        {
            "v": 1,
            "alg": "AES-256-GCM",
            "chunk": chunk_size,
            "key_id": key_id(key),
            "name": name,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **(extra or {}),
        },
        sort_keys=True,
    ).encode()
    bound = hashlib.sha256(MAGIC + header).digest()
    aead = AESGCM(key)
    digest = hashlib.sha256()
    out_digest = hashlib.sha256()

    def emit(data: bytes) -> None:
        dst.write(data)
        out_digest.update(data)

    emit(MAGIC + _LEN.pack(len(header)) + header)
    size = index = 0
    current = _read_exact(src, chunk_size)
    while True:
        following = _read_exact(src, chunk_size) if current else b""
        last = not following
        digest.update(current)
        size += len(current)
        nonce = os.urandom(NONCE_BYTES)
        sealed = aead.encrypt(nonce, current, bound + _AAD.pack(index, int(last)))
        emit(_LEN.pack(len(sealed)) + nonce + sealed)
        index += 1
        if last:
            break
        current = following
    return {"bytes": size, "sha256": digest.hexdigest(), "records": index, "ciphertext_sha256": out_digest.hexdigest()}


def read_header(src: BinaryIO) -> Dict[str, Any]:
    if _read_exact(src, len(MAGIC)) != MAGIC:
        raise BackupIntegrityError("not a BCP backup file (bad magic)")
    raw_len = _read_exact(src, _LEN.size)
    if len(raw_len) != _LEN.size:
        raise BackupIntegrityError("truncated header")
    (length,) = _LEN.unpack(raw_len)
    if length > MAX_HEADER:
        raise BackupIntegrityError("header too large")
    header = _read_exact(src, length)
    if len(header) != length:
        raise BackupIntegrityError("truncated header")
    try:
        parsed = json.loads(header)
    except ValueError as exc:
        raise BackupIntegrityError("header is not JSON") from exc
    parsed["_raw"] = header
    return parsed


def decrypt_stream(src: BinaryIO, dst: Optional[BinaryIO], key: bytes) -> Dict[str, Any]:
    """Decrypt and authenticate; with ``dst=None`` only verifies. Raises BackupIntegrityError."""
    header = read_header(src)
    raw = header.pop("_raw")
    if header.get("key_id") != key_id(key):
        raise BackupIntegrityError(f"file needs key {header.get('key_id')}, this key is {key_id(key)}")
    bound = hashlib.sha256(MAGIC + raw).digest()
    aead = AESGCM(key)
    digest = hashlib.sha256()
    size = index = 0
    max_record = int(header.get("chunk") or CHUNK_SIZE) + TAG_BYTES

    def next_record():
        raw_len = _read_exact(src, _LEN.size)
        if not raw_len:
            return None
        if len(raw_len) != _LEN.size:
            raise BackupIntegrityError("truncated record length")
        (length,) = _LEN.unpack(raw_len)
        if length < TAG_BYTES or length > max_record:
            raise BackupIntegrityError(f"record {index} has an impossible length")
        body = _read_exact(src, NONCE_BYTES + length)
        if len(body) != NONCE_BYTES + length:
            raise BackupIntegrityError(f"record {index} is truncated")
        return body

    record = next_record()
    if record is None:
        raise BackupIntegrityError("no records (file truncated after header)")
    while record is not None:
        following = next_record()
        last = following is None
        try:
            plain = aead.decrypt(record[:NONCE_BYTES], record[NONCE_BYTES:], bound + _AAD.pack(index, int(last)))
        except InvalidTag as exc:
            reason = "file truncated or records reordered" if last else "record altered or out of order"
            raise BackupIntegrityError(f"record {index}: authentication failed ({reason})") from exc
        digest.update(plain)
        size += len(plain)
        if dst is not None:
            dst.write(plain)
        index += 1
        record = following
    return {"bytes": size, "sha256": digest.hexdigest(), "records": index, "header": header}


# --- manifest ----------------------------------------------------------------------------------


def _manifest_key(key: bytes) -> bytes:
    return hmac.new(key, b"bcp-backup-manifest", hashlib.sha256).digest()


def canonical(manifest: Dict[str, Any]) -> bytes:
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()


def sign_manifest(manifest: Dict[str, Any], key: bytes) -> Dict[str, Any]:
    signed = dict(manifest)
    signed["signature"] = {"alg": "HMAC-SHA256", "key_id": key_id(key), "value": hmac.new(_manifest_key(key), canonical(manifest), hashlib.sha256).hexdigest()}
    return signed


def verify_manifest(manifest: Dict[str, Any], key: bytes) -> None:
    sig = manifest.get("signature") or {}
    expected = hmac.new(_manifest_key(key), canonical(manifest), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(sig.get("value") or ""), expected):
        raise BackupIntegrityError("manifest signature does not match (edited manifest or wrong key)")
