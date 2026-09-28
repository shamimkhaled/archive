# Design Document

**Requirements (normative):** [REQUIREMENTS.md](./REQUIREMENTS.md)  
**Product:** Sonali Bank Intelligent Memory Platform  
**UI stack:** Jinja2 templates + static CSS/JS PWA shell  
**Brand constants:** `src/bcp_project/brand.py` (working name: Archive System)

Design must serve EOI pillars A–E — especially secure packs (R5.2), search/mind map (R6), and future Ask Sonali governed chat (R7).

---

## 1. Design principles

1. **Bank-first trust** — Navy/gold Sonali identity, confidential watermarks, clear access states.  
2. **One job per screen** — Upload, search, mind map, meetings, and admin are separate destinations.  
3. **Permission visible** — Search and viewer always surface whether the user can view, must request, or is denied.  
4. **Mobile as peer** — Installable PWA with bottom tabs; desktop keeps full nav.  
5. **Bilingual** — EN/BN language preference in the shell; Bangla OCR and keyword support in retrieval.  
6. **Source over spectacle** — AI-assisted features emphasize citation and control, not chatbot chrome.

---

## 2. Brand system

| Token | Value | Use |
|---|---|---|
| Product name | Sonali Bank Archive System | Titles, emails, watermarks |
| Org | Sonali Bank PLC / সোনালী ব্যাংক পিএলসি | Headers, seals |
| Tagline EN | Secure board governance, documents & archive | Login / about |
| Tagline BN | বিশ্বস্ত ও স্মার্ট | Brand line |
| Navy | `#1A1A54` | Primary UI |
| Gold | `#C5922F` | Accents |
| Doc ID prefix | `SB-{year}-…` | Archive identifiers |
| PWA short name | Sonali Archive | Home-screen label |

Assets (under `/static/img/`): logo, banner, login background.

Watermark seals always burn in product name, username, UTC timestamp, and confidentiality suffix (`Confidential` / `Downloaded` / `Meeting document`).

---

## 3. Information architecture

```
Login
 └── App shell (authenticated)
      ├── Home (dashboard)
      ├── Archive
      │    ├── Search
      │    ├── Mind map (/archive/map)
      │    ├── Viewer (/view/{doc_id})
      │    └── Access requests
      ├── Upload (admin / uploader)
      ├── Meetings
      │    ├── Organizer: /meetings/*
      │    └── Invitee: /board/meetings/*
      ├── Notifications
      ├── Appearance
      └── Admin (admin)
           ├── Users
           └── Access request review
```

### Mobile bottom tabs

**Home · Archive · Mind Map · Meetings · More**

Android long-press shortcuts: Mind Map, Archive, Meetings.

---

## 4. Key screens

| Screen | Template | Purpose |
|---|---|---|
| Login | `login.html` | Credential entry; branded background |
| Dashboard | `dashboard.html` | Counts, next meeting, role-aware CTAs |
| Upload | `upload.html` | PDF ingest with NDJSON progress stream |
| Search | `search.html` | Keyword / semantic + metadata filters |
| Archive map | `archive_map.html` | Relationship / mind-map explorer |
| Viewer | `viewer.html` | PDF.js secure viewer over watermarked stream |
| Access denied / requests | `access_denied.html`, `access_requests.html` | Request purpose + mode |
| Admin access / users | `admin_access_requests.html`, `admin_users.html` | Approvals & CRUD |
| Meetings | `meetings_list.html`, `meeting_form.html`, `meeting_detail.html` | Organizer workflow |
| Board meetings | `board_meetings_list.html`, `board_meeting_detail.html` | Invitee + attendance |
| Attendance print | `attendance_print.html` | Printable sheet |
| Notifications | `notifications.html` | In-app center |
| Appearance | `appearance.html` | Theme / display prefs |

Shared chrome: `base.html` + `macros/ui.html`. Offline fallback: `static/offline.html`. Service worker: `/sw.js`.

---

## 5. Interaction patterns

### Upload

- Restrict to PDF; show staged progress (parse → summarize → store → index).  
- Prefer `/api/upload/stream` (NDJSON) for live feedback; classic `/upload` remains.

### Search

- Primary query box with optional `lang=en|bn`.  
- Results show title/type/date snippets and **access chips** (`can_view`, pending, request CTA).  
- Metadata mode: doc ID, type, uploader, date range.  
- Do not surface raw vector scores to end users (quality-filtered labels only).

### Viewer

- Never stream the clean source PDF for archive view.  
- Always load `/view/{doc_id}/file` (server-stamped watermark).  
- Download is a separate, audited action with stronger privilege.

### Meetings

- Organizer edits agenda, toggles notifications, attaches pack docs, opens/closes attendance.  
- Invitees sign with drawn signature when attendance is open.  
- Transcription UI may show live captions; treat as **demo** until real ASR is integrated.

### Access requests

- Purpose required; mode `view_only` or `download`.  
- Reviewers set approve/deny + optional expiry days (default grant window 7 days).

---

## 6. Accessibility & responsive notes

- Prefer semantic forms and labels in templates.  
- Touch targets sized for phone bottom nav.  
- Print stylesheet path for attendance sheets.  
- Avoid relying on hover-only actions for primary flows.

---

## 7. Security UX

- Failed auth full-page navigations redirect to `/login`; API/XHR keep JSON `401`.  
- CSRF token available to forms and `X-CSRF-Token` header for cookie-auth POSTs.  
- Clear copy when access is denied vs. pending vs. expired.  
- Watermark text is identity-binding, not decorative.

---

## 8. Future design (capability E)

When shipping **Ask Sonali Bank**:

- Chat panel secondary to sources panel (citations first).  
- Empty state: “No Source, No Answer” when retrieval fails.  
- Blocked state: “No Permission, No Retrieval” when RBAC denies.  
- Every answer block lists document ID + page anchors; human confirm/copy actions.

Do not introduce a free-form public chatbot aesthetic; keep the board-governance visual language.
