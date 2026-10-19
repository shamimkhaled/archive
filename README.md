# Sonali Bank Archive System

A secure, bilingual (English + Bangla) document archive and board-meeting platform for Sonali Bank PLC.
Documents are ingested with OCR, summarised, indexed for hybrid search, and served through a
watermarked viewer. Board meetings get recordings, AI-drafted minutes, and cited board-paper briefings,
all behind role-based access control and a hash-chained audit log.

**Stack:** FastAPI · SQLAlchemy 2 (async) + Alembic · PostgreSQL · Qdrant · Redis Streams · server-rendered Jinja2 pages (HTML, CSS, plain JavaScript) installable as a PWA · OpenAI-compatible AI port (cloud or bank-hosted GPU)

---

## Features

| Area | What it does |
|---|---|
| **Ingest** | PDF, PNG, JPEG, TIFF upload → text layer or OCR (`ben+eng`, line recovery, per-page confidence) → metadata suggestion → summary → chunk + summary vectors in Qdrant. Write-once originals with SHA-256 and optional S3 Object Lock. Optional ClamAV scan. |
| **OCR verification** | Low-confidence uploads go to a review queue (`/review`); four-eyes check (reviewer ≠ uploader) in production. |
| **Versioning** | New versions of a document are stored alongside the original, each with its own hash and review. |
| **Search** | Hybrid vector + keyword retrieval, RRF fusion, reranker, metadata filters; ACL is enforced *inside* the vector query. Redis cache with version bump on upload. |
| **Ask Sonali** | Question answering from cited chunks only, with page deep links. Mutating actions requested by AI agents pass a human-in-the-loop (HITL) approval gate. |
| **Secure viewing** | Request-gated access (`view_only` / `download`), server-stamped watermark on every PDF, range requests for the pdf.js viewer, one audit row per view. Downloads are admin-only. |
| **Board meetings** | Meetings, agendas, shared documents, invites, reminders (48h / 24h) with ICS + Google Calendar, digital attendance with signatures, printable sheet. |
| **Minutes** | Recording upload or live transcription → speech-to-text (Whisper) → optional diarization → draft minutes with decisions and actions → secretary edit → approval. Only approved minutes are indexed. |
| **Board-paper briefings** | Eight-heading cited briefing with map-reduce over long papers; decision register. |
| **Records management** | Retention schedules, legal hold, and disposal at `/admin/retention`. |
| **Identity** | Password + JWT cookie, TOTP MFA (QR code + recovery codes), AD/LDAP bind, OIDC SSO (PKCE) with group-to-role mapping. |
| **Security** | CSRF double-submit, security headers / CSP, `Cache-Control: no-store` on every page and API response, `Clear-Site-Data` on sign-out, login + API rate limits, append-only hash-chained audit log, SIEM forwarding (CEF/JSON over syslog or HTTP). |
| **Integration** | `/api/v1` with scoped service keys, MCP stdio server for AI agents, webhooks, connector port (local inbox / SFTP), minimal-field core-banking / loan / HR lookups. |
| **AI provider port** | One interface for chat, embeddings, rerank, and STT; provider and model chosen per task (OpenAI, OpenRouter, local vLLM/TEI/Whisper, or deterministic `stub`). Switchable at `/admin/ai`. |
| **Operations** | Prometheus metrics, Grafana dashboards, alert rules, background-task admin (`/admin/queues`), worker autoscaling, encrypted DR backups and restore / failover drills. |

---

## Roles and permissions

| Role | Upload | OCR review | Search / Ask | View archive | Download (watermarked) | Organise meetings | Approve access requests | Manage users / AI / queues |
|---|---|---|---|---|---|---|---|---|
| `admin` | Yes | Yes | Yes | Yes | **Yes (only role)** | Yes | Yes | Yes |
| `board_secretary` | No | Yes | Yes | Yes | No | Yes | Yes | No (retention: yes) |
| `uploader` | **Yes** | Yes | No | No | No | No | No | No |
| `board_member` | No | No | Yes | After an approved request | No (may request) | Invited only | No | No |

Role scopes for API and agent access live in `src/bcp_project/policy.py`; document grants in
`src/bcp_project/access_control.py`. The navigation each role sees is defined in
`src/bcp_project/ui/nav.py`.

### Access workflow

1. A board member searches the archive; results show their access status per document.
2. The member submits a **view_only** or **download** request with a purpose.
3. An admin or board secretary approves (with optional expiry) or denies.
4. Viewing streams a **server-watermarked** PDF (`/view/{doc_id}/file`). Download is a separate, audited, admin-only route (`/download/{doc_id}`).

---

## Architecture

```
Browser / installed PWA (Jinja pages)          Bank systems & AI agents (/api/v1, MCP)
                 └──────────────────────┬──────────────────────┘
                          web (FastAPI, N replicas)
              auth · policy · search · Ask · viewer · meetings · admin
                 │                │               │                 │
           PostgreSQL          Qdrant      Object storage       AI port
   records, versions, ACL,   summary +    (S3 / R2 / MinIO   (cloud or bank GPU,
   audit chain, outbox       chunk vectors  or local disk)    chosen per task)
                 │
           outbox-relay ──► Redis Streams ──► worker-ingest   (OCR, metadata, summary, embed)
                                         ├──► worker-meeting  (STT, diarization, minutes)
                                         └──► worker-notify   (email, in-app)
```

A job row is written in the same transaction as the business row (transactional outbox), then the relay
publishes it to a Redis stream. Workers use consumer groups with retries, a dead-letter stream, and admin
requeue.

### Web interface

The browser UI is rendered on the server with Jinja2; there is no JavaScript build step.

| Piece | Where |
|---|---|
| Page templates and shared macros (buttons, status labels, empty states, icons) | `src/bcp_project/templates/`, `templates/macros/` |
| Design tokens (colour themes, light/dark, text size, spacing, breakpoints) | `src/bcp_project/static/css/tokens.css` |
| Layout and components | `static/css/style.css`, `static/css/components.css` |
| Plain-language text for labels, statuses and error reasons | `src/bcp_project/ui/copy_en.json` (read by `ui/__init__.py`) |
| Role-based navigation | `src/bcp_project/ui/nav.py` |
| Partial page navigation, toasts, forms, viewer helpers | `static/js/app.js` |
| PDF viewer | `static/vendor/pdfjs/` (pdf.js, self-hosted) |
| Fonts (Inter, Noto Sans Bengali; self-hosted, no CDN) | `static/fonts/` |
| Service worker, manifest, offline page | `static/js/sw.js`, `static/manifest.webmanifest`, `static/offline.html` |

---

## Prerequisites

- Python 3.10+ (3.12 is used in the image and CI)
- Docker (PostgreSQL 16, Qdrant, Redis — see `docker-compose.yml`)
- Tesseract with `eng` + `ben` traineddata, and Poppler (OCR)
- An AI provider: OpenAI / OpenRouter key, a local OpenAI-compatible server, or the `stub` provider for offline work
- Optional: AWS S3 / R2 / MinIO, Resend (email; skipped if unset), ClamAV, `ffmpeg` + `faster-whisper` (meeting STT)

---

## Local development

### 1. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# optional: pip install -r requirements-worker.txt    # speech-to-text (faster-whisper)
# optional: pip install -r requirements-optional.txt  # LDAP sign-in, locust load tests

cp .env.example .env
```

In `.env`, set at least `JWT_SECRET_KEY`, `DATABASE_URL`, and your AI provider keys. For a fully local
setup use `STORAGE_BACKEND=local` (files go to `UPLOAD_DIR`). Every setting, including the AI
provider and per-task model settings, is documented in `.env.example`.

### 2. Start the data services and migrate

```bash
docker compose up -d postgres qdrant redis      # Postgres on :5433, Redis on :6380, Qdrant on :6333
PYTHONPATH=src python -m bcp_project.migrate    # Alembic upgrade to head (also runs on app start unless AUTO_MIGRATE=0)
PYTHONPATH=src python scripts/create_admin_user.py --username admin
```

### 3. Run the app

```bash
PYTHONPATH=src uvicorn bcp_project.main_api:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000/login. Templates, CSS, and JavaScript are served directly from
`src/bcp_project/templates` and `src/bcp_project/static`, so edits show on the next page load.
Interactive API docs are at `/docs` (disabled when `APP_ENV=production`).

### 4. Background workers (optional)

Without the queue, uploads are processed in-process. To run the durable pipeline locally:

```bash
# API with the queue enabled (OUTBOX_RELAY_MODE=inline lets the web process relay the outbox)
ENABLE_JOB_QUEUE=1 PYTHONPATH=src uvicorn bcp_project.main_api:app --reload --port 8000

# Worker — all queues, or one of: ingest | meeting | notify | default
ENABLE_JOB_QUEUE=1 JOB_WORKER_QUEUES=all PYTHONPATH=src python -m bcp_project.jobs.worker

# Separate relay process (when OUTBOX_RELAY_MODE=process)
PYTHONPATH=src python -m bcp_project.jobs.relay
```

### Full pipeline in Docker

Runs the same layout as production (web, outbox relay, one worker per queue):

```bash
docker compose --profile pipeline up -d --build    # web on http://127.0.0.1:8080
```

---

## Testing and quality checks

```bash
# Unit and page tests (includes template rendering and UI design-system checks)
APP_ENV=test JWT_SECRET_KEY=test-only-secret PYTHONPATH=src pytest -q

# Bilingual retrieval evaluation (recall / MRR / grounding thresholds)
APP_ENV=test JWT_SECRET_KEY=test-only-secret PYTHONPATH=src python scripts/eval_retrieval.py

# Screenshots of the main screens per role at phone, tablet and desktop widths, light and dark
# (demo data on a throw-away SQLite database; needs: pip install playwright && playwright install chromium)
PYTHONPATH=src python scripts/ui_screenshots.py
```

CI (`.github/workflows/ci.yml`) also runs: Ruff, migrations on a fresh database (idempotent, single head),
an append-only audit check, `pip-audit` + Bandit, an OWASP ZAP baseline scan, Prometheus / Alertmanager
config and alert-rule tests, Docker builds for each role with a Trivy scan, and a hardened-runtime check
(non-root, read-only filesystem, no capabilities).

---

## Deployment

One image (`Dockerfile`) serves every role; the `SERVICE_ROLE` build argument selects `web`, `pipeline`,
or `worker` (adds Whisper + ffmpeg). The entrypoint fixes volume ownership, optionally runs migrations
(`MIGRATE_ON_START=1`, guarded by a Postgres advisory lock), and drops to a non-root user.

| Target | How |
|---|---|
| **Railway** (current) | Five services defined in `.railway/railway.ts`: `web` (migrates pre-deploy), `outbox-relay`, `worker-ingest`, `worker-meeting`, `worker-notify`. |
| **Bank data centre** (target) | Same services behind a WAF/load balancer; Postgres primary + standby, MinIO with object lock, and an isolated AI zone (`deploy/onprem/docker-compose.gpu.yml`: vLLM, TEI, Whisper behind a bearer-token gateway with no egress). Hardening overlay: `deploy/onprem/docker-compose.hardening.yml`. |
| **Monitoring** | `deploy/observability.compose.yml` — Prometheus, Alertmanager, blackbox probes, Grafana dashboard. Copy `deploy/prometheus/alertmanager.example.yml` to `alertmanager.yml` and create `deploy/prometheus/metrics_token` (both git-ignored). |
| **Disaster recovery** | `scripts/dr_backup.py`, `scripts/dr_restore.py`, `scripts/dr_failover_drill.py` (encrypted backup, restore drill, failover drill with smoke checks). |

Health endpoints: `/healthz` (liveness), `/readyz` (readiness), `/metrics` (Prometheus; requires a bearer token when `METRICS_TOKEN` is set).

---

## Project layout

```
src/bcp_project/
  main_api.py            App factory: middleware, lifespan, routers, static mounts
  config.py  db.py       Environment loading; async engine and sessions
  models.py  schemas.py  ORM models; request/response schemas
  migrate.py             Alembic upgrade-to-head with an advisory lock
  deps.py                Shared auth / role / document helpers and the Jinja environment
  routers/               HTTP surface: auth, pages, documents, ingest, review, ask, meetings,
                         board, meeting_minutes, briefings, access, admin*, api_v1, health
  services/              Business logic: ingest, search, ask_sonali, hitl, minutes, briefing,
                         decision_register, document_versions, retention, audit_chain, siem,
                         connector_sync, customer_query, external_identity
  ai/                    AI provider port: providers, per-task routing, egress policy
  jobs/                  Outbox, relay, Redis Streams queue, worker, handlers, locks, autoscale
  ports/                 Adapter seams: storage, vector, email, identity, document source,
                         customer / loan / HR, events, circuit breaker
  mcp/                   MCP stdio server and tools for AI agents
  dr/                    Backup encryption and sources for DR tooling
  evaluation/            Retrieval / grounding metrics
  ui/                    Plain-language copy catalogue, status labels, role-based navigation
  policy.py  access_control.py  search_acl.py   RBAC scopes, grants, vector ACL filter
  auth.py  mfa.py  security.py  rate_limit.py    JWT, TOTP, CSRF + headers, rate limits
  ocr.py  pdf_parser.py  chunker.py  summary_extractor.py  qdrant_store.py
  asr.py  diarization.py  tx_live.py  minutes_draft.py  minutes_pdf.py
  pdf_watermark.py  pdf_delivery.py  observability.py  correlation.py
  templates/             Jinja pages and macros
  static/                CSS, JavaScript, service worker, manifest, icons, fonts, pdf.js
migrations/              Alembic environment and versioned migrations
scripts/                 Admin, migration, backfill, benchmark, DR, evaluation, and screenshot tools
tests/                   Pytest suites
eval/                    Bilingual retrieval corpus and evaluation reports
loadtest/                Locust and k6 load tests
deploy/                  On-prem compose files, nginx, Prometheus, Grafana
```



### Useful scripts

| Script | Purpose |
|---|---|
| `create_admin_user.py` | Create or reset (`--update`) an admin account |
| `create_service_client.py` | Issue an `/api/v1` service or agent key with scopes |
| `eval_retrieval.py` | Bilingual retrieval evaluation (used in CI) |
| `ui_screenshots.py` | Screenshots of each screen per role, size and colour mode (demo data only) |
| `reindex_chunks.py`, `reindex_embeddings.py` | Rebuild vectors (e.g. after changing the embedding model) |
| `backfill_search_payload.py`, `backfill_version_hashes.py` | One-off data backfills |
| `rewrap_mfa_secrets.py` | Re-encrypt TOTP secrets under a new key |
| `migrate_local_pdfs_to_s3.py` | Move local uploads to object storage |
| `autoscale_workers.py` | Set worker replicas from stream backlog |
| `dr_backup.py`, `dr_restore.py`, `dr_failover_drill.py` | Encrypted backup, restore drill, failover drill |
| `bench_search.py`, `bench_accuracy.py`, `bench_stt.py` | Performance and accuracy benchmarks |
| `local_ai_check.py` | Smoke-test bank-hosted AI endpoints |
| `license_inventory.py`, `check_audit_append_only.py` | Compliance checks |

`create_database_and_tables.py`, `create_tables.py`, and `migrate_schema.py` are legacy bootstrap
scripts from before Alembic; use `python -m bcp_project.migrate` instead.

### Integrator API and MCP

```bash
# Issue a scoped key for an AI agent
PYTHONPATH=src python scripts/create_service_client.py --client-id ask-agent --name "MCP" \
  --agent --scopes ask:sonali,docs:search,mcp:invoke

# Run the MCP stdio server on behalf of a user
BCP_MCP_API_KEY=bcp_... BCP_MCP_ON_BEHALF=some_user PYTHONPATH=src python -m bcp_project.mcp.server
```

The full HTTP reference is served at `/docs` outside production.

---

## Install as a PWA

Open the site in Chrome or Edge (or Safari on iOS), sign in, then use **Install** / **Add to Home Screen**.
The service worker caches only the static shell (CSS, JavaScript, fonts, icons, and the offline page).
Pages, API responses, search results, PDFs, and page images are never cached; when the connection drops,
a banner says so and navigation shows the offline page. A new version waits until the person chooses
**Reload** in the update banner.

---

## Further reading

[`DOCUMENTATION.md`](DOCUMENTATION.md) covers the module map, access and viewer security, search,
security controls, the web interface, and meetings in more detail.
