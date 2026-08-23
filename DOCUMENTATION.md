# Sonali Bank Archive System — Documentation

PWA (web + installable mobile) for Sonali Bank PLC archive and board meetings.

Stack: **FastAPI · SQLAlchemy async · PostgreSQL · Qdrant · Redis · Jinja2 · Resend**.

## Architecture

```
Browser / installed PWA
        │
    FastAPI (main_api.py)
        ├─ PostgreSQL — users, documents (+ summary_json), access requests, audit logs, meetings
        ├─ Qdrant — document_summaries (parent) + document_chunks (child); hybrid search
        ├─ Redis — semantic + metadata search cache (versioned; fail-open)
        ├─ S3 or local UPLOAD_DIR — PDF binaries
        └─ Resend — meeting invites / reminders (+ ICS)
```

## Module map (`src/bcp_project/`)

| Module | Role |
|---|---|
| `brand.py` | Sonali Bank PLC product/org constants for templates, emails, watermarks |
| `pdf_parser.py` | llama-parse → pypdf → OCR (`eng`+`ben` when Tessdata present) |
| `summary_extractor.py` | LLM structured summary; bilingual searchable keywords |
| `chunker.py` | Token/whitespace chunks with `parent_doc_id` |
| `qdrant_store.py` | Index + **hybrid** search (summaries + chunks); rich embedding text |
| `cache.py` | Redis cache for keyword + metadata search |
| `access_control.py` | Privileged bypass; approved grant checks; audit helper |
| `pdf_watermark.py` | ReportLab + pypdf server-side stamp |
| `security.py` | CSRF double-submit, CSP/HSTS headers, login rate limiter |
| `models.py` | ORM including `DocumentAccessRequest`, `AuditLog` |
| `notifications.py` / `reminders.py` / `calendar_utils.py` | Live Resend + APScheduler 48h/24h + ICS/GCal |
| `main_api.py` | All HTTP routes |

## Access & viewer security

- Non-privileged archive users (`board_member`) **cannot** open `/view/{doc_id}` or `/view/{doc_id}/file` without an **approved** request.
- `/view/{doc_id}/file` always returns a **watermarked** PDF (identity seal burned in), never the clean source, for viewing.
- `/download/{doc_id}` requires download-mode approval (or privileged role) and writes an audit row.
- Grants may expire (`expires_at`); expired grants are treated as denied.

## Search

- `GET /api/search?q=…&lang=en|bn` — hybrid vector search; Redis cached by query+lang version; access flags enriched per user after cache read.
- `GET /api/search/metadata` — Doc ID / type / uploader / date range; also Redis-cached.
- Embedding text includes org, personnel, finance, projects, and EN/BN keywords.

## Security controls

| Control | Behavior |
|---|---|
| Auth | bcrypt passwords; JWT in HttpOnly cookie |
| CSRF | Cookie + `X-CSRF-Token` / form field for cookie-auth mutations |
| Headers | CSP, `X-Frame-Options`, nosniff, Referrer-Policy; HSTS in production |
| Login lockout | In-process sliding window (default 8 failures / 10 min) |
| Audit | login/logout, upload, view, download, access approve/deny |

Authorization **never** fail-opens. Redis/Resend may fail open for cache/email only.

## Meetings & attendance

Unchanged product behavior: organizers (`admin`, `board_secretary`) schedule meetings, agendas, documents, invites; Resend invitation + automatic 48h/24h reminders with ICS and Google Calendar link; digital signature attendance + printable sheet.

## Upgrading existing databases

```bash
python scripts/migrate_schema.py
```

Adds `documents.summary_json`, `document_access_requests`, `audit_logs`, and related enums/indexes idempotently.
