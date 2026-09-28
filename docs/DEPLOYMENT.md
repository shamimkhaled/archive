# Deployment

**Requirements (normative):** [REQUIREMENTS.md](./REQUIREMENTS.md) — R11 pilot phases, R12 KPIs, R10 sovereignty.

How to run Sonali Bank Intelligent Memory Platform / Archive System locally and in production.

---

## 1. Prerequisites

- Python 3.10+ (Docker image: 3.12)  
- Docker (Postgres 16, Qdrant v1.13, Redis 7)  
- LLM credentials (OpenAI **or** OpenRouter)  
- Object storage: local dir for demo, **S3/R2 for production**  
- Optional: Resend API key; Tesseract `eng`+`ben` + Poppler for OCR  

---

## 2. Local development

### 2.1 Services

```bash
docker compose up -d
```

Starts Postgres (`bcpdb`), Qdrant, Redis. Optional worker service is commented in `docker-compose.yml`.

### 2.2 App config

```bash
cp .env.example .env
# edit secrets
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/create_database_and_tables.py   # or migrate_schema.py on upgrades
python scripts/create_admin_user.py --username admin
```

### 2.3 API process

```bash
PYTHONPATH=src uvicorn bcp_project.main_api:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000/login

### 2.4 Optional durable worker

```bash
# Terminal A
ENABLE_JOB_QUEUE=1 PYTHONPATH=src uvicorn bcp_project.main_api:app --reload --host 127.0.0.1 --port 8000

# Terminal B
ENABLE_JOB_QUEUE=1 PYTHONPATH=src python -m bcp_project.jobs.worker
```

Without `ENABLE_JOB_QUEUE`, chunk indexing uses in-process `BackgroundTasks`.

---

## 3. Environment variables (summary)

See `.env.example` for the full list.

| Area | Key vars |
|---|---|
| App | `APP_ENV`, `JWT_SECRET_KEY`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `UPLOAD_DIR`, `COOKIE_SECURE` |
| DB | `DATABASE_URL` |
| Qdrant | `QDRANT_HOST`/`PORT` or `QDRANT_URL` + `QDRANT_API_KEY` |
| Redis | `REDIS_URL` or Upstash REST pair; `SEARCH_CACHE_TTL_SECONDS` |
| Jobs | `ENABLE_JOB_QUEUE`, `JOB_*` |
| LLM | `OPENAI_API_KEY` **or** `OPENROUTER_API_KEY` + models |
| Email | `RESEND_API_KEY`, `RESEND_FROM`, `RESEND_FROM_NAME` |
| Storage | `STORAGE_BACKEND`, `AWS_*`, `AWS_PREFIX`, `ALLOW_EPHEMERAL_UPLOADS` |

Production checklist:

1. `APP_ENV=production`  
2. Strong `JWT_SECRET_KEY` (≥ 32 chars)  
3. Durable `DATABASE_URL`  
4. HTTPS Qdrant cloud URL + key  
5. `rediss://` Redis  
6. One LLM provider only  
7. `STORAGE_BACKEND=s3` with working bucket credentials  

---

## 4. Docker image

`Dockerfile`:

- Base: `python:3.12-slim-bookworm`  
- System: build tools, curl, Poppler, Tesseract  
- Runs uvicorn on `$PORT` (default 8080) with proxy headers  
- Healthcheck: `GET /healthz`  
- Entrypoint: `docker-entrypoint.sh` (chown volume, drop to `appuser`)  

Build & run example:

```bash
docker build -t sonali-archive .
docker run --env-file .env -p 8080:8080 sonali-archive
```

---

## 5. Railway

Config: `railway.toml` (DOCKERFILE builder, healthcheck `/healthz`).

### Required secrets (typical)

- `APP_ENV=production`  
- `JWT_SECRET_KEY`  
- `DATABASE_URL` (Neon or Railway Postgres)  
- `QDRANT_URL` + `QDRANT_API_KEY`  
- `REDIS_URL` **or** Upstash REST vars  
- OpenRouter **or** OpenAI keys (not mixed)  
- `STORAGE_BACKEND=s3` + AWS/R2 credentials & bucket  

### Networking tips

1. Latest deployment must be **ACTIVE**.  
2. Public networking / custom domain attached to the **web** service.  
3. DNS CNAME matches Railway’s hostname.  
4. Health path: `/healthz`.  
5. Closing a Railway Shell does not stop the web process—use Redeploy/Restart for the app.  

### Worker on Railway

Run a second service/start command:

```bash
python -m bcp_project.jobs.worker
```

with the same env and `ENABLE_JOB_QUEUE=1` on both API and worker.

---

## 6. Database upgrades

```bash
python scripts/migrate_schema.py
```

Idempotently adds `summary_json`, access-request tables, audit logs, enums/indexes as needed. App startup also `create_all` and patches a few meeting transcription columns.

---

## 7. Operational scripts

| Script | Purpose |
|---|---|
| `scripts/create_database_and_tables.py` | Fresh DB |
| `scripts/migrate_schema.py` | Upgrade existing |
| `scripts/create_admin_user.py` | Bootstrap admin |
| `scripts/reindex_chunks.py` | Rebuild chunk vectors |
| `scripts/migrate_local_pdfs_to_s3.py` | Move binaries to object storage |

---

## 8. PWA install (end users)

1. Open the site in Chrome/Edge (or Safari on iOS).  
2. Sign in → **Install** / **Add to Home Screen**.  
3. Phone tabs: Home · Archive · Mind Map · Meetings · More.  

---

## 9. Security hardening for production

- Disable OpenAPI (`APP_ENV=production` already hides `/docs`).  
- Force secure cookies (`COOKIE_SECURE`) behind HTTPS.  
- Never commit `.env`.  
- Prefer S3/R2 over container disk.  
- Monitor `/healthz` and `/readyz`.  
- Keep Resend and Redis optional for availability—but never optional for authz.  

---

## 10. Smoke test after deploy

1. `/healthz` → 200  
2. Login as admin  
3. Upload a small text PDF → appears in search  
4. `/view/{doc_id}/file` returns watermarked PDF  
5. Create meeting → invite user with email → reminder job does not crash logs  
6. (If queue enabled) worker ack’s `chunk_index` jobs  

---

## 11. Related docs

- [ARCHITECTURE.md](./ARCHITECTURE.md) — component design  
- [INTEGRATION.md](./INTEGRATION.md) — external systems  
- [API.md](./API.md) — HTTP surface  
- [ROADMAP.md](./ROADMAP.md) — phased delivery  
