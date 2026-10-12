"""Phase 10: encrypted backups, manifest signing, and the drill helpers."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest

from bcp_project.dr import crypto
from bcp_project.dr.sources import libpq_parts, object_location, redact, same_database

ROOT = Path(__file__).resolve().parents[1]
KEY = bytes(range(32))


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _encrypt(data: bytes, chunk: int = 16) -> bytes:
    out = io.BytesIO()
    crypto.encrypt_stream(io.BytesIO(data), out, KEY, name="t", chunk_size=chunk)
    return out.getvalue()


def _records(blob: bytes):
    """Split a file into (prefix, [records]) for tamper tests."""
    view = io.BytesIO(blob)
    crypto.read_header(view)
    prefix = blob[: view.tell()]
    records = []
    while True:
        raw = view.read(4)
        if not raw:
            break
        length = int.from_bytes(raw, "big")
        records.append(raw + view.read(12 + length))
    return prefix, records


@pytest.mark.parametrize("size", [0, 1, 16, 17, 100])
def test_round_trip(size):
    data = os.urandom(size)
    blob = _encrypt(data)
    out = io.BytesIO()
    info = crypto.decrypt_stream(io.BytesIO(blob), out, KEY)
    assert out.getvalue() == data
    assert info["bytes"] == size
    assert info["records"] == max(1, -(-size // 16))


def test_ciphertext_hides_plaintext_and_reports_digests():
    data = b"Board resolution 2026/14 " * 10
    out = io.BytesIO()
    info = crypto.encrypt_stream(io.BytesIO(data), out, KEY, name="t", chunk_size=32)
    assert b"Board resolution" not in out.getvalue()
    assert info["sha256"] == __import__("hashlib").sha256(data).hexdigest()
    assert info["ciphertext_sha256"] == __import__("hashlib").sha256(out.getvalue()).hexdigest()


def test_flipped_byte_is_detected():
    blob = bytearray(_encrypt(b"x" * 64))
    blob[-5] ^= 0x01
    with pytest.raises(crypto.BackupIntegrityError):
        crypto.decrypt_stream(io.BytesIO(bytes(blob)), None, KEY)


def test_truncation_at_a_record_boundary_is_detected():
    prefix, records = _records(_encrypt(b"y" * 64))
    assert len(records) == 4
    with pytest.raises(crypto.BackupIntegrityError, match="truncated"):
        crypto.decrypt_stream(io.BytesIO(prefix + b"".join(records[:-1])), None, KEY)
    with pytest.raises(crypto.BackupIntegrityError, match="no records"):
        crypto.decrypt_stream(io.BytesIO(prefix), None, KEY)


def test_truncation_inside_a_record_is_detected():
    blob = _encrypt(b"z" * 64)
    with pytest.raises(crypto.BackupIntegrityError, match="truncated"):
        crypto.decrypt_stream(io.BytesIO(blob[:-3]), None, KEY)


def test_reordered_or_dropped_records_are_detected():
    prefix, records = _records(_encrypt(bytes(range(64))))
    swapped = prefix + records[1] + records[0] + b"".join(records[2:])
    with pytest.raises(crypto.BackupIntegrityError):
        crypto.decrypt_stream(io.BytesIO(swapped), None, KEY)
    dropped = prefix + records[0] + b"".join(records[2:])
    with pytest.raises(crypto.BackupIntegrityError):
        crypto.decrypt_stream(io.BytesIO(dropped), None, KEY)


def test_header_edit_is_detected():
    blob = _encrypt(b"q" * 40)
    edited = blob.replace(b'"name": "t"', b'"name": "u"')
    assert edited != blob
    with pytest.raises(crypto.BackupIntegrityError):
        crypto.decrypt_stream(io.BytesIO(edited), None, KEY)


def test_wrong_key_is_named():
    blob = _encrypt(b"secret")
    with pytest.raises(crypto.BackupIntegrityError, match="needs key"):
        crypto.decrypt_stream(io.BytesIO(blob), None, bytes(32))


def test_not_a_backup():
    with pytest.raises(crypto.BackupIntegrityError, match="magic"):
        crypto.decrypt_stream(io.BytesIO(b"PGDMP..."), None, KEY)


def test_key_parsing(monkeypatch):
    key = crypto.generate_key()
    assert len(crypto.parse_key(key)) == 32
    import base64

    assert crypto.parse_key(base64.urlsafe_b64encode(KEY).decode().rstrip("=")) == KEY
    with pytest.raises(ValueError):
        crypto.parse_key(base64.b64encode(b"short").decode())
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="keygen"):
        crypto.load_key()


def test_manifest_signature():
    manifest = crypto.sign_manifest({"stamp": "x", "postgres": {"tables": {"documents": 3}}}, KEY)
    crypto.verify_manifest(manifest, KEY)
    manifest["postgres"]["tables"]["documents"] = 2
    with pytest.raises(crypto.BackupIntegrityError):
        crypto.verify_manifest(manifest, KEY)


# --- connection helpers ---------------------------------------------------------------------------


def test_libpq_parts_keeps_the_password_out_of_the_url():
    dsn, env = libpq_parts("postgresql://archive:p%40ss@db.example.neon.tech/neondb?sslmode=require&channel_binding=require")
    assert dsn == "postgresql://archive@db.example.neon.tech/neondb?sslmode=require"
    assert env == {"PGPASSWORD": "p@ss"}
    assert redact("postgresql://archive:p%40ss@ep-abc-123.c-5.us-east-2.aws.neon.tech/neondb") == "*.aws.neon.tech/neondb"
    assert redact("postgresql://u:p@127.0.0.1:56543/archive_restore") == "127.0.0.1:56543/archive_restore"


def test_same_database_guard():
    live = "postgresql+asyncpg://u:p@db.example.com:5432/archive?ssl=require"
    assert same_database(live, "postgresql://other:x@db.example.com/archive")
    assert not same_database(live, "postgresql://u:p@127.0.0.1:55432/archive")
    assert not same_database(live, "postgresql://u:p@db.example.com/archive_restore")


def test_object_location_prefers_the_original_upload():
    assert object_location("s3://b/renditions/x.pdf", "s3://b/originals/x.jpg") == "s3://b/originals/x.jpg"
    assert object_location("s3://b/x.pdf", None) == "s3://b/x.pdf"
    assert object_location("", None) is None


# --- backup verification and drill records -------------------------------------------------------


def test_verify_backup_end_to_end(tmp_path, monkeypatch):
    dr_backup = _script("dr_backup")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", __import__("base64").b64encode(KEY).decode())
    dump = b"PGDMP fake dump " * 100
    original = b"%PDF-1.7 original"
    with open(tmp_path / "postgres.dump.enc", "wb") as fh:
        dump_info = crypto.encrypt_stream(io.BytesIO(dump), fh, KEY, name="postgres.dump")
    digest = __import__("hashlib").sha256(original).hexdigest()
    (tmp_path / "objects").mkdir()
    with open(tmp_path / "objects" / f"{digest}.enc", "wb") as fh:
        crypto.encrypt_stream(io.BytesIO(original), fh, KEY, name=f"objects/{digest}")
    manifest = {
        "format": dr_backup.FORMAT,
        "stamp": "20261012T000000Z",
        "key_id": crypto.key_id(KEY),
        "postgres": {"dump": {"file": "postgres.dump.enc", **dump_info}, "tables": {"documents": 1}},
        "qdrant": {"skipped": "test"},
        "objects": {"entries": [{"doc_id": "D1", "version": 1, "sha256": digest, "stored_as": f"objects/{digest}.enc"}]},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(crypto.sign_manifest(manifest, KEY)))
    assert dr_backup.verify_backup(tmp_path, KEY)["files_verified"] == 2

    blob = bytearray((tmp_path / "postgres.dump.enc").read_bytes())
    blob[-1] ^= 0xFF
    (tmp_path / "postgres.dump.enc").write_bytes(bytes(blob))
    with pytest.raises(crypto.BackupIntegrityError):
        dr_backup.verify_backup(tmp_path, KEY)


def test_failover_record(tmp_path):
    drill = _script("dr_failover_drill")
    assert drill.ready({"status": 200, "body": {"status": "ready"}})
    assert drill.ready({"status": 200, "body": {"status": "degraded"}})
    assert not drill.ready({"status": 503, "body": {"status": "not_ready"}})
    assert not drill.ready({"status": None})
    record = {
        "kind": "failover_drill",
        "scenario": "test",
        "operator": "ops",
        "drill_started_at": "2026-10-12T02:00:00+00:00",
        "primary": "https://a",
        "standby": "https://b",
        "public": None,
        "rto_minutes": 3.5,
        "rto_target_minutes": 240,
        "rpo_target_minutes": 15,
        "audit_rows_lost": 0,
        "ok": True,
        "events": [{"t_seconds": 0.0, "event": "failure injected", "ok": True, "command": "manual"}],
    }
    path = drill.write_record(record, tmp_path)
    text = path.read_text()
    assert "**Result: PASS**" in text and "3.5 minutes (target 240)" in text
    assert json.loads((tmp_path / "failover-20261012T020000.json").read_text())["rto_minutes"] == 3.5
