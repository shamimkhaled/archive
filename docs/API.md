# API Reference

**Requirements context:** [REQUIREMENTS.md](./REQUIREMENTS.md) (EOI) — this file documents the **current** HTTP surface.

**Base app:** FastAPI (`bcp_project.main_api:app`)  
**Auth:** HttpOnly JWT cookie `access_token` (browser) or `Authorization: Bearer` from `POST /token`  
**CSRF:** Mutating cookie-auth requests need CSRF cookie + `X-CSRF-Token` (or form field)  
**OpenAPI:** `/docs` available when not in production  

Roles used below: `admin`, `board_secretary`, `uploader`, `board_member`.

---

## Health & PWA

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/healthz` | No | Liveness |
| GET | `/readyz` | No | Readiness |
| GET | `/favicon.ico` | No | Icon |
| GET | `/sw.js` | No | Service worker |

---

## Auth

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/login` | No | Login page |
| POST | `/login` | No | Form login → set cookie |
| GET | `/logout` | Cookie | Clear session |
| POST | `/token` | No | OAuth2 password → `{ access_token, token_type }` |

---

## Pages

| Method | Path | Roles | Description |
|---|---|---|---|
| GET | `/` | Any logged-in | Dashboard (else login) |
| GET | `/appearance` | Logged-in | Appearance settings |

---

## Documents & archive

| Method | Path | Roles | Description |
|---|---|---|---|
| GET | `/upload` | `admin`, `uploader` | Upload UI |
| POST | `/upload` | `admin`, `uploader` | Classic multipart upload |
| POST | `/api/upload/stream` | `admin`, `uploader` | NDJSON progress upload |
| GET | `/api/document-types` | Logged-in (uploaders+) | Suggested + existing types |
| GET | `/api/documents/next-id` | `admin`, `uploader` | Next `SB-{year}-…` suggestion |
| GET | `/search` | `admin`, `board_secretary`, `board_member` | Search UI |
| GET | `/api/search` | same | Hybrid search |
| GET | `/api/search/metadata` | same | Metadata filters |
| GET | `/api/archive/documents` | same | Archive listing payload |
| GET | `/archive/map` | same | Mind map UI |
| GET | `/api/archive/graph` | same | Graph JSON |
| GET | `/api/documents/{doc_id}/related` | same | Related cluster |
| GET | `/view/{doc_id}` | privileged or grant | Viewer page |
| GET | `/view/{doc_id}/file` | privileged or grant | **Watermarked** PDF bytes |
| GET | `/download/{doc_id}` | `admin` (download rules) | Watermarked download + audit |

### `GET /api/search`

| Query | Type | Notes |
|---|---|---|
| `q` | string | Required query |
| `lang` | `en` \| `bn` \| `any` | Optional hint |

**Response shape (illustrative):**

```json
{
  "results": [
    {
      "doc_id": "SB-2026-0001",
      "score_label": "Strong",
      "can_view": true,
      "can_download": false,
      "access_status": "approved"
    }
  ],
  "count": 1,
  "query": "…",
  "mode": "hybrid",
  "cached": false
}
```

Exact result fields follow `qdrant_store` fusion + access enrichment.

### `GET /api/search/metadata`

Typical filters: document ID, type, uploader, date range (see router for parameter names). Redis-cached similarly to keyword search.

---

## Access requests

| Method | Path | Roles | Description |
|---|---|---|---|
| GET | `/access-requests` | Logged-in | Requester UI |
| POST | `/access-requests` | `board_member` (+ rules) | Form create request |
| POST | `/api/documents/{doc_id}/access-requests` | same | JSON create |
| GET | `/admin/access-requests` | `admin`, `board_secretary` | Review UI |
| POST | `/admin/access-requests/{request_id}/review` | same | Approve/deny (+ expiry) |

Request body concepts: `purpose`, `requested_mode` (`view_only` \| `download`).

---

## Admin users

| Method | Path | Roles | Description |
|---|---|---|---|
| GET | `/admin/users` | `admin` | User admin UI |
| POST | `/admin/users` | `admin` | Create user |
| POST | `/admin/users/{user_id}/update` | `admin` | Update user |
| POST | `/admin/users/{user_id}/delete` | `admin` | Delete user |

---

## Meetings (organizers)

Roles: typically `admin`, `board_secretary`.

| Method | Path | Description |
|---|---|---|
| GET | `/meetings` | List |
| GET | `/meetings/new` | Create form |
| POST | `/meetings` | Create meeting + invites |
| GET | `/meetings/{id}` | Detail |
| POST | `/meetings/{id}/agenda` | Update agenda |
| POST | `/meetings/{id}/notifications` | Toggle notifications |
| POST | `/meetings/{id}/notifications/resend` | Resend invites |
| POST | `/meetings/{id}/documents` | Attach pack PDF |
| GET | `/meetings/{id}/documents/{doc_id}/view` | Pack viewer page |
| GET | `/meetings/{id}/documents/{doc_id}/file` | Pack PDF stream |
| POST | `/meetings/{id}/attendance/open` | Open signing |
| POST | `/meetings/{id}/attendance/close` | Close signing |
| GET | `/meetings/{id}/attendance/print` | Printable sheet |
| GET | `/meetings/{id}/attendance/{username}/signature` | Signature image |
| POST | `/meetings/{id}/transcription/start` | Start mic recording + live caption session |
| POST | `/api/meetings/{id}/transcription/chunks` | Upload WebM audio chunk (`seq`, `chunk`); enqueues live STT |
| POST | `/meetings/{id}/transcription/stop` | Stop recording; final Whisper pass + minutes draft |
| GET | `/api/meetings/{id}/transcription` | Status JSON (`live` / `processing` / segments / minutes) |
| WS | `/ws/meetings/{id}/transcription` | Live caption stream (organizer cookie auth) |
| GET | `/api/meetings/{id}/minutes` | Minutes draft JSON |
| POST | `/meetings/{id}/minutes` | Save edited minutes (`body_md`) |
| POST | `/meetings/{id}/minutes/regenerate` | Re-run minutes draft from transcript |

---

## Board (invitees)

| Method | Path | Description |
|---|---|---|
| GET | `/board/meetings` | Invitee list |
| GET | `/board/meetings/{id}` | Invitee detail |
| POST | `/board/meetings/{id}/attendance` | Submit signature attendance |
| GET | `/board/meetings/{id}/documents/{doc_id}/view` | Pack viewer |
| GET | `/board/meetings/{id}/documents/{doc_id}/file` | Pack file |

---

## Notifications

| Method | Path | Description |
|---|---|---|
| GET | `/notifications` | UI |
| GET | `/api/notifications` | List events |
| POST | `/api/notifications/read` | Mark read |

---

## API v1 (integrator / agent surface)

Machine-oriented JSON API. Auth:

- **User:** `Authorization: Bearer <jwt>` (from `POST /token`) or cookie  
- **Service/agent:** `X-API-Key: bcp_…` (from `scripts/create_service_client.py`)  
- **On behalf of user (for grants):** `X-On-Behalf-Of: <username>`  
- Responses include `X-Correlation-ID`  
- Redis rate limit per client/user (`API_V1_RATE_LIMIT_PER_MINUTE`, default 120)

| Method | Path | Scopes | Description |
|---|---|---|---|
| GET | `/api/v1/documents/search?q=` | `docs:search` | Hybrid search (authz-filtered) |
| GET | `/api/v1/documents/{doc_id}` | `docs:meta` | Metadata (+ summary if viewable) |
| GET | `/api/v1/documents/{doc_id}/status` | `docs:status` | Existence / index / can_view |
| GET | `/api/v1/meetings` | `meetings:list` | Meetings for principal user |
| GET | `/api/v1/mcp/tools` | `mcp:invoke` | List MCP tool schemas |
| POST | `/api/v1/mcp/tools/{name}` | `mcp:invoke` | Invoke tool (`{"arguments":{…}}`) |
| GET | `/api/v1/metrics` | `audit:read` | In-process counters/timings |

Privileged archive bypass for trusted automation: scope `docs:search:privileged`.

### Integrations (Phase 2)

| Method | Path | Scopes | Description |
|---|---|---|---|
| GET | `/api/v1/integrations/health` | `integrations:health` | Adapter registry + circuit status |
| GET | `/api/v1/customers/{ref}` | `cbs:customer.read_min` | Allowlisted customer summary |
| POST | `/api/v1/connectors/document-source/pull` | `connectors:pull` | Pull inbox/SFTP PDFs into archive |

Outbound event: `document.indexed` (HMAC-signed webhooks via `WEBHOOK_URLS`).

OIDC scaffolding: `GET /auth/oidc/start`, `GET /auth/oidc/callback` when `OIDC_ENABLED=1`.

### Ask Sonali + HITL + facades (Phase 3)

| Method | Path | Scopes | Description |
|---|---|---|---|
| POST | `/api/v1/ask` | `ask:sonali` | Governed Q&A; refuses without authorized sources; citations required |
| GET | `/ask` | (session UI) | Ask Sonali Bank page |
| POST | `/api/v1/hitl/requests` | `hitl:request` | Create approval request for mutating tools |
| GET | `/api/v1/hitl/requests` | `hitl:review` | List pending approvals |
| POST | `/api/v1/hitl/requests/{id}/review` | admin/secretary | Approve or deny |
| GET | `/api/v1/loans/{ref}` | `loan:read_min` | Allowlisted loan summary stub |
| GET | `/api/v1/hr/employees/{id}` | `hr:read_min` | Allowlisted HR summary stub |

HITL-gated MCP tools (`connector_pull_now`, `request_document_access`, `trigger_approved_workflow`) require `approval_request_id` of an **approved** row.

### MCP stdio server

```bash
BCP_MCP_API_KEY=bcp_... BCP_MCP_ON_BEHALF=board_user \
  PYTHONPATH=src python -m bcp_project.mcp.server
```

Tools include: `search_documents`, `get_document_metadata`, `check_document_status`, `list_my_meetings`, `get_approved_customer_summary`, `ask_sonali`, `get_approved_loan_summary`, `get_approved_hr_summary`, plus HITL-gated mutations.

---

## Error conventions

| Status | Meaning |
|---|---|
| 401 | Unauthenticated (HTML → `/login` redirect when Accept wants HTML) |
| 403 | Authenticated but forbidden / missing scope / HITL required |
| 404 | Missing resource |
| 422 | Validation error |
| 429 | Rate limited (login or `/api/v1`) |

---

## Background worker (not HTTP)

```bash
ENABLE_JOB_QUEUE=1 PYTHONPATH=src python -m bcp_project.jobs.worker
# Optional: JOB_WORKER_QUEUES=ingest|meeting|notify|all
```

Consumes Redis multi-queues (`chunk_index`, connector pull, meeting STT/minutes, webhooks). See [DR.md](./DR.md).

---

## Related

- [DR.md](./DR.md) — readiness, multi-queue, recovery drills  
- Decision registry / minutes actions: still roadmap (R6.6, R5.4–R5.5)
