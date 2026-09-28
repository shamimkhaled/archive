# Memory & Knowledge Layer

**Requirements (normative):** [REQUIREMENTS.md](./REQUIREMENTS.md) — Capabilities **A–E** (R3–R7), Decision Intelligence (R6), Governed AI (R7)

**Purpose:** Describe how institutional memory must work per the EOI, and how the codebase stores/retrieves it today.

PDF north star: move from file searching to **questioning institutional memory**, with **No Source / No Answer** and **No Permission / No Retrieval**.

---

## 1. What “memory” means here

Institutional memory is not a single database table. It is a layered store:

| Layer | Store | Role |
|---|---|---|
| **Authoritative record** | PostgreSQL `documents`, meeting tables | Source of truth for IDs, metadata, grants, audit |
| **Binary fidelity** | S3 / local object storage | Unchanged PDF bytes |
| **Structured understanding** | `documents.summary_json` + LLM schema | Org/personnel/projects/finance/keywords |
| **Semantic memory** | Qdrant `document_summaries` + `document_chunks` | Vector recall for hybrid search |
| **Relational memory** | Graph builder over summaries + neighbors | Mind map / related documents |
| **Operational memory** | Meetings, invitations, attendance, notifications | Board process continuity |
| **Ephemeral cache** | Redis | Search/metadata/graph cache; job queue; locks |

Design rule from the PDF: **No Source, No Answer** and **No Permission, No Retrieval**. Retrieval and (future) generation must respect grants and cite pages.

---

## 2. Document memory lifecycle

```
PDF upload
  → parse (llama-parse → pypdf → OCR eng/ben)
  → LLM DocumentSummary
  → store binary (S3/local)
  → upsert summary vector (parent)
  → enqueue/run chunk_index (children)
  → Postgres DocumentRecord + audit
  → bump Redis search cache version
```

### Structured summary schema

Produced by `summary_extractor.DocumentSummary`:

- `core_info` — title, org, subject, dates, etc.  
- `key_personnel` — names/roles  
- `major_projects` — project references  
- `finance_and_admin` — amounts / admin notes  
- `searchable_keywords` — bilingual EN/BN terms  

Embedding text for the parent vector is enriched from these fields (`qdrant_store.build_summary_embedding_text`) so semantic search aligns with how bank staff describe documents.

### Parent / child vectors

| Collection | Unit | Use |
|---|---|---|
| `document_summaries` | One point per `doc_id` | Document-level recall & ranking |
| `document_chunks` | Token/whitespace chunks with `parent_doc_id` | Page/passage recall & citation anchors |

Hybrid search fuses both with **RRF** (Reciprocal Rank Fusion), then enriches each hit with the caller’s access flags.

---

## 3. Access-aware memory

Memory is useless if it leaks. Access is applied **after** cache reads and **before** view/download:

- Privileged view: `admin`, `board_secretary`  
- Download: `admin` only (always watermarked)  
- `board_member`: approved `DocumentAccessRequest` with optional `expires_at`  
- Search results include `can_view`, `can_download`, `access_status` without exposing denied binaries  

Authorization **never** fail-opens. Cache/email subsystems may fail open without granting PDF access.

---

## 4. Relational memory (mind map)

`graph_builder.py` constructs a lightweight knowledge graph for the archive UI:

- Entity edges from summary fields (org, project, person, keyword)  
- Semantic neighbors among documents  
- APIs: `/api/archive/graph`, `/api/documents/{doc_id}/related`, page `/archive/map`  

This supports Capability **D** (relationship map). A formal **decision registry** (approved → revised → superseded) is not yet a first-class model; chronology today is inferred from dates, links, and search.

---

## 5. Meeting memory

| Artifact | Persistence |
|---|---|
| Meeting metadata | `board_meetings` |
| Agenda text | `board_meetings.agenda` |
| Pack PDFs | `meeting_documents` + object storage |
| Invites / reminder stamps | `meeting_invitations` |
| Attendance + signature file | `meeting_attendance` |
| In-app events | `notification_events` |
| Transcription JSON | `board_meetings.transcription_*` (**dummy AI captions today**) |

Intended end-state (PDF Capability B): draft minutes → approval → action tracking → final AI indexing into the same archive memory. Current code covers setup, pack, conduct (attendance), and notification loops; minutes/actions/real ASR remain roadmap items.

---

## 6. Search as memory interface

| Mode | Endpoint | Behavior |
|---|---|---|
| Hybrid semantic | `GET /api/search?q=&lang=` | Summary + chunk vectors, RRF, Postgres keyword fallback, Redis cache |
| Metadata | `GET /api/search/metadata` | Doc ID / type / uploader / date range |
| Graph | `GET /api/archive/graph` | Relationship exploration |
| Related | `GET /api/documents/{id}/related` | Local cluster around a document |

Lang hints: `en`, `bn`, `any` (invalid values ignored).

---

## 7. Async indexing memory durability

Chunk embedding can be heavy. Two modes:

1. **In-process** — FastAPI `BackgroundTasks` (default when `ENABLE_JOB_QUEUE=0`).  
2. **Durable queue** — Redis lists (`bcp:jobs` / processing / dead) + `python -m bcp_project.jobs.worker`.

Job type today: `chunk_index` with `{doc_id, page_payloads}`. Failed jobs retry (max attempts) then dead-letter.

---

## 8. Ports: swappable memory backends

Adapters isolate bank-integrator concerns (`ports/protocols.py`):

| Port | Responsibility |
|---|---|
| `LlmPort` | Embeddings + summary extraction |
| `VectorStorePort` | Qdrant collections / upsert |
| `ObjectStoragePort` | PDF store/probe |
| `EmailPort` | Meeting mail |

This enables future private/local LLM or alternate vector stores without rewriting routers—aligned with the PDF’s data-sovereignty guidance.

---

## 9. Audit as memory of use

`audit_logs` records who did what to which resource (login, upload, view, download, access review, dummy transcription events). Combined with watermarks, this is the integrity trail for controlled records.

---

## 10. Roadmap for governed generative memory (Capability E)

Prerequisites already in place: hybrid retrieval, access enrichment, page-oriented chunks, audit.

Still required for “Ask Sonali Bank”:

1. Permission-filtered retrieval set before any LLM call  
2. Answer generation that refuses when retrieval empty  
3. Mandatory document ID + page citations in the response schema  
4. Human-in-the-loop UI (confirm / copy / escalate)  
5. Append-only log of AI queries and cited sources  

Until then, treat search + mind map + structured summaries as the production memory interface.
