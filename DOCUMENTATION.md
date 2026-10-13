# Sonali Bank Archive System — Documentation

Server-rendered web app (installable as a PWA on desktop and mobile) for the Sonali Bank PLC archive and board meetings.

Stack: **FastAPI · SQLAlchemy async + Alembic · PostgreSQL · Qdrant · Redis · Jinja2 templates with plain CSS and JavaScript · Resend**.

## Architecture

```
Browser / installed PWA (Jinja pages, no build step)
        │
    FastAPI (main_api.py → routers/*)
        ├─ PostgreSQL — users, documents (+ summary_json), access requests, audit logs, meetings, outbox
        ├─ Qdrant — document_summaries (parent) + document_chunks (child); hybrid search
        ├─ Redis — search cache + job streams (ENABLE_JOB_QUEUE)
        ├─ S3 or local UPLOAD_DIR — PDF binaries
        ├─ ports/* — LLM / vector / storage / email adapters
        └─ Resend — meeting invites / reminders (+ ICS)
    + outbox relay and jobs.worker processes for ingest, meeting, and notify queues
```

## Module map (`src/bcp_project/`)

| Module | Role |
|---|---|
| `main_api.py` | App factory: middleware, lifespan, `include_routers`, `/static` mount |
| `deps.py` | Shared auth deps, PDF loaders, the Jinja environment (`templates`) and its globals |
| `routers/` | Route modules (auth, pages, documents, review, meetings, board, briefings, admin, …) |
| `ui/` | Plain-language copy catalogue (`copy_en.json`), status and reason labels, date display, role-based navigation (`nav.py`) |
| `templates/` | Jinja pages; `macros/ui.html` (buttons, status pills, empty states, form fields) and `macros/icons.html` |
| `static/` | `css/` (tokens, layout, components), `js/` (app shell, viewer helpers, recording, signature pad, service worker), fonts, icons, pdf.js |
| `ports/` | Adapter ports (OpenAI, Qdrant, S3, Resend) |
| `jobs/` | Outbox, relay, Redis Streams queue, locks, handlers, worker CLI |
| `brand.py` | Sonali Bank PLC product/org constants for templates, emails, watermarks |
| `pdf_parser.py` | llama-parse → pypdf → OCR (`eng`+`ben` when Tessdata present) |
| `summary_extractor.py` | LLM structured summary; bilingual searchable keywords |
| `chunker.py` | Token/whitespace chunks with `parent_doc_id` |
| `qdrant_store.py` | Index + **hybrid** search (summaries + chunks); rich embedding text |
| `cache.py` | Redis cache for keyword + metadata search |
| `access_control.py` | Privileged bypass; approved grant checks; audit helper |
| `pdf_watermark.py` | ReportLab + pypdf server-side stamp |
| `security.py` | CSRF double-submit, CSP/HSTS headers, `no-store` caching, safe post-login redirects, login rate limiter |
| `models.py` | ORM including `DocumentAccessRequest`, `AuditLog` |
| `notifications.py` / `reminders.py` / `calendar_utils.py` | Live Resend + APScheduler 48h/24h + ICS/GCal |

## Web interface

All screens are Jinja templates rendered by FastAPI. There is no separate frontend application and no
JavaScript build: templates, CSS, and scripts are served as they are from `src/bcp_project/`.

- **Layout.** `base.html` provides the shell: a sidebar on wide screens, a bottom bar plus "More" sheet on
  phones, a page header, toasts, the offline banner, and the update banner. The links each role sees come
  from `ui/nav.py`, so the menu never shows a page the role cannot open.
- **Design system.** `static/css/tokens.css` holds every colour, font size, spacing value, and radius.
  Themes are set on `<html>` with `data-theme` (green, red, yellow, white), `data-mode` (light, dark), and
  `data-text-size` (standard, large, extra large), chosen by each person on `/appearance`.
  `style.css` handles layout and `components.css` the shared components used by `macros/ui.html`.
- **Wording.** Labels, status names, and error reasons come from `ui/copy_en.json` through `t()`,
  `status_label`/`status_tone`, and `reason_label`/`reason_fix`, so the same state is described the same
  way on every page. Dates are shown with `local_dt`.
- **Navigation.** `static/js/app.js` fetches the next page with the `X-Requested-With: BCPNav` header and
  swaps the main content, falling back to a normal page load on any error. A sign-in redirect always
  becomes a full navigation to `/login`. Pages doing work that would be lost (such as a live recording)
  can block navigation until the person confirms.
- **Fonts and viewer.** Inter and Noto Sans Bengali are self-hosted in `static/fonts/`, and pdf.js in
  `static/vendor/pdfjs/`; the Content-Security-Policy allows no third-party script, style, or font hosts.
- **Screenshots.** `scripts/ui_screenshots.py` starts the app on a throw-away SQLite database with demo
  records and captures each screen per role at phone, tablet, and desktop widths in light and dark mode.

### PWA and caching

- `static/manifest.webmanifest` lists the icons (including maskable and monochrome) and screenshots.
- `static/js/sw.js` pre-caches only static files under `/static/` (CSS, JavaScript, fonts, icons, images,
  and `offline.html`). Navigations go to the network and fall back to the offline page.
- Pages, API responses, search results, PDFs, and page images are never cached: every response outside
  `/static/` is sent with `Cache-Control: no-store`, and sign-out sends `Clear-Site-Data: "cache"`.
- A new service worker waits until the person chooses **Reload** in the update banner.
- Notification clicks open only same-site paths.

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
| Auth | bcrypt passwords; JWT in HttpOnly cookie; TOTP MFA with QR enrolment and recovery codes |
| Post-login return | `?next=` is kept only for same-site paths (`safe_next`); absolute and protocol-relative URLs are dropped |
| CSRF | Cookie + `X-CSRF-Token` / form field for cookie-auth mutations |
| Headers | CSP (self-hosted assets only, `object-src 'none'`), `X-Frame-Options`, nosniff, Referrer-Policy; HSTS in production |
| Caching | `Cache-Control: no-store` on everything outside `/static/`; `Clear-Site-Data: "cache"` on sign-out |
| Login lockout | In-process sliding window (default 8 failures / 10 min) |
| Audit | login/logout, upload, view, download, access approve/deny |

Authorization **never** fail-opens. Redis/Resend may fail open for cache/email only.

## Meetings & attendance

Organizers (`admin`, `board_secretary`) schedule meetings, agendas, documents, invites; Resend invitation + automatic 48h/24h reminders with ICS and Google Calendar link; digital signature attendance + printable sheet. Board members see their own meetings under **My meetings**; organizers manage them under **Manage meetings**, with each meeting page grouped by stage (before, during, after).

## Upgrading existing databases

```bash
PYTHONPATH=src python -m bcp_project.migrate
```

Runs Alembic to the latest revision under a Postgres advisory lock; it is safe to run repeatedly and also
runs on app start unless `AUTO_MIGRATE=0`.
