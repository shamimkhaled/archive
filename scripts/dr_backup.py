#!/usr/bin/env python3
"""Encrypted, verifiable backups of the archive (docs/DR.md section 6).

  keygen   Print a new BACKUP_ENCRYPTION_KEY (store it in the vault, not with the backups)
  run      Back up Postgres, Qdrant, and the stored originals into one directory
  verify   Check the manifest signature and decrypt-verify every file without writing plaintext

A backup directory holds ``manifest.json`` (signed, no secrets) and ``*.enc`` files:

  postgres.dump.enc          pg_dump custom format, taken from one exported snapshot so the row
                             counts and audit-chain tip in the manifest describe exactly this dump
  qdrant/<collection>.enc    Qdrant collection snapshots (optional: the index can be rebuilt)
  objects/<sha256>.enc       Original uploads, content-addressed (``--objects full``); with
                             ``--objects manifest`` only the list and hashes are recorded and the
                             bucket's own versioning / replication holds the bytes

  PYTHONPATH=src python scripts/dr_backup.py run --out var/dr --objects full --keep 14
  PYTHONPATH=src python scripts/dr_backup.py run --out var/dr --upload s3://sbl-archive-dr/backups
  PYTHONPATH=src python scripts/dr_backup.py verify var/dr/20261012T020000Z
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bcp_project.config import load_environment  # noqa: E402
from bcp_project.dr import crypto  # noqa: E402
from bcp_project.dr.sources import libpq_parts, object_location, read_object, redact, sha256_hex  # noqa: E402

FORMAT = "bcp-archive-backup/1"


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


# --- Postgres ------------------------------------------------------------------------------------


async def snapshot_facts(conn, tables: List[str]) -> Dict[str, Any]:
    existing = {
        r["tablename"] for r in await conn.fetch("select tablename from pg_tables where schemaname = current_schema()")
    }
    counts = {}
    for table in tables:
        if table in existing:
            counts[table] = await conn.fetchval(f'select count(*) from "{table}"')
    chain = None
    if "integration_cursors" in existing:
        row = await conn.fetchrow("select last_id, detail from integration_cursors where name = 'audit_chain'")
        if row:
            chain = {"tip": row["detail"], "last_sealed_id": row["last_id"]}
    latest = {}
    for table, column in (("audit_logs", "created_at"), ("documents", "created_at"), ("document_versions", "created_at")):
        if table in existing:
            value = await conn.fetchval(f'select max("{column}") from "{table}"')
            latest[table] = value.isoformat() if value else None
    versions = []
    if "document_versions" in existing:
        versions = [
            dict(r)
            for r in await conn.fetch(
                "select doc_id, version, sha256, file_location, original_location, size_bytes from document_versions order by doc_id, version"
            )
        ]
    return {
        "server_version": await conn.fetchval("show server_version"),
        "snapshot_time": (await conn.fetchval("select now()")).isoformat(),
        "tables": counts,
        "audit_chain": chain,
        "latest_rows": latest,
        "versions": versions,
    }


def run_pg_dump(dsn: str, env: Dict[str, str], snapshot: Optional[str], dst: Path, key: bytes) -> Dict[str, Any]:
    cmd = ["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--compress=6"]
    if snapshot:
        cmd.append(f"--snapshot={snapshot}")
    cmd.append(dsn)
    started = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, **env})
    with open(dst, "wb") as out:
        info = crypto.encrypt_stream(proc.stdout, out, key, name="postgres.dump")
    stderr = proc.stderr.read().decode(errors="replace")
    if proc.wait() != 0:
        dst.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump failed: {stderr.strip()[-800:]}")
    if info["bytes"] == 0:
        raise RuntimeError("pg_dump produced no output")
    return {**info, "seconds": round(time.perf_counter() - started, 1)}


async def backup_postgres(out: Path, key: bytes) -> Dict[str, Any]:
    import asyncpg

    from bcp_project.models import Base

    dsn, env = libpq_parts()
    tables = [t.name for t in Base.metadata.sorted_tables]
    conn = await asyncpg.connect(dsn, password=env.get("PGPASSWORD"))
    try:
        tx = conn.transaction(isolation="repeatable_read", readonly=True)
        await tx.start()
        try:
            snapshot = await conn.fetchval("select pg_export_snapshot()")
        except Exception:
            snapshot = None
        facts = await snapshot_facts(conn, tables)
        dump = await asyncio.to_thread(run_pg_dump, dsn, env, snapshot, out / "postgres.dump.enc", key)
        await tx.rollback()
    finally:
        await conn.close()
    facts["consistent_snapshot"] = bool(snapshot)
    return {"dump": {"file": "postgres.dump.enc", **dump}, **facts}


# --- Qdrant -------------------------------------------------------------------------------------


def backup_qdrant(out: Path, key: bytes) -> Dict[str, Any]:
    import httpx

    url = (os.getenv("QDRANT_URL") or "").rstrip("/")
    if not url:
        return {"skipped": "QDRANT_URL not set"}
    headers = {"api-key": os.environ["QDRANT_API_KEY"]} if os.getenv("QDRANT_API_KEY") else {}
    collections: List[Dict[str, Any]] = []
    with httpx.Client(base_url=url, headers=headers, timeout=600) as client:
        names = [c["name"] for c in client.get("/collections").raise_for_status().json()["result"]["collections"]]
        aliases = client.get("/aliases").json().get("result", {}).get("aliases", [])
        for name in names:
            started = time.perf_counter()
            snap = client.post(f"/collections/{name}/snapshots", params={"wait": "true"}).raise_for_status().json()["result"]["name"]
            dst = out / "qdrant" / f"{name}.enc"
            dst.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile() as tmp:
                with client.stream("GET", f"/collections/{name}/snapshots/{snap}") as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(1 << 20):
                        tmp.write(chunk)
                tmp.seek(0)
                with open(dst, "wb") as fh:
                    info = crypto.encrypt_stream(tmp, fh, key, name=f"qdrant/{name}")
            client.delete(f"/collections/{name}/snapshots/{snap}")
            points = client.get(f"/collections/{name}").json().get("result", {}).get("points_count")
            collections.append({"collection": name, "file": f"qdrant/{name}.enc", "points": points, "seconds": round(time.perf_counter() - started, 1), **info})
    return {"collections": collections, "aliases": aliases}


# --- objects ------------------------------------------------------------------------------------


def backup_objects(out: Path, key: bytes, versions: List[Dict[str, Any]], mode: str) -> Dict[str, Any]:
    if mode == "none":
        return {"mode": mode, "versions": len(versions), "unique_objects_stored": 0, "missing": [], "hash_mismatches": [], "entries": []}
    entries, missing, mismatched = [], [], []
    stored: set = set()
    started = time.perf_counter()
    for v in versions:
        location = object_location(v.get("file_location"), v.get("original_location"))
        entry = {"doc_id": v["doc_id"], "version": v["version"], "sha256": v.get("sha256"), "location": location}
        if mode == "full" and location:
            try:
                data = read_object(location)
            except Exception as exc:  # noqa: BLE001 - recorded, not fatal
                missing.append({**entry, "error": f"{type(exc).__name__}: {exc}"[:200]})
                entries.append({**entry, "status": "missing"})
                continue
            digest = sha256_hex(data)
            if v.get("sha256") and digest != v["sha256"]:
                mismatched.append({**entry, "actual_sha256": digest})
            if digest not in stored:
                dst = out / "objects" / f"{digest}.enc"
                dst.parent.mkdir(parents=True, exist_ok=True)
                with open(dst, "wb") as fh:
                    crypto.encrypt_stream(io.BytesIO(data), fh, key, name=f"objects/{digest}")
                stored.add(digest)
            entries.append({**entry, "stored_as": f"objects/{digest}.enc", "bytes": len(data), "status": "stored"})
        else:
            entries.append({**entry, "status": "listed"})
    return {
        "mode": mode,
        "versions": len(versions),
        "unique_objects_stored": len(stored),
        "missing": missing,
        "hash_mismatches": mismatched,
        "seconds": round(time.perf_counter() - started, 1),
        "entries": entries,
    }


# --- run / verify / prune -----------------------------------------------------------------------


def upload_dir(directory: Path, target: str) -> List[str]:
    from bcp_project.aws_utils import get_s3_client

    bucket, _, prefix = target[len("s3://") :].partition("/")
    client = get_s3_client()
    keys = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            key = "/".join(p for p in (prefix.strip("/"), directory.name, str(path.relative_to(directory))) if p)
            client.upload_file(str(path), bucket, key)
            keys.append(key)
    return keys


def prune(out: Path, keep: int) -> List[str]:
    stamps = sorted(p for p in out.iterdir() if p.is_dir() and (p / "manifest.json").exists())
    removed = []
    for old in stamps[:-keep] if keep > 0 else []:
        shutil.rmtree(old)
        removed.append(old.name)
    return removed


def cmd_run(args) -> int:
    key = crypto.load_key()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out / stamp
    out.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    print(f"backup {stamp} -> {out}")

    pg = asyncio.run(backup_postgres(out, key))
    versions = pg.pop("versions")
    print(f"  postgres: {pg['dump']['bytes']:,} bytes in {pg['dump']['seconds']}s, {sum(pg['tables'].values()):,} rows, snapshot={pg['consistent_snapshot']}")
    qdrant = backup_qdrant(out, key) if args.qdrant else {"skipped": "--no-qdrant (rebuild with scripts/reindex_embeddings.py)"}
    if qdrant.get("collections"):
        print(f"  qdrant: {len(qdrant['collections'])} collections")
    objects = backup_objects(out, key, versions, args.objects)
    print(f"  objects: {objects['versions']} versions, {objects['unique_objects_stored']} stored, {len(objects['missing'])} missing, {len(objects['hash_mismatches'])} hash mismatches")

    manifest = {
        "format": FORMAT,
        "stamp": stamp,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_seconds": round(time.perf_counter() - started, 1),
        "source": {"database": redact(None), "server_version": pg.pop("server_version"), "qdrant": (os.getenv("QDRANT_URL") or "").split("//")[-1].split(":")[0] or None, "commit": git_commit()},
        "key_id": crypto.key_id(key),
        "postgres": pg,
        "qdrant": qdrant,
        "objects": objects,
    }
    (out / "manifest.json").write_text(json.dumps(crypto.sign_manifest(manifest, key), indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(f"  manifest signed (key {manifest['key_id']}), total {manifest['duration_seconds']}s")
    if args.upload:
        keys = upload_dir(out, args.upload)
        print(f"  uploaded {len(keys)} files to {args.upload}")
    if args.keep:
        removed = prune(args.out, args.keep)
        if removed:
            print(f"  pruned {', '.join(removed)}")
    return 1 if objects["hash_mismatches"] else 0


def verify_backup(directory: Path, key: bytes) -> Dict[str, Any]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    crypto.verify_manifest(manifest, key)
    checked = []
    expected = {"postgres.dump.enc": manifest["postgres"]["dump"]["sha256"]}
    for c in (manifest.get("qdrant") or {}).get("collections", []):
        expected[c["file"]] = c["sha256"]
    for e in (manifest.get("objects") or {}).get("entries", []):
        if e.get("stored_as"):
            expected[e["stored_as"]] = e["stored_as"].split("/")[-1][: -len(".enc")]
    for rel, digest in expected.items():
        with open(directory / rel, "rb") as fh:
            info = crypto.decrypt_stream(fh, None, key)
        if info["sha256"] != digest:
            raise crypto.BackupIntegrityError(f"{rel}: plaintext SHA-256 differs from the manifest")
        checked.append(rel)
    return {"stamp": manifest["stamp"], "files_verified": len(checked), "key_id": manifest["key_id"]}


def cmd_verify(args) -> int:
    key = crypto.load_key()
    try:
        result = verify_backup(args.backup, key)
    except (crypto.BackupIntegrityError, FileNotFoundError) as exc:
        print(f"FAILED: {exc}")
        return 1
    print(json.dumps(result, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("keygen")
    p = sub.add_parser("run")
    p.add_argument("--out", type=Path, default=Path(os.getenv("BACKUP_DIR") or ROOT / "var" / "dr"))
    p.add_argument("--objects", choices=("none", "manifest", "full"), default=os.getenv("BACKUP_OBJECTS") or "manifest")
    p.add_argument("--no-qdrant", dest="qdrant", action="store_false")
    p.add_argument("--upload", default=os.getenv("BACKUP_UPLOAD_URL"), help="s3://bucket/prefix for the offsite copy")
    p.add_argument("--keep", type=int, default=int(os.getenv("BACKUP_KEEP") or 0), help="Keep the newest N local backups")
    p = sub.add_parser("verify")
    p.add_argument("backup", type=Path)
    args = parser.parse_args()

    if args.command == "keygen":
        print(crypto.generate_key())
        return 0
    load_environment()
    return cmd_run(args) if args.command == "run" else cmd_verify(args)


if __name__ == "__main__":
    sys.exit(main())
