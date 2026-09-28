# Integrations

**Requirements (normative):** [REQUIREMENTS.md](./REQUIREMENTS.md) — R9 / R10 (private AI option, SSO/AD, object store, vector, audit).

External systems and how this codebase connects to them. Adapter seams live in `src/bcp_project/ports/`.

---

## 1. Integration map

```
                    ┌─────────────┐
                    │  FastAPI    │
                    └──────┬──────┘
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
     PostgreSQL/Neon    Qdrant Cloud     Redis/Upstash
           │               │               │
           │               │               ├─ search cache
           │               │               ├─ job queue
           │               │               └─ locks
           ▼               ▼
        Object store      LLM provider
      (S3 / R2 / local)  (OpenAI / OpenRouter)
           │
           ▼
         Resend ──► invitee mailboxes (+ ICS)
```

---

## 2. PostgreSQL (primary data)

| Item | Detail |
|---|---|
| Driver | `asyncpg` via SQLAlchemy async |
| URL | `DATABASE_URL` (`postgresql://` auto-normalized to `postgresql+asyncpg://`) |
| Hosted | Local Docker or Neon (SSL helpers in config) |
| Owns | Users, documents, access requests, audit logs, meetings graph |

Bootstrap scripts: `scripts/create_database_and_tables.py`, `scripts/migrate_schema.py`, `scripts/create_admin_user.py`.

---

## 3. Qdrant (vector memory)

| Item | Detail |
|---|---|
| Local | `QDRANT_HOST` + `QDRANT_PORT` (compose service `qdrant`) |
| Cloud | `QDRANT_URL` **https** + `QDRANT_API_KEY` (aliases: `QDRANT_CLUSTER_ENDPOINT` / `QDRANT_CLUSTER_API`) |
| Collections | `document_summaries`, `document_chunks` |
| Client usage | `qdrant_store.QdrantIndexer` / `QdrantVectorStoreAdapter` |

**Caution:** Cloud endpoints must be HTTPS. HTTP `…:6333` often returns “404 page not found” on upload/search.

---

## 4. Redis / Upstash

| Use | Detail |
|---|---|
| Search/metadata cache | `cache.py`; TTL `SEARCH_CACHE_TTL_SECONDS` |
| Job queue | Lists when `ENABLE_JOB_QUEUE=1`; multi-queue ingest/meeting/notify when `ENABLE_MULTI_QUEUE=1` (see [DR.md](./DR.md)) |
| Locks | Reminder sweep single-flight |

Config:

- Prefer `REDIS_URL` (`redis://` local or `rediss://` Upstash)  
- Or `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN` (app can derive `rediss://`)

Fail-open: cache/lock/email degradation must not grant document access.

---

## 5. Object storage (AWS S3 / Cloudflare R2 / local)

| Item | Detail |
|---|---|
| Module | `aws_utils.py` / `ObjectStorageAdapter` |
| Mode | `STORAGE_BACKEND=local\|s3\|auto` |
| Keys | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_BUCKET_NAME`, `AWS_PREFIX`, `AWS_REGION` |
| Custom endpoint | `AWS_S3_ENDPOINT_URL` (R2/MinIO); leave empty for AWS |

Production (Railway): **S3/R2 required** — container disk is ephemeral.  
`ALLOW_EPHEMERAL_UPLOADS=1` is demo-only.

Migration helper: `scripts/migrate_local_pdfs_to_s3.py`.

---

## 6. LLM provider (OpenAI or OpenRouter)

| Concern | Env |
|---|---|
| Official OpenAI | `OPENAI_API_KEY` (+ default models) |
| OpenRouter | `OPENROUTER_API_KEY`, optional `OPENAI_BASE_URL=https://openrouter.ai/api/v1`, chat/embedding model IDs |
| Models | `OPENAI_CHAT_MODEL`, `OPENAI_EMBEDDING_MODEL` |

Used for:

- Structured document summaries  
- Query + chunk embeddings (typically 1536-d `text-embedding-3-small` family)

**Do not mix** an OpenRouter key with `api.openai.com` (causes `401 invalid_issuer`). Prefer one provider per deploy.

Port: `OpenAILlmAdapter` implementing `LlmPort`.

---

## 7. Email (Resend)

| Item | Detail |
|---|---|
| Keys | `RESEND_API_KEY`, `RESEND_FROM`, `RESEND_FROM_NAME` |
| Alias | `RESEND_EMAIL_API` |
| Content | Meeting invite / 48h / 24h reminder bodies + ICS attachment + Google Calendar link |

Graceful skip when unset. Port: `ResendEmailAdapter` / `notifications.send_meeting_email`.

Calendar helpers: `calendar_utils.py`. Scheduler: APScheduler in app lifespan → `reminders.send_due_reminders`.

---

## 8. OCR toolchain (optional system deps)

| Binary | Purpose |
|---|---|
| Poppler (`pdftoppm`) | Rasterize PDF pages |
| Tesseract (`eng` + `ben` tessdata) | Bangla/English OCR fallback |

Installed in the production Dockerfile. Without them, parse falls back to text-extractable PDFs only.

Also: llama-parse path when configured/available inside `pdf_parser.py`.

---

## 9. Identity integrations (planned)

PDF calls for SSO/AD and MFA readiness. **Current:** local username/password + JWT cookie.  
Future work should plug into `auth.py` without changing grant semantics in `access_control.py`.

---

## 10. Bank system integrator notes

Ports exist so a bank SI can swap:

| Port | Swap examples |
|---|---|
| `LlmPort` | Private Azure OpenAI, on-prem model gateway |
| `VectorStorePort` | Self-hosted Qdrant, alternate vector DB |
| `ObjectStoragePort` | On-prem MinIO, bank object store |
| `EmailPort` | Exchange / SMTP gateway instead of Resend |

Keep authorization and audit inside the app core—adapters must not bypass RBAC.

### Phase 1 machine surface

| Surface | How |
|---|---|
| `/api/v1/*` | JWT or `X-API-Key` + optional `X-On-Behalf-Of` |
| MCP stdio | `python -m bcp_project.mcp.server` with `BCP_MCP_API_KEY` |
| Policy | `policy.py` scopes shared by HTTP and MCP |
| Create client | `scripts/create_service_client.py` |

### Phase 2 connectors

| Adapter | Env | Notes |
|---|---|---|
| Document source | `DOCUMENT_SOURCE=local\|sftp` | Inbox drop or SFTP pull (`paramiko` optional) |
| Customer | `CUSTOMER_PORT=mock\|http` | Allowlisted fields only |
| Identity | `OIDC_ENABLED` + `OIDC_*` | Local password default; OIDC scaffolding |
| Events | `WEBHOOK_URLS`, `WEBHOOK_SECRET` | `document.indexed` fan-out |
| Registry | `/api/v1/integrations/health` | Health + circuit breakers |

### Phase 3 Ask / HITL / facades

| Surface | Notes |
|---|---|
| Ask Sonali | `/ask` UI + `POST /api/v1/ask` + MCP `ask_sonali` — No Source/No Answer + citations |
| HITL | `/api/v1/hitl/requests` — mutating MCP tools need approved `approval_request_id` |
| Loan / HR | Mock allowlisted stubs via registry (`loan`, `hr`) until bank facades are wired |
| DR | [DR.md](./DR.md) — multi-queue workers, readiness, recovery drills |

---

## 11. What this product does **not** integrate with

Per scope boundary:

- Core Banking System / ledgers (CustomerPort is a **read-minimized facade**, not CBS)  
- Loan origination / credit engines (LoanPort stub is read-min summary only)  
- Payment switches / card / treasury rails  
- Uncontrolled consumer public AI chat products  

Those remain explicitly out of scope as systems of record.
