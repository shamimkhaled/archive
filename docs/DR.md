# Disaster recovery & hardening runbook

Operational guide for Phase 3 resilience of the Document Archiver / bank integrator stack.

---

## 1. Health probes

| Probe | Path | Expect |
|---|---|---|
| Liveness | `GET /healthz` | `{"status":"ok"}` — process up |
| Readiness | `GET /readyz` | `status: ready` (or `degraded` with warning) — Postgres reachable; S3 probed when `STORAGE_BACKEND=s3` |
| Integrations | `GET /api/v1/integrations/health` | Scope `integrations:health`; adapter + circuit status |

Load balancers should fail traffic on non-2xx `/readyz` (not `/healthz` alone).

---

## 2. Multi-queue workers

When `ENABLE_JOB_QUEUE=1` and `ENABLE_MULTI_QUEUE=1` (default on):

| Queue env | Job types |
|---|---|
| `bcp:jobs:ingest` | `chunk_index`, `connector_pull` |
| `bcp:jobs:meeting` | `meeting_transcribe`, `meeting_chunk_stt`, `meeting_minutes` |
| `bcp:jobs:notify` | `webhook_dispatch`, `audit_export` |
| `bcp:jobs` | fallback / unknown |

Specialize workers:

```bash
# Meeting-focused replica
JOB_WORKER_QUEUES=meeting ENABLE_JOB_QUEUE=1 PYTHONPATH=src python -m bcp_project.jobs.worker

# Ingest + default
JOB_WORKER_QUEUES=ingest ENABLE_JOB_QUEUE=1 PYTHONPATH=src python -m bcp_project.jobs.worker

# All (default)
JOB_WORKER_QUEUES=all ENABLE_JOB_QUEUE=1 PYTHONPATH=src python -m bcp_project.jobs.worker
```

Failed jobs retry up to `JOB_MAX_ATTEMPTS` then land in `JOB_DEAD_LETTER_KEY` (`bcp:jobs:dead`).

---

## 3. Failure modes (fail-open vs fail-closed)

| Subsystem | On failure |
|---|---|
| Redis cache / locks | Fail-open — degrade UX, never grant document access |
| Job enqueue | Fall back to in-process `BackgroundTasks` where implemented |
| Qdrant / LLM | Search/Ask return errors; do not invent answers |
| CBS / Loan / HR ports | Circuit breaker opens; `/integrations/health` reports open |
| Object storage | `/readyz` → degraded; PDF view/download may 404 |

**Access control is fail-closed.** Redis/cache outages must not bypass grants.

---

## 4. Chaos / drill checklist (lab)

1. Stop Postgres → `/readyz` returns 503; `/healthz` still 200.  
2. Stop Redis → search cache miss; jobs stop enqueueing; API remains authz-correct.  
3. Block Qdrant → hybrid search degrades; Ask Sonali refuses or 502 on LLM path.  
4. Open customer circuit (force failures) → integrations health shows open; other adapters unaffected.  
5. Kill meeting worker only → ingest workers still drain `bcp:jobs:ingest`.  
6. Inspect dead-letter list length after intentional poison jobs.

---

## 5. Recovery order

1. Restore Postgres + verify `/readyz`.  
2. Restore Redis; clear poison jobs from dead-letter after triage.  
3. Restore Qdrant; reindex if collections lost (`scripts` reindex helpers).  
4. Restore S3/R2 connectivity; confirm `probe_s3` in `/readyz`.  
5. Restart API + workers with matching `ENABLE_JOB_QUEUE` / `JOB_WORKER_QUEUES`.  
6. Spot-check: login, archive search, Ask Sonali, meeting pack PDF.

---

## 6. Backup expectations (bank-owned)

- **Postgres:** PITR / daily snapshots (Neon or bank DBA).  
- **Object store:** versioning + cross-region replication per bank policy.  
- **Qdrant:** snapshot collections or rebuild from Postgres + PDF OCR pipeline.  
- **Secrets:** rotate API keys / JWT secret via bank vault; revoke `service_clients` rows on compromise.

---

## Related

- [DEPLOYMENT.md](./DEPLOYMENT.md)  
- [INTEGRATION.md](./INTEGRATION.md)  
- [ARCHITECTURE.md](./ARCHITECTURE.md)  
