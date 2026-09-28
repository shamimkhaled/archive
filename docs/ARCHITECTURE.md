# Architecture

**Requirements (normative):** [REQUIREMENTS.md](./REQUIREMENTS.md) — especially **R9** (layers) and **R10** (security/sovereignty)  
**Application entry:** `src/bcp_project/main_api.py`  
**Package layout:** `src/bcp_project/{routers,ports,jobs,...}`

> Architecture below maps the **EOI high-level stack** to software components. Gaps vs R9/R10 are called out explicitly.

---

## 0. EOI architecture layers → code

| PDF layer (R9) | Requirement | Current mapping |
|---|---|---|
| Data sources | Paper/digital, legacy, meetings | PDF upload + meeting packs; scan hardware external |
| Archive processing | High-res scan, mixed OCR, metadata QC | `pdf_parser`, `summary_extractor` (QC queue **gap**) |
| Data & search DB | Object store + vector + full-text | S3/local + Qdrant + Postgres ILIKE/chunks |
| Knowledge layer | Relationships, versioning, source links | `graph_builder` (**supersession/versioning gap**) |
| AI intelligence | Private models: Q&A, summary, compare | Summaries + embeddings; Ask Sonali Q&A **v0** (`/ask`, `/api/v1/ask`); compare / briefings **gap**; private hosting **gap** |
| Identity & security | SSO/AD, RBAC, append-only audit | Password JWT + RBAC + `audit_logs` (SSO/AD/MFA **gap**) |
| User experience | Secure viewer + admin dashboard | Jinja PWA + admin routers |

---

## 1. High-level view

```
Browser / installed PWA
        │
   FastAPI (main_api → routers/*)
        ├─ PostgreSQL     users, documents (+ summary_json), grants, audit, meetings
        ├─ Qdrant         document_summaries + document_chunks (1536-d cosine)
        ├─ Redis          search/graph cache, optional job queue, distributed locks
        ├─ S3 / local     PDF binaries
        ├─ ports/*        LLM, vector, storage, email adapters
        ├─ Resend         meeting invites / reminders (+ ICS)
        └─ APScheduler    48h/24h reminder sweep (Redis single-flight lock)
   + optional jobs.worker for durable chunk indexing
```

Matches the platform PDF’s high-level architecture: secure web viewer + admin, object storage + vector index, archive processing (OCR/metadata), identity/RBAC + audit, AI intelligence layer, knowledge relationships.

---

## 2. Runtime components

| Component | Module(s) | Notes |
|---|---|---|
| App factory | `main_api.py` | Middleware, lifespan, `include_routers`, static mount |
| Shared deps | `deps.py` | Auth helpers, PDF load, `schedule_chunk_index` |
| Routers | `routers/` | HTTP/HTML surface split by domain |
| ORM | `models.py`, `db.py` | SQLAlchemy 2 async + asyncpg |
| Security | `auth.py`, `security.py`, `access_control.py`, `policy.py`, `service_auth.py` | JWT, CSRF, grants, scopes, API keys |
| Ingest | `pdf_parser.py`, `summary_extractor.py`, `chunker.py` | Parse → summarize → chunk |
| Vectors | `qdrant_store.py` | Index + hybrid fuse |
| Graph | `graph_builder.py` | Mind map / related |
| Cache | `cache.py` | Redis TTL + version bump |
| Jobs | `jobs/{queue,worker,handlers,locks}.py` | Redis BRPOPLPUSH worker |
| Ports | `ports/{protocols,adapters}.py` | Integrator seams |
| API v1 / MCP | `routers/api_v1.py`, `mcp/`, `services/document_query.py` | Machine + agent read surface |
| Connectors | `ports/{registry,circuit,document_source,customer,identity,events}.py` | Bank SI adapters |
| Observability | `correlation.py`, `observability.py` | Correlation IDs, counters |
| Meetings | `notifications.py`, `reminders.py`, `calendar_utils.py` | Email + ICS + GCal |
| Brand | `brand.py` | Product/org constants |

Legacy CLI ingest: `main.py` / `bcp-project` entrypoint.

---

## 3. Request path patterns

### HTML pages

Jinja2 templates rendered by routers (`response_class=HTMLResponse`). Auth failure on full-page navigations → redirect `/login`.

### JSON APIs

`/api/*` endpoints return JSON; `401` stays JSON for SPA/`X-Requested-With: BCPNav` fetches.

### Middleware stack

1. `SecurityHeadersMiddleware` — CSP, nosniff, frame options; HSTS when production  
2. `CsrfMiddleware` — double-submit cookie for mutating cookie-auth requests  
3. Sliding session middleware — refresh JWT cookie when &lt;50% lifetime remains  

OpenAPI (`/docs`, `/redoc`, `/openapi.json`) disabled when `APP_ENV=production`.

---

## 4. Data model (logical)

```
User ──< DocumentAccessRequest >── DocumentRecord
User ──< AuditLog
User ──< NotificationEvent >── BoardMeeting
BoardMeeting ──< MeetingDocument
BoardMeeting ──< MeetingInvitation
BoardMeeting ──< MeetingAttendance
```

**Roles:** `admin` | `board_secretary` | `uploader` | `board_member`  
**Access modes:** `view_only` | `download`  
**Access statuses:** `pending` | `approved` | `denied` | `revoked`  
**Meeting statuses:** `scheduled` | `completed` | `cancelled`

Document types are free-text with suggested catalog (`document_types.py`): Meeting Minutes, Board Paper, Circular, Policy, etc.

---

## 5. Document pipeline architecture

```
POST /upload or /api/upload/stream
        │
        ├─ validate role + PDF size
        ├─ parse_pdf
        ├─ extract_document_summary (LlmPort)
        ├─ ObjectStoragePort.store_pdf
        ├─ VectorStorePort.upload_summary
        ├─ schedule_chunk_index ──► BackgroundTasks
        │                        └─► Redis job → worker → run_chunk_index
        └─ INSERT documents + audit + cache bump
```

View path always goes through `pdf_watermark.stamp_pdf_bytes` before streaming.

---

## 6. Search architecture

```
GET /api/search
  → Redis cache hit? enrich access → return
  → embed query
  → parallel Qdrant summary + chunk search
  → RRF fuse (+ lang / keyword boosts)
  → Postgres ILIKE fallback / merge
  → quality filter → cache store → access enrich → response
```

Timeouts protect embed/summary/chunk legs so one slow dependency does not hang the request.

---

## 7. Jobs & locks

| Concern | Mechanism |
|---|---|
| Chunk indexing | Redis list queue when `ENABLE_JOB_QUEUE=1` |
| Reliability | BRPOPLPUSH processing list; max attempts; dead-letter |
| Reminder sweep | APScheduler ~15 min + `redis_lock("reminder_sweep")` |

Without the queue flag, chunk indexing still runs via in-process background tasks.

---

## 8. Ports & adapters

```
routers / handlers
        │
   protocols (LlmPort, VectorStorePort, ObjectStoragePort, EmailPort)
        │
   adapters (OpenAI/OpenRouter, Qdrant, S3/local, Resend)
```

Singletons: `get_llm_client()`, `get_vector_store()`, `get_object_storage()`, `get_email_client()`.

---

## 9. Security architecture

| Control | Implementation |
|---|---|
| Credentials | bcrypt (passlib) |
| Session | JWT in HttpOnly cookie; optional OAuth2 password `/token` |
| Service auth | `service_clients` API keys (`X-API-Key`) + scopes |
| CSRF | Cookie + header/form field (`/api/v1` and API-key requests exempt) |
| Login abuse | In-process sliding window (~8 failures / 10 min) |
| API abuse | Redis rate limit per user/client |
| Archive authz | Role privilege + approved grants; shared `policy.py` for HTTP+MCP |
| PDF confidentiality | Server-side watermark; admin-only download |
| Audit | `audit_logs` + correlation_id / actor_type / tool_name; optional NDJSON export job |
| Observability | `X-Correlation-ID`; in-process metrics (`/api/v1/metrics`); OTel hook optional |
| Fail posture | Authz fail-closed; Redis/Resend fail-open for non-auth concerns |

---

## 10. Deployment architecture

| Environment | Pattern |
|---|---|
| Local | `docker compose` for Postgres + Qdrant + Redis; uvicorn + optional worker |
| Railway | Dockerfile build; managed Postgres/Neon, Qdrant Cloud, Upstash Redis, S3/R2 |
| Container | Python 3.12-slim, Poppler + Tesseract, healthcheck `/healthz` |

See [DEPLOYMENT.md](./DEPLOYMENT.md).

---

## 11. Testing surface

| Suite | Focus |
|---|---|
| `test_access_and_search.py` | Grants, expiry, download rules, RRF, Bangla, rate limit |
| `test_ports_and_jobs.py` | Queue flag, adapters, chunk_index dispatch |
| `test_graph_builder.py` | Entity edges / related payload |
| `test_qdrant_point_id.py` | Stable UUID5 for `SB-…` IDs |
| `test_dummy_transcription.py` | Demo transcription growth/cap |
| `test_env_loading.py` | `.env` loading |

Mostly unit-level; expand HTTP integration coverage as Capability E lands.

---

## 12. Alignment with PDF technical architecture

| PDF block | Code mapping |
|---|---|
| Secure web viewer / admin dashboard | Jinja PWA + admin routers |
| Object storage / vector / full-text | S3 + Qdrant + Postgres ILIKE / OCR text |
| Archive processing (OCR, metadata QC) | `pdf_parser` + LLM summary (QC is lightweight today) |
| Identity & security (RBAC, audit) | Roles + grants + `audit_logs` (SSO/AD & MFA: roadmap) |
| AI intelligence | Summaries + embeddings; Ask Sonali Q&A v0 (`ask_sonali` / HITL); compare: roadmap |
| Knowledge layer (relationships, source links) | Graph builder + parent/child vectors |
| Data sovereignty | Adapter ports; private LLM option: roadmap |
