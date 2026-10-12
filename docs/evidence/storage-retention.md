# Storage design and retention

Section 9 item covered here: Storage & Retention Strategy.

## 1. What is stored where

| Data | Store | System of record? | Protection |
|---|---|---|---|
| Original uploads (PDF, image) and every later version | Object storage (S3, R2, MinIO), key `kind/doc_id/vN-<sha256 prefix>.ext` | yes | Write-once (`If-None-Match: *`; local files opened exclusive and set read-only), SHA-256 stored per version, optional S3 Object Lock (`S3_OBJECT_LOCK_DAYS`, `S3_OBJECT_LOCK_MODE` = GOVERNANCE or COMPLIANCE), bucket versioning |
| Document records, versions, extracted page text, confirmed metadata, grants, meetings, minutes, decisions, actions, briefings | PostgreSQL | yes | PITR (Neon) or WAL archiving + standby (bank), encrypted nightly dump |
| Audit log | PostgreSQL `audit_logs` | yes | Append-only triggers, SHA-256 hash chain, copy in the SIEM |
| Vectors and search payload | Qdrant | no — rebuilt from Postgres + originals | Snapshot in backups; `scripts/reindex_embeddings.py` rebuilds it |
| Job streams, locks, rate counters, cache | Redis | no | AOF persistence; queued jobs are also in the Postgres outbox until published |
| Watermarked copies for viewing | Web replica memory (`PDF_STAMP_CACHE_MB`, 5 minutes) | no | Never written to disk; `Cache-Control: no-store` |
| Model weights (on-prem) | Read-only volume on the GPU host | no | Pinned versions, checksums at download |

## 2. Integrity

- Every version's SHA-256 is computed at upload and stored on the version row; the object key
  carries a prefix of it. `GET /api/documents/{id}/versions/{n}/verify` rehashes the stored object.
- Restore drills recheck originals against the recorded hashes (`scripts/dr_restore.py`).
- 13 versions uploaded before Phase 4 have no recorded hash. `scripts/backfill_version_hashes.py`
  records the hash of the stored object, audited as `version_hash_backfill` with
  `basis=stored_object_at_backfill`, so nobody mistakes it for an upload-time hash. Running it with
  `--apply` needs the bank's approval.

## 3. Retention

| Data | Current behaviour | Setting |
|---|---|---|
| Originals, versions and document records | Kept permanently unless a retention rule covers the document type; then kept until the retention date and removed only through approved disposal (below) | `/admin/retention`; Object Lock days per bucket |
| Minutes, briefings, decisions | Kept permanently; papers linked to a Board meeting or briefing cannot be disposed of | — |
| Disposal tombstones | Kept permanently | — |
| Audit log | Kept indefinitely in Postgres (cannot be deleted by the app); forwarded to the SIEM | SIEM retention per bank policy |
| Published outbox rows | Purged after 7 days | `OUTBOX_RETENTION_DAYS` |
| Job streams | Trimmed to the newest ~100,000 entries per stream | `JOB_STREAM_MAXLEN` |
| Dead-letter entries | Kept until retried or cleared by an admin | — |
| Search cache | 1 hour | `SEARCH_CACHE_TTL_SECONDS` |
| Local backups | Newest N kept | `BACKUP_KEEP` |
| Offsite backups | Bucket lifecycle (recommend 35 daily, 12 monthly, 7 yearly) | object-store lifecycle rule |
| Application logs | Platform default (Railway) or bank log store | — |

### Records retention schedule, legal hold and disposal

Managed at `/admin/retention` (`services/retention.py`, `routers/admin_retention.py`). Records
officers are the `admin` and `board_secretary` roles; only `admin` changes the schedule.

1. **Retention rules per document type.** Each rule sets a period in years (or "permanent") and a
   trigger: document date or upload date, plus the authority it comes from (for example the policy
   clause). A document type with no rule is kept permanently. Each document's `retention_until` is
   recalculated when a rule changes and by a daily job (one replica at a time via a Redis lock).
   Rule changes are audited (`retention_rule_set`, `retention_rule_delete`).
2. **Legal hold** per document, with a required reason. A hold blocks disposal whatever the
   retention date says. Set and release are audited (`legal_hold_set`, `legal_hold_release`).
3. **Four-eyes disposal.** A records officer proposes disposal of a document that is past its
   retention date. A *different* records officer approves or rejects it; the proposer cannot approve
   their own request. Blockers are checked at proposal and again at approval: legal hold, no
   retention period, retention date not yet reached, part of a Board meeting record, or has a Board
   briefing.
4. **What disposal does**, in this order, so a failure leaves the record findable rather than
   orphaned: removes the document's vectors from both Qdrant collections; deletes every stored
   object version (on S3, every version and delete marker of each key, because a plain delete on a
   versioned bucket only hides the object); then deletes the document, version, page-text and
   access-request rows. It writes a tombstone (ID, title, type, dates, uploader, reference,
   sensitivity, each version's SHA-256 and size, proposer and approver) to `document_disposals`,
   audits `document_disposed`, and clears the search cache. If any step fails the disposal stays
   approved with the error recorded (`disposal_failed`) and can be retried.
5. **Object Lock is never bypassed.** Disposal does not use the governance-bypass header. A version
   still under Object Lock makes disposal fail and stay pending until the lock expires, so set
   `S3_OBJECT_LOCK_DAYS` no longer than the shortest retention period that will be disposed of,
   or accept that disposal waits for the lock.
6. **Disposed IDs are reserved.** A new upload cannot reuse the ID of a disposed document, so
   references in old minutes and audit rows never point at a different paper.
7. **The audit log is not disposed of.** Audit rows about a disposed document stay, and the hash
   chain stays intact.

The periods themselves must come from Sonali Bank's records management policy and Bangladesh Bank
requirements; they are an input to the POC, not a product default. Until rules are entered,
everything is kept.

## 4. Capacity

Originals dominate storage: ≈ 5 MB × documents × 1.5 for versions and image originals. Postgres stays
small (< 20 GB at 25,000 documents). Qdrant memory scales with vectors (`sizing.md` section 1).
Backups hold one copy of the database and, with `--objects full`, one encrypted copy of each
distinct original (content-addressed by SHA-256, so a file shared by several versions is stored
once per backup).
