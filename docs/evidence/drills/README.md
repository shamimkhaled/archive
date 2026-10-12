# DR drill records

Section 9 item covered here (with `docs/DR.md`): High Availability & DR. Each drill writes a
Markdown record and a JSON file with full detail into this folder. Records name hosts only by a
redacted form (for example `*.aws.neon.tech/neondb`); never commit credentials or full hostnames.

| Record | Drill | Environment | Result | RTO | RPO |
|---|---|---|---|---|---|
| [`restore-20261012T131526`](restore-20261012T131526.md) | Restore | Dev (Neon → local PostgreSQL 18) | FAIL: no originals could be checked (legacy versions had no recorded hash) | — | — |
| [`restore-20261012T131655`](restore-20261012T131655.md) | Restore | Dev (Neon → local PostgreSQL 18) | **PASS** | 0.95 min | 3.9 min |
| — | Failover | needs a standby environment (UAT pair or bank DR site) | not yet run | target ≤ 240 min | target ≤ 15 min |

The first restore record is kept on purpose. It found that 13 document versions uploaded before
write-once storage had no recorded SHA-256, so a restore could not prove their originals were intact.
The drill now compares those versions with the live original, and `scripts/backfill_version_hashes.py`
records hashes for them (awaiting the bank's approval to apply).

## Restore drill (quarterly)

1. Take or pick a backup (`scripts/dr_backup.py run`, or the latest nightly). Fetch the key from the
   vault into `BACKUP_ENCRYPTION_KEY` for this shell only.
2. Provision an empty target database (never the production database; the script refuses it).
3. Run:

   ```bash
   PYTHONPATH=src python scripts/dr_restore.py --backup var/dr/<stamp> \
     --target-url postgresql://drill@<restore-host>/archive_restore \
     --objects-from backup --operator "<name, team>"
   ```

   Add `--qdrant-url` to restore vector snapshots too; leave it out to prove that the index can be
   rebuilt from Postgres and the originals.
4. The drill passes when the backup signature, decryption, row counts, audit-chain tip and original
   hashes all check out. The record lands here; commit it.
5. Drop the target database.

## Failover drill (twice a year)

1. Agree the window with the business, and name the operator and the observer.
2. Prepare the commands that fail the primary and promote the standby. For example: drain the
   load balancer pool, promote the PostgreSQL standby, switch DNS. If they are manual, leave the
   flags out and the script waits for Enter.
3. Get a drill token for a test admin user (`DRILL_TOKEN`), not a real person's session.
4. Run:

   ```bash
   PYTHONPATH=src python scripts/dr_failover_drill.py \
     --primary https://archive.<bank-domain> --standby https://archive-dr.<bank-domain> \
     --public https://archive.<bank-domain> \
     --fail-cmd "<drain primary>" --switch-cmd "<promote standby>" \
     --token "$DRILL_TOKEN" --operator "<name, team>" --rto-target 240 --rpo-target 15
   ```

5. The script records each step's time, the time until the standby reported ready, smoke tests
   (health, login page, PWA shell, search, audit-chain verify), and how many sealed audit rows were
   lost against the baseline (the measured RPO).
6. Fail back during the same window, record it as a second drill, then commit both records.

## Record template (manual drills)

Use this when a drill cannot be scripted (for example a data-centre power test):

```markdown
# <Restore | Failover> drill — <date time UTC>

**Result: PASS | FAIL**

| Item | Value |
|---|---|
| Operator / observer | |
| Environment and sites | |
| Trigger (what was failed) | |
| Backup or replication point | |
| RTO target / measured | |
| RPO target / measured | |

| Step | Start (UTC) | End (UTC) | Result | Notes |
|---|---|---|---|---|
| Fail primary | | | | |
| Promote standby | | | | |
| Application ready | | | | |
| Smoke tests | | | | |
| Fail back | | | | |

Issues found and actions (owner, due date):
```
