# Sonali Bank Archive System

A secure bilingual (English + Bangla) document archive and board-meeting PWA for Sonali Bank PLC — FastAPI + SQLAlchemy (async) + PostgreSQL + Qdrant + Redis + Jinja2.

## Features

| Capability | Status |
|---|---|
| PDF ingest → structured summary → parent + chunk vectors in Qdrant | **Present** |
| Hybrid semantic search (summaries + chunks, RRF fusion) + metadata filters | **Present** |
| Redis search/metadata cache (version bump on upload) | **Present** |
| Request-gated view/download (`view_only` / `download`) | **Present** |
| Server-stamped watermarked PDF stream + audited download | **Present** |
| Board meetings, agendas, shared docs, invites | **Present** |
| Digital attendance with signature + printable sheet | **Present** |
| Resend invites + 48h/24h reminders + ICS + Google Calendar | **Present** |
| RBAC: admin, board_secretary, uploader, board_member | **Present** |
| CSRF, security headers, login rate limit, audit log | **Present** |
| English + Bangla retrieval (bilingual keywords, BN OCR when installed, `lang` search hint) | **Present** |

## Permission matrix

| Role | Upload | Search archive | View archive | Download (watermarked) | Manage users | Organize meetings | Approve access |
|---|---|---|---|---|---|---|---|
| `admin` | Yes | Yes | Yes | **Yes (only role)** | Yes | Yes | Yes |
| `board_secretary` | No | Yes | Yes | No | No | Yes | Yes |
| `uploader` | **Upload only** | No | No | No | No | — | No |
| `board_member` | No | Yes | After approved request | No (may request) | No | Invited only | No |

Downloads are always server-watermarked and restricted to administrators.

## Prerequisites

- Python 3.10+
- Docker (PostgreSQL, Qdrant, Redis — see `docker-compose.yml`)
- **OpenAI** (or OpenRouter) for summarization/embeddings
- **AWS S3** or local `UPLOAD_DIR` for PDFs
- **Resend** for emails (optional; skipped gracefully if unset)
- Optional OCR: Tesseract with `eng` + `ben` tessdata, Poppler

## Installation

1. Clone and create a venv; `pip install -r requirements.txt`
2. `cp .env.example .env` and set API keys
3. `docker compose up -d`
4. `python scripts/create_database_and_tables.py` (or `python scripts/migrate_schema.py` for upgrades)
5. `python scripts/create_admin_user.py --username admin`
6. `PYTHONPATH=src uvicorn bcp_project.main_api:app --reload --host 127.0.0.1 --port 8000` → http://127.0.0.1:8000/login

### Install as PWA (phone / desktop)

1. Open the site in Chrome/Edge (or Safari on iOS).
2. Sign in, then use **Install** / **Add to Home Screen**.
3. On phones, use the bottom tabs: **Home · Archive · Mind Map · Meetings · More**.
4. Long-press the app icon (Android) for shortcuts: Mind Map, Archive, Meetings.

## Project layout

```
src/bcp_project/
  main_api.py          Routes: auth, upload, search, view/download, access requests, meetings, admin
  models.py            Users, documents, access requests, audit logs, meetings
  access_control.py    Privilege bypass + grant checks
  pdf_watermark.py     Server-side watermark stamping
  security.py          CSRF, security headers, login rate limiting
  qdrant_store.py      Hybrid search + embedding helpers
  cache.py             Redis search/metadata cache
  notifications.py     Resend email
  reminders.py         48h/24h reminder sweep
  calendar_utils.py    ICS + Google Calendar links
  templates/ static/   PWA shell (web + installable mobile)
scripts/               DB bootstrap / migrate / admin create
```

## Access workflow

1. Board member searches the archive (results show access status).
2. Member submits a **view_only** or **download** request with a purpose.
3. Admin / board secretary approves (optional expiry days) or denies.
4. View streams a **server-watermarked** PDF (`/view/{doc_id}/file`). Download is a separate audited route (`/download/{doc_id}`) and requires download mode.
