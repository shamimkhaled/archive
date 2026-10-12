# High availability, backup and disaster recovery

Runbook for keeping the archive available and getting it back after a failure. Drill records and
the procedure for running drills: [`docs/evidence/drills/`](evidence/drills/README.md).

**Targets (proposed, to be confirmed by the bank):** RTO 4 hours for the full service, RPO 15 minutes
for records and audit, 0 for originals once their upload is acknowledged.

---

## 1. Health probes

| Probe | Path | Expect |
|---|---|---|
| Liveness | `GET /healthz` | `{"status":"ok"}`: process up |
| Readiness | `GET /readyz` | `status: ready` (or `degraded` with a warning): Postgres reachable; S3 probed when `STORAGE_BACKEND=s3` |
| Integrations | `GET /api/v1/integrations/health` | Scope `integrations:health`; adapter and circuit status |
| Security | `GET /api/admin/security/status` | Unsealed audit rows, chain tip, SIEM cursor |
| Metrics | `GET /metrics` (web), `:9108/metrics` (workers, relay) | Bearer `METRICS_TOKEN`; scraped by Prometheus |

Load balancers route on `/readyz`, not `/healthz` alone. Prometheus alerts for each failure mode
below (service down, Redis Streams unreachable, outbox not draining, dead letters, replication lag,
disk filling) are in `deploy/prometheus/alerts.yml`; see
[`docs/evidence/monitoring.md`](evidence/monitoring.md).

---

## 2. Job processing (Redis Streams)

Jobs are written to the Postgres outbox in the same transaction as the record they belong to. The
`outbox-relay` publishes them to Redis Streams; worker services read with consumer groups.

| Stream | Job types | Service |
|---|---|---|
| `bcp:stream:ingest` | `document_ingest`, `chunk_index`, `connector_pull`, `board_briefing` | `worker-ingest` |
| `bcp:stream:meeting` | `meeting_transcribe`, `meeting_chunk_stt`, `meeting_minutes`, `meeting_recording_ingest` | `worker-meeting` |
| `bcp:stream:notify` | `webhook_dispatch`, `audit_export`, `siem_forward` | `worker-notify` |
| `bcp:stream:default` | anything else | any worker with `JOB_WORKER_QUEUES=all` |
| `bcp:stream:dead` | jobs that failed `JOB_MAX_ATTEMPTS` (3) times | inspect and retry at `/admin/queues` |

- A worker that dies mid-job leaves the entry pending; another consumer claims it after
  `JOB_CLAIM_IDLE_MS` (5 minutes).
- If Redis is down, uploads still succeed. Their jobs wait in the outbox and are published when
  Redis returns.
- Old list keys (`bcp:jobs:*`) are drained into the streams once at worker start.

---

## 3. Failure modes

| Subsystem down | Effect | Access control |
|---|---|---|
| Postgres | `/readyz` 503; the app is unavailable | Fail-closed |
| Redis | Search cache misses; rate limits fall back to per-replica counters; jobs queue in the outbox; scheduled sweeps pause | Unchanged (grants live in Postgres) |
| Qdrant | Search falls back to keyword results (flagged as degraded); Ask can only use the keyword hits' summary snippets, so it answers less often and refuses otherwise | Unchanged |
| Object storage | `/readyz` degraded; viewing and new uploads fail; search still works | Unchanged |
| AI provider | AI jobs retry and then go to the dead-letter stream; Ask returns 502; `restricted` content never fails over to the cloud | Unchanged |
| GPU host (on-prem) | As for the AI provider; admin may route non-restricted tasks to the cloud if policy allows (`docs/evidence/local-ai-cutover.md` section 8) | Unchanged |
| One web or worker replica | Other replicas carry on; pending jobs are claimed by other consumers | Unchanged |

**Access control is fail-closed.** No outage path grants access to a document.

---

## 4. High availability layout

| Component | Railway (current) | Bank data centre (target) |
|---|---|---|
| web | 2 replicas behind Railway's edge | 2–3 VMs behind the load balancer |
| workers | 1+ replica per stream; autoscaled on backlog (`scripts/autoscale_workers.py`) | 2 VMs; add replicas per stream |
| Postgres | Neon with point-in-time restore | Primary + synchronous standby (Patroni or the bank's HA tooling); asynchronous standby at the DR site |
| Redis | Managed, persistence on | Primary + replica + Sentinel, AOF |
| Qdrant | Qdrant Cloud | 1–3 nodes; collection replicas when 3 nodes |
| Objects | S3 / R2 with versioning and cross-region replication | MinIO erasure-coded; bucket replication to the DR site; Object Lock |
| AI | Cloud providers | GPU server per site |

---

## 5. Backups

`scripts/dr_backup.py` makes one self-checking, encrypted backup directory:

| File | Content |
|---|---|
| `manifest.json` | Signed (HMAC-SHA256 under a key derived from the backup key); row counts per table, audit-chain tip and version list taken in the same database snapshot as the dump; per-file SHA-256 of plaintext and ciphertext; no secrets |
| `postgres.dump.enc` | `pg_dump` custom format from an exported snapshot (consistent with the manifest) |
| `qdrant/<collection>.enc` | Qdrant snapshots (optional; the index can be rebuilt) |
| `objects/<sha256>.enc` | Originals, content-addressed (`--objects full`); with `--objects manifest` only hashes are recorded and the bucket's versioning and replication hold the bytes |

Encryption: AES-256-GCM in 4 MiB records, each bound to the file header, its position and an
end-of-file marker. Tampering, truncation, reordering or the wrong key are detected before any
plaintext is used.

```bash
PYTHONPATH=src python scripts/dr_backup.py keygen               # once; store in the vault
PYTHONPATH=src python scripts/dr_backup.py run --out /backup/archive --objects full \
  --upload s3://sbl-archive-dr/backups --keep 14
PYTHONPATH=src python scripts/dr_backup.py verify /backup/archive/<stamp>
```

Schedule: nightly `run` (objects `manifest` on nights when the bucket is replicated, `full` weekly),
and `verify` on the offsite copy after upload. Database PITR (Neon, or WAL archiving on premises)
covers the gap between nightly dumps to meet the 15-minute RPO.

**Key handling.**

- `BACKUP_ENCRYPTION_KEY` (32 bytes, base64) lives in the bank vault or HSM under dual control.
  It never sits next to the backups or in the repository.
- Each backup records the key's ID, so the right key can be found after a rotation.
- Keep old keys for as long as backups made with them are retained.
- Database passwords reach `pg_dump` and `pg_restore` through `PGPASSWORD`, never on the command
  line. Hostnames are redacted in manifests and drill records.

Retention: local `--keep N`. Offsite, a bucket lifecycle rule (recommended: 35 daily, 12 monthly,
7 yearly) with Object Lock on the backup bucket.

---

## 6. Restore

Full procedure and pass criteria: [`evidence/drills/README.md`](evidence/drills/README.md).

```bash
PYTHONPATH=src python scripts/dr_restore.py --backup /backup/archive/<stamp> \
  --target-url postgresql://restore@<host>/archive --objects-from backup --operator "<name>"
```

Recovery order after a site loss:

1. Postgres, from a standby, PITR or `dr_restore.py`. Check `/readyz`.
2. Object storage: the replica bucket, or the `objects/` from the backup.
3. Redis: empty is acceptable. The relay republishes unpublished outbox rows. Jobs that were in
   flight are re-run from `/admin/queues` or with `POST /api/documents/{id}/ingest/retry`.
4. Qdrant: snapshot restore, or rebuild with `scripts/reindex_embeddings.py` (or
   `scripts/reindex_chunks.py` from the originals).
5. Start web, relay and workers.
6. Verify: `POST /api/admin/audit/verify`, search, Ask, open a document, and a meeting pack.

---

## 7. Drills

| Drill | Frequency | Tool | Last result |
|---|---|---|---|
| Restore | Quarterly | `scripts/dr_restore.py` | PASS, 12 Oct 2026 (Dev): RTO 0.95 min, RPO 3.9 min, 1,746 rows, audit chain verified, 13/13 originals intact |
| Failover | Twice a year | `scripts/dr_failover_drill.py` | Not yet run: needs the UAT pair or the bank DR site |
| Component chaos (lab) | Each release | Manual | Stop Postgres (expect `/readyz` 503); stop Redis (uploads still accepted, jobs wait in the outbox); block Qdrant (keyword results only); kill a worker mid-job (another consumer claims it) |

**Finding from the first restore drill.** Thirteen versions uploaded before write-once storage had no
recorded SHA-256. Their originals now match the live store, but a backup alone could not prove it.
`scripts/backfill_version_hashes.py --apply` records their hashes, audited as
`version_hash_backfill`. It is pending the bank's approval because it writes to production.

---

## Related

- [`docs/evidence/architecture.md`](evidence/architecture.md)
- [`docs/RAILWAY.md`](RAILWAY.md)
- [`docs/SECURITY_OPERATIONS.md`](SECURITY_OPERATIONS.md)
- [`docs/evidence/storage-retention.md`](evidence/storage-retention.md)
- [`docs/evidence/monitoring.md`](evidence/monitoring.md)
