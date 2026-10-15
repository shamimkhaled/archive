# Sonali Bank AI Archive: Visual Documentation Guide



## 1. System architecture

```mermaid
flowchart LR
  subgraph Client
    People[Browser users]
  end
  subgraph App
    Web[Web app - FastAPI + server-rendered pages]
  end
  subgraph Data
    DB[(PostgreSQL - 25 tables)]
    Qdrant[(Qdrant - vector search)]
    Files[(File store - local or S3)]
  end
  subgraph AI
    LLM[AI helper - summary / Ask Sonali]
    ASR[ASR - Whisper local or remote]
    Dia[Diarization - optional]
  end
  subgraph Queue
    Outbox[outbox_events table]
    Relay[Outbox relay]
    Redis[(Redis streams)]
  end
  subgraph Workers
    Ingest[Ingest worker]
    Meet[Meeting worker]
    Notify[Notify worker]
  end

  People --> Web
  Web --> DB
  Web --> Qdrant
  Web --> Files
  Web --> LLM
  Web --> Outbox
  Outbox --> Relay --> Redis
  Redis --> Ingest
  Redis --> Meet
  Redis --> Notify
  Ingest --> DB
  Ingest --> Qdrant
  Ingest --> Files
  Ingest --> LLM
  Meet --> DB
  Meet --> ASR
  Meet --> Dia
  Meet --> LLM
  Notify --> DB
```

## 2. Document lifecycle

```mermaid
stateDiagram-v2
  [*] --> received: upload (10%)
  received --> queued: job saved (20%)
  queued --> parsing: worker reads file (35%)
  parsing --> needs_review: weak OCR / Bangla doubt (45%)
  parsing --> summarizing: strong read (60%)
  needs_review --> summarizing: different person accepts
  needs_review --> rejected: different person rejects
  summarizing --> indexing: (82%)
  indexing --> indexed: searchable (100%)
  parsing --> failed
  summarizing --> failed
  indexing --> failed
  indexed --> [*]
  rejected --> [*]
  failed --> [*]
```

## 3. Upload to search (sequence)

```mermaid
sequenceDiagram
  actor U as Uploader
  participant W as Web app
  participant DB as PostgreSQL
  participant S as File store
  participant R as Redis ingest lane
  participant I as Ingest worker
  participant Q as Qdrant
  actor V as Reviewer (different person)

  U->>W: Upload PDF / image
  W->>S: Save original (unchanged)
  W->>DB: documents + document_versions + outbox_events (one transaction)
  W-->>U: received
  DB->>R: Relay copies outbox row
  R->>I: document_ingest
  I->>I: PDF text or OCR (English + Bangla)
  alt weak read
    I->>DB: needs_review
    V->>W: Accept or fix pages
  end
  I->>I: Summarize, chunk
  I->>Q: Store vectors (with ACL payload)
  I->>DB: indexed
```

## 4. Meeting speech to approved minutes

```mermaid
flowchart TD
  Live[Live meeting audio] --> Chunk[meeting_chunk_stt]
  File[Uploaded recording] --> Full[meeting_transcribe]
  Chunk --> Words[Words with timestamps]
  Full --> Words
  Words --> Who{Diarization on?}
  Who -- yes --> Spk[Speaker-labelled words]
  Who -- no --> Raw[Unlabelled words]
  Spk --> Draft[meeting_minutes writes draft]
  Raw --> Draft
  Draft --> Edit[Secretary edits - minutes_state: draft]
  Edit --> Review[in_review]
  Review -->|changes| Edit
  Review --> Approved[approved]
  Approved --> Idx[Indexed into search]
  Draft -. never indexed .-x Idx
```

### The three meeting clocks

```mermaid
flowchart LR
  subgraph transcription_status
    t1[off] --> t2[live] --> t3[processing] --> t4[stopped]
  end
  subgraph minutes_status
    m1[off] --> m2[pending] --> m3[generating] --> m4[ready]
    m3 --> m5[failed]
  end
  subgraph minutes_state
    s1[draft] --> s2[in_review] --> s3[approved]
    s2 --> s1
  end
```

## 5. Queue lanes

```mermaid
flowchart LR
  Outbox[(outbox_events)] --> Relay[Relay] --> Lanes
  subgraph Lanes[Redis streams]
    L1[bcp:stream:ingest]
    L2[bcp:stream:meeting]
    L3[bcp:stream:notify]
    L4[bcp:stream:default]
  end
  L1 --> W1[Ingest worker: document_ingest, chunk_index, board_briefing, connector_pull]
  L2 --> W2[Meeting worker: meeting_transcribe, meeting_chunk_stt, meeting_minutes]
  L3 --> W3[Notify worker: webhook_dispatch, audit_export, siem_forward]
  W1 -- 3 failures --> Dead[bcp:stream:dead]
  W2 -- 3 failures --> Dead
  W3 -- 3 failures --> Dead
  Dead --> Admin[/admin/queues resend/]
  Admin --> Relay
  L2 -. unused name .-> X[meeting_recording_ingest - no worker, QA to confirm unused]
```

## 6. Roles and permissions

```mermaid
flowchart LR
  admin --> up[Upload]
  admin --> chk[Check weak read]
  admin --> srch[Search + Ask]
  admin --> open[Open paper]
  admin --> dl[Download]
  admin --> mtg[Run meeting]
  board_secretary --> chk
  board_secretary --> srch
  board_secretary --> open
  board_secretary --> mtg
  uploader --> up
  uploader --> chk
  board_member --> srch
  board_member -. after approval .-> open
  board_member -. if invited .-> mtg
```

## 7. Access request flow (board member)

```mermaid
sequenceDiagram
  actor M as Board member
  participant W as Web app
  participant DB as document_access_requests
  actor A as Admin / Secretary
  M->>W: Request view or download + reason
  W->>DB: status = pending
  A->>W: Approve or deny
  W->>DB: approved (may expire) / denied
  M->>W: Open PDF
  W-->>M: Watermarked with M's name
  Note over M,W: Download stays admin-only
```

## 8. Folder ownership

```mermaid
flowchart TB
  Lead[Lead: models.py, schemas.py, db.py, config.py, main_api.py, deps.py, migrations/]
  Lead --> Screens[Screens: templates, css, js, ui]
  Lead --> Doors[Doors: auth, mfa, security, policy, ACL]
  Lead --> Papers[Papers: OCR, parser, watermark, ingest, review]
  Lead --> Find[Find and ask: qdrant, search, ask_sonali, hitl]
  Lead --> Meet[Meetings and speech: asr, diarization, minutes, briefing]
  Lead --> Mail[Mail room: jobs/]
  Lead --> Out[Outside systems: ports, mcp, api_v1, siem]
  Lead --> Ship[Keep, watch, ship: retention, audit chain, dr, deploy]
  Lead --> QA[QA: tests, eval, loadtest]
```

## 9. Database ERD (25 tables)

### 9a. Papers

```mermaid
erDiagram
  documents ||--o{ document_versions : has
  documents ||--o{ document_pages : has
  documents ||--o{ document_access_requests : has
  documents ||--o{ document_disposals : has
  retention_rules ||--o{ documents : "by doc_type"

  documents {
    string id PK
    string title
    string doc_type FK
    date doc_date
    string uploaded_by
    string file_hash
    float read_confidence
    string review_state
    string current_version_id
    string sensitivity "public|internal|confidential|restricted"
  }
  document_versions {
    string id PK
    string document_id FK
    string file_hash
    string status "pending_review|reviewed|approved|superseded|rejected"
  }
  document_pages {
    string id PK
    string document_id FK
    string version_id FK
    int page_no
    text page_text
    float confidence
  }
  document_access_requests {
    string id PK
    string document_id FK
    string requester_id
    string kind "view|download"
    text reason
    string status "pending|approved|denied|revoked"
    datetime expires_at
  }
  retention_rules {
    string doc_type PK
    int retain_years
  }
  document_disposals {
    string id PK
    string document_id FK
    string proposed_by
    string approved_by "second person"
    string status
  }
```

### 9b. Meetings

```mermaid
erDiagram
  board_meetings ||--o{ meeting_decisions : has
  board_meetings ||--o{ meeting_actions : has
  board_meetings ||--o{ meeting_documents : has
  board_meetings ||--o{ meeting_records : has
  board_meetings ||--o{ meeting_invitations : has
  board_meetings ||--o{ meeting_attendance : has
  board_meetings ||--o{ notification_events : has
  meeting_decisions ||--o{ meeting_actions : has
  documents ||--o{ board_briefings : has

  board_meetings {
    string id PK
    string title
    datetime starts_at
    string place
    text agenda
    string attendance_mode "open|closed"
    string status "scheduled|completed|cancelled"
    string transcription_status
    string minutes_status
    string minutes_state
    string recording_hash
  }
  meeting_decisions {
    string id PK
    string meeting_id FK
    text decision
  }
  meeting_actions {
    string id PK
    string meeting_id FK
    string decision_id FK
    string owner
    date due_date
    string status "open|done"
  }
  meeting_documents {
    string id PK
    string meeting_id FK
    string file_ref
  }
  meeting_records {
    string id PK
    string meeting_id FK
    string kind "recording|transcript|approved_minutes"
    string hash
  }
  meeting_invitations {
    string id PK
    string meeting_id FK
    string user_id FK
    bool sent_48h
    bool sent_24h
  }
  meeting_attendance {
    string id PK
    string meeting_id FK
    string user_id FK
    string signature_file
  }
  board_briefings {
    string id PK
    string doc_id FK
    text eight_part_note
    string status "draft|management_review|approved"
  }
  notification_events {
    string id PK
    string meeting_id FK
    string user_id FK
    text message
  }
```

### 9c. People and safety

```mermaid
erDiagram
  users ||--o| user_mfa : has
  users ||--o{ audit_logs : acts
  service_clients ||--o{ approval_requests : requests

  users {
    string id PK
    string name
    string password_hash
    string role "admin|board_secretary|uploader|board_member"
  }
  user_mfa {
    string user_id PK
    string secret_encrypted
  }
  audit_logs {
    string id PK
    string actor
    string action
    string prev_hash
    string row_hash
  }
  service_clients {
    string id PK
    string name
    string key_hash
  }
  approval_requests {
    string id PK
    string client_id FK
    string action
    string status "pending|approved|denied|expired"
  }
```

### 9d. Workers and AI

```mermaid
erDiagram
  outbox_events ||--o| job_runs : "becomes"
  model_registry ||--o{ ai_route_changes : "changed by"

  outbox_events {
    string id PK
    string job_name
    json payload
    datetime relayed_at
  }
  job_runs {
    string id PK
    string job_name
    string status "queued|running|done|failed"
    int attempts
    text error
  }
  model_registry {
    string task PK
    string model
    bool enabled
  }
  ai_route_changes {
    string id PK
    string task FK
    string proposed_model
    string proposed_by
    string approved_by "second admin"
  }
  integration_cursors {
    string name PK
    string position "audit forwarder cursor"
  }
```

## 10. Where each store sits

```mermaid
flowchart LR
  A[Bytes of the PDF / recording] --> FS[(File store)]
  B[Records, status, people, audit] --> PG[(PostgreSQL)]
  C[Vectors + ACL payload for search] --> QD[(Qdrant)]
  D[Jobs in flight] --> RD[(Redis)]
```
