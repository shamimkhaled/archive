# React PWA (`/app`)

The archive has two web front ends during the migration:

- **Jinja pages** (`src/bcp_project/templates`, root service worker `static/sw.js`) — the current UI, always available.
- **React PWA** (`frontend/`) — Vite, React 19, TypeScript, TanStack Query, React Router, served by FastAPI under `/app`.

Both use the same session cookie, the same CSRF token, and the same server routes. Nothing in the
workflow logic (OCR verification, minutes approval, briefing approval, four-eyes checks, audit) is
reimplemented in the browser.

## How it is wired

| Piece | Where |
| --- | --- |
| Static hosting, rollout flags, JSON bridge | `src/bcp_project/spa.py` |
| Session/flags endpoint, viewer and meeting payloads | `src/bcp_project/routers/app_shell.py` (`/api/app/...`) |
| Typed API client | `frontend/src/api/schema.d.ts` generated from FastAPI's OpenAPI schema; `client.ts` adds the CSRF header and 401 handling |
| Service worker | `vite-plugin-pwa` → `frontend/dist/sw.js`, scope `/app/` |
| Container build | `Dockerfile` stage `frontend` → copied to `/app/frontend/dist` |

### Authentication

- The session stays in the httpOnly `access_token` cookie. The browser never sees the JWT.
- Every non-GET request sends `X-CSRF-Token` from the readable `csrf_token` cookie (double submit), exactly as the Jinja pages do.
- A 401 sends the user to `/login?next=<current /app URL>`. The login page stores the `next` path (same-site paths only, see `safe_next`) in a short-lived httpOnly cookie and returns there after password, MFA, or SSO sign-in.

### Form routes as JSON (the bridge)

Workflow actions are HTML form posts that answer `303 See Other` with `?status=` or `?error=`.
When a request carries `X-BCP-Client: spa`, `spa_middleware` turns that redirect into:

```json
{"ok": true, "status": "minutes_approved", "error": null, "redirect": "/meetings/4?status=minutes_approved#minutes"}
```

Refusals (`*_locked`, `*_four_eyes`, `*_input`, `*_failed`, or any `?error=`) come back as HTTP 409 with `ok: false`.
Login, logout, and `/auth/*` are never bridged.

### Caching rules

- Precached: `index.html`, hashed JS/CSS, icons, manifest (the app shell, under 1 MB).
- Never cached: `/api/*`, PDFs, page images, the pdf.js worker, CMaps, and fonts. `runtimeCaching` is empty and CI fails if the built `sw.js` registers anything other than the `/app` navigation fallback.
- Server headers: hashed `assets/*` are `immutable`; `sw.js` and other files are `no-cache`; `index.html` is `no-store`.
- A new deploy shows a "new version available" prompt instead of silently swapping code under an open form.

### PDF viewing

Pages are rendered with pdf.js from the existing range-capable route (`/view/{doc_id}/file`), so only
the chunks for the pages read are downloaded. The server stamps the watermark before sending, and it
writes one audit row per view (the first range request). Iframes are not used: the app is served
with `X-Frame-Options: DENY` and `frame-ancestors 'none'`. CMaps, standard fonts, and the JPEG2000 and
JBIG2 decoders are served from `/app/pdfjs/`. When WebAssembly is blocked by the CSP, pdf.js falls
back to its JavaScript decoders.

## Rollout

Screens move one at a time with `UI_REACT_SCREENS`, in this order:

| Step | Flag value | Jinja URLs redirected to `/app` |
| --- | --- | --- |
| 1 | `search` | `/search`, `/ask` |
| 2 | `viewer` | `/view/{doc_id}` |
| 3 | `review` | `/review`, `/review/{doc_id}` |
| 4 | `meetings` | `/meetings`, `/board/meetings`, `/meetings/{id}` |
| 5 | `briefing` | `/documents/{doc_id}/briefing` |
| 6 | `admin` | `/admin/queues`, `/admin/ai` |

Example: `UI_REACT_SCREENS=search,viewer`. Use `all` once every screen has passed parity.

- Only browser navigations (`Accept: text/html`, GET) are redirected; API calls and form posts are not.
- `?classic=1` on any URL keeps the Jinja page, so support staff can compare the two side by side.
- Nothing is redirected until `frontend/dist/index.html` exists, so a missing build cannot break the site.
- Remove a screen from the flag to roll back. No deploy is needed beyond the variable change.
- These stay on Jinja for now and are linked from the app: meeting scheduling, invitations, attendance signatures, live transcription and recording upload, user administration, access-request review, and document upload.

### Parity checklist per screen

Before adding a screen to the flag, check on a staging copy:

1. Same data as the Jinja page for an admin, a Board Secretary, a Board member, and an uploader. A Board member must not see draft minutes, restricted documents they have no grant for, or the review queue.
2. Same audit rows (`view`, `verify_open`, `briefing_view`, workflow actions) for the same actions.
3. Four-eyes refusals show the server's message.
4. Bangla text renders and can be edited (OCR page text, minutes, briefing sections).
5. Works at 360 px width.

## Installability (Android, iOS) and Lighthouse

- Manifest: `id` and `scope` `/app/`, `display: standalone`, 192 and 512 icons (one maskable), theme `#1a1a54`, plus shortcuts for Search and Meetings.
- iOS: `apple-touch-icon`, `apple-mobile-web-app-capable`, `viewport-fit=cover`, and safe-area padding in the layout. Install from Safari with Share → Add to Home Screen.
- Service worker: registered from `/app/sw.js` with `Service-Worker-Allowed: /app/`. Offline navigation inside `/app/` falls back to the cached shell.
- HTTPS is required in production; Railway terminates TLS.

Lighthouse check (Chrome DevTools or CLI) against a deployed environment:

```bash
npx lighthouse https://<host>/app/search --only-categories=pwa,performance,accessibility,best-practices \
  --chrome-flags="--headless" --output html --output-path lighthouse-app.html
```

Signed-in pages need a session. Run Lighthouse from a logged-in Chrome profile, or audit `/app/` for the installability checks (the manifest and service worker load before sign-in).

## Development

```bash
# Terminal 1 — API on :8000
PYTHONPATH=src uvicorn bcp_project.main_api:app --reload --port 8000

# Terminal 2 — Vite dev server on :5173, proxying API/form/PDF routes to :8000
cd frontend && npm ci && npm run dev
# open http://localhost:5173/app/  (sign in at http://localhost:5173/login)
```

After changing a FastAPI route or its parameters:

```bash
PYTHONPATH=src python scripts/export_openapi.py && (cd frontend && npm run gen:api)
```

CI fails if `frontend/openapi.json` or `src/api/schema.d.ts` is out of date.

Checks: `npm run build` (typecheck and build), `npm test` (vitest), and `pytest tests/test_phase9_frontend.py`.
