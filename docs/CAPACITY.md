# Capacity, load testing, and scaling

This file is the source for section 7.2 of the post-demonstration report. Fill the result tables
from the JSON that the tools write. Do not use estimates.

## Targets

| Operation | Target | Measured by |
| --- | --- | --- |
| Archive search | p95 under 1.5 s | `loadtest/locustfile.py` (`search`), `scripts/bench_search.py` |
| Ask Sonali | p95 under 8 s | `loadtest/locustfile.py` (`ask`), `loadtest/k6_search_ask.js` |
| Upload accepted | p95 under 3 s (ingest runs in the background) | `loadtest/locustfile.py` (`upload`) |
| Open a document (first 256 KB, watermarked) | p95 under 2 s | `loadtest/locustfile.py` (`view first page`) |
| Speech to text | real-time factor 0.5 or lower on worker hardware | `scripts/bench_stt.py` |
| Error rate under load | under 1 % (429s from rate limits counted separately) | Locust report |

## How to run

1. **Prepare staging.**
   - Use the same Railway region and plan as production, and a copy of production-sized data (anonymised).
   - Create load-test accounts with the `board_member` role.
   - Raise `RATE_LIMIT_*` for those accounts' run, or keep the defaults and treat 429s as the limiter working.
2. **Search, view, Ask, and upload together.** Uploads create real documents, so run this against staging only.
   ```bash
   pip install -r requirements-optional.txt
   LOADTEST_USERS="lt1:pw,lt2:pw,lt3:pw" LOADTEST_DOC_IDS="BM-2024-001,CIR-2023-17" \
   LOADTEST_UPLOAD_PDF=samples/20-page-board-paper.pdf \
   locust -f loadtest/locustfile.py --host https://<staging> --headless -u 100 -r 10 -t 15m \
          --csv loadtest/results/u100
   ```
   - Repeat at 25, 50, 100 and 200 users.
   - The highest user count where every p95 target holds and errors stay under 1 % is the published capacity.
   - Each run writes `loadtest/results/capacity-*.json` and exits non-zero if a target is missed.
3. **Search alone, server-side** (no network or browser in the measurement):
   `PYTHONPATH=src python scripts/bench_search.py --rounds 3`, from a container in the Railway region.
4. **Speech to text:**
   `PYTHONPATH=src python scripts/bench_stt.py samples/meeting-10min.wav --language bn --rounds 2 --json loadtest/results/stt.json`
   Run it on the worker image and hardware. Run it again after the cutover to the bank's GPU server.

## Results

Run date, app commit, Railway plan, and data volume (documents, pages, chunks): _fill in_.

| Concurrent users | Search p95 | Ask p95 | Upload p95 | View p95 | Error rate | Requests/s | Within target |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 25 | | | | | | | |
| 50 | | | | | | | |
| 100 | | | | | | | |
| 200 | | | | | | | |

| Recording | Audio length | Processing time | Real-time factor | Provider and model |
| --- | --- | --- | --- | --- |
| | | | | |

Published capacity: **_N_ concurrent users** at the targets above, on _plan / replicas_.

## Tuning settings

| Setting | Default | Effect |
| --- | --- | --- |
| `SERVICE_ROLE` + `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | web 10+10, worker 4+4, pipeline 3+2 | Postgres connections per replica. Keep (replicas × (size + overflow)) below the Neon connection limit, or use the pooled Neon endpoint. |
| `QDRANT_SEARCH_HNSW_EF` | Qdrant default | Higher gives better recall but slower search |
| `QDRANT_HNSW_M`, `QDRANT_HNSW_EF_CONSTRUCT` | 16, 128 | Graph quality; applied to existing collections at startup |
| `EMBED_BATCH_SIZE` | 64 | Texts per embedding request during ingest |
| `QDRANT_UPSERT_BATCH` | 32 | Points per upsert |
| `PDF_STAMP_CACHE_MB` | 128 per replica | Watermarked copies kept for 5 minutes, so page-range requests are not re-stamped |
| `API_KEY_CACHE_SECONDS` | 300 | Service keys skip bcrypt while cached |

Database indexes for search filters and audit queries are in migration `0010`:
- audit by user, action, and resource;
- access requests by requester;
- documents by type and date;
- jobs by status.

## Horizontal scaling

- **Web:** stateless. Add replicas in Railway. Sessions are JWT cookies, rate limits and locks live in Redis, and the scheduler's jobs take Redis locks, so only one replica runs each periodic task.
- **Workers:** consumers in Redis Streams consumer groups. Any number of replicas can read the same stream. A job left unacknowledged by a replica that stops is reclaimed by another after `JOB_CLAIM_IDLE_MS`, and handlers are idempotent.
- **Queue-depth autoscaling:** `scripts/autoscale_workers.py` reads backlog for each stream (undelivered plus unacknowledged entries) and sets the worker service's replica count through the Railway API.

  | Pool | Stream | Replicas | Jobs per replica |
  | --- | --- | --- | --- |
  | ingest | `bcp:stream:ingest` | 1–4 | 20 |
  | meeting | `bcp:stream:meeting` | 1–3 | 2 (transcription is long) |

  - Scale-up is immediate. Scale-down waits for 5 quiet checks.
  - Pools can be overridden with `AUTOSCALE_POOLS` (JSON).
  - It runs in dry-run mode until `--apply` is given with `RAILWAY_API_TOKEN` and `RAILWAY_ENVIRONMENT_ID`.
  - Deploy it as a small separate Railway service with the start command `python scripts/autoscale_workers.py --apply`.
  - Check the dry-run decisions for a day before enabling `--apply`.
  - The Railway mutation used is `serviceInstanceUpdate(numReplicas)`. Confirm it against the account's API version and plan replica limits, because Railway changes this API between plan tiers.
- **Separate worker services per queue:** run the worker image with `JOB_WORKER_QUEUES=ingest` and `JOB_WORKER_QUEUES=meeting` as two services, so each scales on its own backlog.

## Related

- Sizing and cost per environment: [`docs/evidence/sizing.md`](evidence/sizing.md)
- AI accuracy and the OCR latency comparison: [`docs/evidence/benchmark-report.md`](evidence/benchmark-report.md)
