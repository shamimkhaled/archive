#!/usr/bin/env python3
"""Restore drill: rebuild the archive from an encrypted backup into a separate database and prove it.

Steps, each timed and written to the drill record:

  1. Verify the manifest signature and authenticate every encrypted file
  2. Decrypt the Postgres dump and pg_restore it into ``--target-url`` (never the source database)
  3. Row counts per table must equal the counts taken in the backup's snapshot
  4. The audit hash chain must verify on the restored copy and end at the backed-up tip
  5. Original files (from the backup or the live store) must hash to the restored version rows
  6. Optionally restore the Qdrant snapshots into ``--qdrant-url`` and compare point counts

RPO is the age of the backup at the moment of the failure being simulated (data written after the
snapshot is lost); RTO is the wall time from the start of the drill to a verified restore.

  PYTHONPATH=src python scripts/dr_restore.py --backup var/dr/20261012T020000Z \\
      --target-url postgresql://drill@127.0.0.1:55432/archive_restore --operator "DBA on duty"
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bcp_project.config import load_environment, normalize_database_url  # noqa: E402
from bcp_project.dr import crypto  # noqa: E402
from bcp_project.dr.sources import libpq_parts, object_location, read_object, redact, same_database, sha256_hex  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))
from dr_backup import verify_backup  # noqa: E402


class Drill:
    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.started_at = datetime.now(timezone.utc)
        self.steps: List[Dict[str, Any]] = []

    def step(self, name: str, ok: bool, started: float, **detail: Any) -> None:
        seconds = round(time.perf_counter() - started, 2)
        self.steps.append({"step": name, "ok": ok, "seconds": seconds, **detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} ({seconds}s) {json.dumps(detail, default=str)[:300]}")

    @property
    def ok(self) -> bool:
        return all(s["ok"] for s in self.steps)


def pg_restore(dump: Path, target_url: str, strict: bool) -> Dict[str, Any]:
    dsn, env = libpq_parts(target_url)
    cmd = ["pg_restore", "--no-owner", "--no-acl", "--clean", "--if-exists", "--dbname", dsn]
    if strict:
        cmd.append("--exit-on-error")
    proc = subprocess.run(cmd + [str(dump)], capture_output=True, text=True, env={**os.environ, **env})
    errors = [line for line in proc.stderr.splitlines() if "error" in line.lower()]
    return {"returncode": proc.returncode, "errors": len(errors), "first_errors": errors[:5]}


async def target_facts(target_url: str, tables: List[str]) -> Dict[str, Any]:
    import asyncpg

    dsn, env = libpq_parts(target_url)
    conn = await asyncpg.connect(dsn, password=env.get("PGPASSWORD"))
    try:
        counts = {t: await conn.fetchval(f'select count(*) from "{t}"') for t in tables}
        versions = [dict(r) for r in await conn.fetch("select doc_id, version, sha256, file_location, original_location from document_versions")]
    finally:
        await conn.close()
    return {"tables": counts, "versions": versions}


async def verify_restored_chain(target_url: str) -> Dict[str, Any]:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from bcp_project.services.audit_chain import verify_chain

    engine = create_async_engine(normalize_database_url(target_url), pool_size=1, max_overflow=0)
    try:
        async with AsyncSession(engine) as db:
            return await verify_chain(db)
    finally:
        await engine.dispose()


def restore_qdrant(backup: Path, manifest: Dict[str, Any], key: bytes, url: str, api_key: Optional[str]) -> Dict[str, Any]:
    import httpx

    headers = {"api-key": api_key} if api_key else {}
    results = []
    with httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=900) as client:
        for c in (manifest.get("qdrant") or {}).get("collections", []):
            with tempfile.TemporaryFile() as tmp:
                with open(backup / c["file"], "rb") as fh:
                    crypto.decrypt_stream(fh, tmp, key)
                tmp.seek(0)
                client.post(
                    f"/collections/{c['collection']}/snapshots/upload",
                    params={"priority": "snapshot", "wait": "true"},
                    files={"snapshot": (f"{c['collection']}.snapshot", tmp)},
                ).raise_for_status()
            points = client.get(f"/collections/{c['collection']}").json()["result"].get("points_count")
            results.append({"collection": c["collection"], "expected_points": c.get("points"), "restored_points": points, "ok": points == c.get("points")})
        for alias in (manifest.get("qdrant") or {}).get("aliases", []):
            client.post(
                "/collections/aliases",
                json={"actions": [{"create_alias": {"collection_name": alias["collection_name"], "alias_name": alias["alias_name"]}}]},
            )
    return {"collections": results}


def check_objects(
    backup: Path, key: bytes, manifest: Dict[str, Any], restored_versions: List[Dict[str, Any]], source: str, sample: int, live_check: bool
) -> Dict[str, Any]:
    """Hash each original against its restored version row.

    Versions uploaded before hashes were recorded have no SHA-256 on the row; for those the copy
    in the backup is compared with the live original instead (``live_check``), which still proves
    the backup bytes are intact, and the row is reported as needing scripts/backfill_version_hashes.py.
    """
    stored = {(e["doc_id"], e["version"]): e.get("stored_as") for e in (manifest.get("objects") or {}).get("entries", [])}
    rows = list(restored_versions)
    if sample and len(rows) > sample:
        rows = random.Random(0).sample(rows, sample)
    matched = matched_live = 0
    bad: List[Dict[str, Any]] = []
    unavailable: List[Dict[str, Any]] = []
    unhashed: List[str] = []

    def live_digest(v: Dict[str, Any]) -> Optional[str]:
        location = object_location(v.get("file_location"), v.get("original_location"))
        return sha256_hex(read_object(location)) if location else None

    for v in rows:
        ref = {"doc_id": v["doc_id"], "version": v["version"]}
        try:
            enc = stored.get((v["doc_id"], v["version"]))
            backup_digest = None
            if source == "backup" and enc:
                with open(backup / enc, "rb") as fh:
                    backup_digest = crypto.decrypt_stream(fh, None, key)["sha256"]
            if v.get("sha256"):
                digest = backup_digest or live_digest(v)
                if digest == v["sha256"]:
                    matched += 1
                else:
                    bad.append({**ref, "against": "recorded hash"})
                continue
            unhashed.append(f"{v['doc_id']}/v{v['version']}")
            if backup_digest and live_check:
                if backup_digest == live_digest(v):
                    matched_live += 1
                else:
                    bad.append({**ref, "against": "live original"})
        except Exception as exc:  # noqa: BLE001 - reported in the record
            unavailable.append({**ref, "error": type(exc).__name__})
    return {
        "checked": matched + matched_live + len(bad),
        "matched_recorded_hash": matched,
        "matched_live_original": matched_live,
        "mismatched": bad,
        "unavailable": unavailable,
        "versions_without_recorded_hash": len(unhashed),
    }


def write_record(record: Dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"restore-{record['drill_started_at'][:19].replace(':', '').replace('-', '')}"
    (directory / f"{stem}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    lines = [
        f"# Restore drill — {record['drill_started_at'][:16].replace('T', ' ')} UTC",
        "",
        f"**Result: {'PASS' if record['ok'] else 'FAIL'}**",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Operator | {record['operator']} |",
        f"| Backup | `{record['backup']['stamp']}` (key `{record['backup']['key_id']}`, commit `{record['backup']['commit']}`) |",
        f"| Source | `{record['backup']['source']}` (PostgreSQL {record['backup']['server_version']}) |",
        f"| Restore target | `{record['target']}` |",
        f"| Backup snapshot time | {record['backup']['snapshot_time']} |",
        f"| RPO (backup age at drill start) | {record['rpo_minutes']} minutes |",
        f"| RTO (drill start to verified restore) | {record['rto_minutes']} minutes |",
        f"| Rows restored | {record['rows_restored']:,} across {record['tables']} tables |",
        "",
        "| Step | Result | Seconds | Detail |",
        "|---|---|---|---|",
    ]
    for s in record["steps"]:
        detail = {k: v for k, v in s.items() if k not in ("step", "ok", "seconds")}
        text = json.dumps(detail, ensure_ascii=False, default=str)
        lines.append(f"| {s['step']} | {'PASS' if s['ok'] else 'FAIL'} | {s['seconds']} | `{text[:220]}` |")
    lines += ["", "Generated by `scripts/dr_restore.py`; the JSON next to this file has the full detail.", ""]
    path = directory / f"{stem}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--target-url", required=True, help="Empty or disposable database to restore into")
    parser.add_argument("--objects-from", choices=("backup", "live", "none"), default="backup")
    parser.add_argument("--sample", type=int, default=0, help="Hash-check N random versions (0 = all)")
    parser.add_argument("--no-live-check", action="store_true", help="Do not compare unhashed versions with the live store (air-gapped drill)")
    parser.add_argument("--qdrant-url", help="Disposable Qdrant to restore snapshots into")
    parser.add_argument("--qdrant-api-key", default=os.getenv("DRILL_QDRANT_API_KEY"))
    parser.add_argument("--lenient", action="store_true", help="Continue pg_restore past errors (records them)")
    parser.add_argument("--operator", default=os.getenv("DRILL_OPERATOR") or getpass.getuser())
    parser.add_argument("--record", type=Path, default=ROOT / "docs" / "evidence" / "drills")
    args = parser.parse_args()

    load_environment()
    if os.getenv("DATABASE_URL") and same_database(args.target_url, os.getenv("DATABASE_URL")):
        raise SystemExit("Refusing to restore over the live database (target equals DATABASE_URL).")

    key = crypto.load_key()
    drill = Drill()
    manifest = json.loads((args.backup / "manifest.json").read_text(encoding="utf-8"))

    t = time.perf_counter()
    try:
        verified = verify_backup(args.backup, key)
        drill.step("verify backup signature and encryption", True, t, files=verified["files_verified"])
    except (crypto.BackupIntegrityError, FileNotFoundError) as exc:
        drill.step("verify backup signature and encryption", False, t, error=str(exc))
        return finish(drill, manifest, args)

    facts: Dict[str, Any] = {}
    t = time.perf_counter()
    try:
        facts = restore_and_check(drill, manifest, args, key)
    except Exception as exc:  # noqa: BLE001 - a crashed drill is still a recorded (failed) drill
        drill.step("drill aborted", False, t, error=f"{type(exc).__name__}: {str(exc)[:300]}")
    return finish(drill, manifest, args, facts)


def restore_and_check(drill: Drill, manifest: Dict[str, Any], args, key: bytes) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="bcp-drill-") as tmp:
        dump = Path(tmp) / "postgres.dump"
        t = time.perf_counter()
        with open(args.backup / "postgres.dump.enc", "rb") as src, open(dump, "wb") as dst:
            info = crypto.decrypt_stream(src, dst, key)
        drill.step("decrypt database dump", info["sha256"] == manifest["postgres"]["dump"]["sha256"], t, bytes=info["bytes"])

        t = time.perf_counter()
        restored = pg_restore(dump, args.target_url, strict=not args.lenient)
        drill.step("pg_restore into target", restored["returncode"] == 0 or (args.lenient and restored["errors"] == 0), t, **restored)

    expected_counts: Dict[str, int] = manifest["postgres"]["tables"]
    t = time.perf_counter()
    facts = asyncio.run(target_facts(args.target_url, list(expected_counts)))
    diff = {k: {"backup": v, "restored": facts["tables"].get(k)} for k, v in expected_counts.items() if facts["tables"].get(k) != v}
    drill.step("row counts match the backup snapshot", not diff, t, tables=len(expected_counts), rows=sum(facts["tables"].values()), differences=diff)

    t = time.perf_counter()
    chain = asyncio.run(verify_restored_chain(args.target_url))
    want_tip = (manifest["postgres"].get("audit_chain") or {}).get("tip")
    tip_ok = not want_tip or chain.get("tip") == want_tip
    drill.step("audit hash chain verifies on the restored copy", bool(chain.get("ok")) and tip_ok, t, sealed=chain.get("sealed"), reason=chain.get("reason"), tip_matches_backup=tip_ok)

    if args.objects_from != "none":
        t = time.perf_counter()
        objects = check_objects(args.backup, key, manifest, facts["versions"], args.objects_from, args.sample, not args.no_live_check)
        passed = (not objects["mismatched"] and objects["checked"] > 0) or not facts["versions"]
        drill.step(f"original files hash to the restored version rows ({args.objects_from})", passed, t, **objects)

    if args.qdrant_url:
        t = time.perf_counter()
        q = restore_qdrant(args.backup, manifest, key, args.qdrant_url, args.qdrant_api_key)
        drill.step("Qdrant snapshots restored with matching point counts", all(c["ok"] for c in q["collections"]), t, **q)
    return facts


def finish(drill: Drill, manifest: Dict[str, Any], args, facts: Optional[Dict[str, Any]] = None) -> int:
    snapshot = manifest["postgres"].get("snapshot_time")
    snap_at = datetime.fromisoformat(snapshot) if snapshot else None
    if snap_at and snap_at.tzinfo is None:
        snap_at = snap_at.replace(tzinfo=timezone.utc)
    record = {
        "kind": "restore_drill",
        "ok": drill.ok,
        "operator": args.operator,
        "drill_started_at": drill.started_at.isoformat(timespec="seconds"),
        "target": redact(args.target_url),
        "backup": {
            "stamp": manifest.get("stamp"),
            "key_id": manifest.get("key_id"),
            "commit": (manifest.get("source") or {}).get("commit"),
            "source": (manifest.get("source") or {}).get("database"),
            "server_version": (manifest.get("source") or {}).get("server_version"),
            "snapshot_time": snapshot,
            "consistent_snapshot": manifest["postgres"].get("consistent_snapshot"),
        },
        "rpo_minutes": round((drill.started_at - snap_at).total_seconds() / 60, 1) if snap_at else None,
        "rto_minutes": round((time.perf_counter() - drill.started) / 60, 2),
        "rows_restored": sum((facts or {}).get("tables", {}).values()),
        "tables": len((facts or {}).get("tables", {})),
        "steps": drill.steps,
    }
    path = write_record(record, args.record)
    print(f"\n{'PASS' if drill.ok else 'FAIL'} — RTO {record['rto_minutes']} min, RPO {record['rpo_minutes']} min. Record: {path}")
    return 0 if drill.ok else 1


if __name__ == "__main__":
    sys.exit(main())
