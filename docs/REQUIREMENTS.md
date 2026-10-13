# Requirements (from EOI)

**Source of truth:** `Sonali_Bank_Intelligent_Memory_Platform.pdf`  
**Document type:** Confidential explanatory Expression of Interest (EOI)  
**Date:** 1 September 2026  
**Prepared by:** Alawaf Technologies Limited (Solution Designer & System Integrator)  
**Customer:** Sonali Bank PLC  

This file extracts **product requirements** from the PDF **before** mapping them to the current codebase. Implementation status lives in [ROADMAP.md](./ROADMAP.md) and [PRD.md](./PRD.md).

---

## R0 — Product identity

| ID | Requirement |
|---|---|
| R0.1 | Product name: **Sonali Bank Intelligent Memory Platform** |
| R0.2 | Bangla framing: *সোনালী ব্যাংক পিএলসি-এর জন্য বুদ্ধিমান ডিজিটাল আর্কাইভ ও প্রাতিষ্ঠানিক জ্ঞান প্ল্যাটফর্ম* |
| R0.3 | Value proposition: transform work from **“file searching”** to **“questioning institutional memory”** |
| R0.4 | Four headline outcomes: Secure Institutional Memory · Source-Backed AI Assistance · Paperless Meetings · Controlled Records |

---

## R1 — Problem / target state

### Current pain (must address)

| ID | Pain |
|---|---|
| R1.1 | Scanned PDFs are often image-only → Bangla/English text not searchable |
| R1.2 | Meeting decisions are disconnected from implementing circulars |
| R1.3 | Finding the **latest approved position** requires reading many long files |
| R1.4 | Knowledge handover gap when officers transfer (location/history of files lost) |

### Target state (must deliver)

| ID | Target |
|---|---|
| R1.5 | **Permission-aware archive** — unified access with airtight security |
| R1.6 | **AI Intelligence Core** — OCR + metadata + full-text + semantic search |
| R1.7 | **Decision Registry** — automatic links between decisions and related documents |
| R1.8 | **Trusted AI Layer** — “No Source, No Answer” with document + page citations |

---

## R2 — Scope boundary

| ID | Rule |
|---|---|
| R2.1 | Platform is Sonali Bank’s **Knowledge & Archive layer** |
| R2.2 | Platform is **not** a new transaction-processing / CBS system |

### In scope (MUST)

| ID | Capability |
|---|---|
| R2.3 | Approved archival records, policies, circulars, manuals |
| R2.4 | Board/committee meeting agenda, minutes, decisions, **action tracking** |
| R2.5 | Project, ICT, procurement, audit, compliance records |
| R2.6 | Permission-aware **“Ask Sonali Bank” AI** + employee productivity assistance |

### Explicitly out of scope (MUST NOT)

| ID | Exclusion |
|---|---|
| R2.7 | Replace CBS or perform transaction processing |
| R2.8 | Loan origination, credit scoring, automated lending |
| R2.9 | Payment switch, card, internet banking, treasury transactions |
| R2.10 | Uncontrolled public AI or autonomous decision-making on confidential data |

---

## R3 — Five core capabilities (MUST)

| ID | Pillar | Requirement statement |
|---|---|---|
| R3.A | Intelligent Digital Archive | Store and organize paper/digital documents |
| R3.B | Paperless Meeting & Decision Memory | Transform meeting → decision → final record |
| R3.C | Knowledge Search | Accurate search via keyword, metadata, and semantic meaning |
| R3.D | File & Decision Intelligence | Build chronology; analyze **latest position** of documents |
| R3.E | Ask Sonali Bank AI Assistant | Source-backed Q&A and briefing from authorized sources |

---

## R4 — Capability A: Intelligent Digital Archive (lifecycle)

| ID | Stage | Requirement |
|---|---|---|
| R4.1 | Intake & Scan | High-resolution scan of physical files; **keep originals unchanged** |
| R4.2 | OCR & Text Extraction | Mixed **Bengali + English** text extraction with **OCR confidence** |
| R4.3 | Metadata & Classification | Auto-tag Title, Date, Division; **Human QC** step |
| R4.4 | Archival Packaging & Indexing | Searchable PDF + **full-text index** + **vector index** |
| R4.5 | Relationship example | 58-page ICT project file (proposal → tender → decision → completion) must link via OCR, dates, relationship mapping so officers grasp full history from subject/reference search |

---

## R5 — Capability B: Paperless Meeting & Decision Memory

| ID | Stage | Requirement |
|---|---|---|
| R5.1 | Meeting Setup | Committee, venue, confidentiality |
| R5.2 | Agenda & Secure Pack | View-only policy, **dynamic watermark**, controlled release |
| R5.3 | Conduct Meeting | Capture attendance and decisions |
| R5.4 | Draft Minutes & Approval | Structured review, **version control**, final approval |
| R5.5 | Action Tracking | Responsible unit, due date, **completion evidence** |
| R5.6 | Final AI Indexing | Approved decisions enter institutional archive as permanent memory |

---

## R6 — Capability C & D: Knowledge Search & Decision Intelligence

| ID | Requirement |
|---|---|
| R6.1 | **Metadata search** (e.g. ICT Division approved projects 2023–2026) |
| R6.2 | **Full-text (OCR)** search for phrases inside scanned reports |
| R6.3 | **Semantic search** for similar policies/cases without exact keywords |
| R6.4 | **Relationship map** showing links among documents |
| R6.5 | Lifecycle: Policy Proposal → Committee Meeting → Approved Decision → Circular → Revised Decision (feedback loop) |
| R6.6 | System **automatically shows** which record is **superseded** vs **latest authoritative** |

---

## R7 — Capability E: Ask Sonali Bank (Governed AI)

| ID | Principle | Requirement |
|---|---|---|
| R7.1 | Policy | AI is **not** final authority; support tool only; answers only from authorized sources |
| R7.2 | No Source, No Answer | No hallucinated answers when authorized evidence is insufficient; every substantive answer **must** include Document + Page citations |
| R7.3 | No Permission, No Retrieval | AI must not retrieve/summarize/show files the user is not authorized to access |
| R7.4 | Human in the Loop | AI may prepare summaries, chronologies, briefings; humans retain final decisions, approvals, interpretive explanations |

---

## R8 — Personas & AI outcomes (MUST support)

| ID | User group | Need | AI-powered outcome |
|---|---|---|---|
| R8.1 | Senior Management | Fast context on pending issues | One-page briefing (background + unresolved points) before a meeting |
| R8.2 | Board/Committee Secretariat | Secure packs + action tracking | Real-time pending decisions/actions from last ~3 meetings |
| R8.3 | Division Heads | Prior approvals / precedents | Comparative analysis of historical decisions vs new proposal |
| R8.4 | General Officers | Long files → notes | Chronology + cited summary synthesized across multiple documents |

---

## R9 — Technical architecture (MUST)

Scalable & deployment-neutral stack:

| ID | Layer | Requirement |
|---|---|---|
| R9.1 | Data sources | Paper/digital, legacy archives, meetings |
| R9.2 | Archive processing | High-res scan, mixed-language OCR, metadata QC |
| R9.3 | Data & search DB | Object storage + vector index + full-text engine |
| R9.4 | Knowledge layer | Object relationships, versioning, source links |
| R9.5 | AI intelligence | Local/private models for Q&A, summaries, compare |
| R9.6 | Identity & security | SSO/AD, RBAC, append-only audit logs |
| R9.7 | User experience | Secure web viewer + admin dashboard |

---

## R10 — Security, access governance & data sovereignty (MUST)

| ID | Area | Requirement |
|---|---|---|
| R10.1 | Identity & access | RBAC/ABAC; MFA readiness; division/committee scoping; AI only sees user-authorized docs |
| R10.2 | Local/private AI option | OCR + LLM hostable in bank-controlled local/private cloud; reduce external AI API dependency for sensitive data |
| R10.3 | Secure viewer | Dynamic watermarking; download/print controls |
| R10.4 | Audit & integrity | Append-only audit from login → search → AI query → page view; **checksum** verification |

---

## R11 — 90-day pilot plan (delivery requirements)

| ID | Phase | Weeks | Deliverables |
|---|---|---|---|
| R11.1 | Discover & Design | 1–2 | Policy selection, taxonomy, security matrix |
| R11.2 | Archive Foundation | 3–5 | Pilot ingest, scan/import, OCR, metadata |
| R11.3 | Search + AI Intelligence | 6–8 | Full-text search, relationship map, **Ask Sonali** |
| R11.4 | Paperless Meeting | 9–10 | One committee lifecycle: agenda → archive |
| R11.5 | Validate & Handover | 11–12 | UAT, security review, pilot report |

---

## R12 — Pilot success KPIs (acceptance)

| ID | KPI | Acceptance bar |
|---|---|---|
| R12.1 | Record findability | Significant reduction in file-find time vs baseline |
| R12.2 | OCR searchability | ≥ **95%** of good-quality pilot scans searchable |
| R12.3 | Source-backed AI | **100%** of substantive AI answers have Document + Page citation |
| R12.4 | Zero exposure | **Zero** unauthorized sensitive-document exposure in pen-test / access-control tests |

---

## R13 — Governance & next steps

| ID | Requirement |
|---|---|
| R13.1 | Joint bank sponsorship + Alawaf SI operating model |
| R13.2 | Human-in-the-loop always retained for AI activities |
| R13.3 | Post-pilot: phased expansion to divisions/regional offices |
| R13.4 | Legacy migration at scale |
| R13.5 | Retention automation + Legal Hold |
| R13.6 | Immediate: discovery (record survey, volume, infrastructure sizing), joint working group, BOQ/commercial, EOI completion |

---

## Post-demonstration evaluation 3.1 and 3.2 — evidence

Where each requirement from the post-demonstration report (sections 3.1 and 3.2, workflows 6.1 and
6.2) is met, and how it is shown. Sections 3.3 and 3.4 were already met; see PRD.md.

### 3.1 OCR-based digital archive

| Requirement | Code | Tests | Measured evidence |
|---|---|---|---|
| Upload scans, PDFs, images, letters, circulars, office orders, memos, Board papers | `intake.validate_upload`, `document_types.DEFAULT_DOCUMENT_TYPES` (includes Office Order) | `test_phase4_ocr.py` | — |
| Bangla and English OCR with measurable accuracy | `ocr.py` (Tesseract `ben+eng`, line recovery, legacy Bijoy detection) | `test_phase4_ocr.py` | `eval/reports/ocr-synthetic.json` (120 synthetic pages, three scan qualities): CER 0.05% English, 1.8% Bangla (2.5% by grapheme cluster), Bangla WER 7.9%; `docs/evidence/benchmark-report.md` |
| Extract text and metadata (title, reference, date, department, subject, category, keywords) | `services/metadata_suggest.py` (AI with rules fallback) | `test_metadata_rules_fallback_extracts_reference` | `scripts/bench_accuracy.py metadata` |
| Classify, index, and tag automatically; original unchanged | `metadata_suggest.apply_suggestions` fills empty fields and classifies "Other" uploads; `intake.store_write_once` (never overwrites) | `test_apply_suggestions_fills_only_empty_fields_and_classifies_other`, `test_clean_text_pdf_auto_applies_metadata`, `test_store_write_once_local` | — |
| Full-text search, version history, role-based access, audit trail | search routers, `services/document_versions.py`, `access_control.py`, hash-chained `AuditLog` | `test_clean_scan_indexes_directly_and_new_version_supersedes`, `test_seal_and_verify_chain` | — |
| Human verification for low-confidence OCR **and metadata** | `services/ingest.run_document_ingest` routes to `/review` with reasons; `ocr.bangla_suspect_words`; `metadata_suggest.metadata_review_reasons` | `test_low_confidence_scan_waits_for_review_and_stays_hidden`, `test_suspect_bangla_page_routes_to_review_despite_high_confidence`, `test_type_mismatch_sends_clean_pdf_to_review`, `test_metadata_review_reasons` | Bangla routing catches 3 of 3 pages above 5% CER, flags 8 of 60 Bangla pages and 0 of 60 English pages (`ocr-synthetic.json`, `review_routing`) |

Review reasons shown to the reviewer: low OCR confidence, Bangla words misread as Latin characters,
AI metadata unavailable, title missing, AI document type differs from the one entered, AI date
differs from the one entered. Metadata review is on by default (`METADATA_REVIEW=off` keeps
auto-apply only). A rules fallback caused by the egress policy is not a review reason, since it
is expected for restricted documents.

OCR review thresholds (POC defaults, tune on bank samples):

| Setting | Default | Effect |
|---|---|---|
| `OCR_REVIEW_THRESHOLD` | 75 | Mean word confidence below this sends the upload to review |
| `OCR_PAGE_REVIEW_THRESHOLD` | 60 | Any page below this sends the upload to review |
| `OCR_BANGLA_PAGE_REVIEW_THRESHOLD` | page threshold (92 recommended) | Stricter page bar for mostly-Bangla pages |
| `OCR_BANGLA_SUSPECT_WORDS` | 1 | A Bangla page with this many Latin/ASCII junk words goes to review (0 = off) |
| `OCR_BANGLA_SUSPECT_WORD_CONF` | 50 | Only junk words below this word confidence count |

### 3.2 Meeting minutes (speech-to-text)

| Requirement | Code | Tests | Measured evidence |
|---|---|---|---|
| Recorded audio/video and live transcription | `routers/meeting_minutes.py` upload, `meeting_audio.py`, live chunk STT in `jobs/handlers.py` | `test_recording_upload_stores_hashes_and_queues_transcription` | — |
| Bangla and English speech recognition | `asr.py` (faster-whisper; per-file language and model), `ai.transcribe` routes | `test_phase_a_transcription.py` | `eval/reports/stt-synthetic.json` (see below) |
| Speaker identification where possible | `diarization.py` (pyannote or bank-hosted HTTP), status on the admin AI page and `/readyz`; worker image `WITH_DIARIZATION=1` with `requirements-diarization.txt` | `test_assign_speakers_by_greatest_overlap_and_rename`, `test_diarization_failure_keeps_transcript`, `test_diarization_status_reports_configuration_without_loading_models` | Speaker-attributed WER (cpWER) in `stt-synthetic.json`; diarizer accuracy not yet measured (see below) |
| Transcript, discussion points, decisions, actions, responsible persons/**divisions**, target dates | `minutes_draft.py` (Discussion points section; Action / Owner / Responsible division / Due table), `services/minutes.extract_decisions_and_actions` (quote-grounded), `MeetingAction.division` | `test_extraction_keeps_only_items_quoted_from_the_transcript`, `test_llm_minutes_prompt_asks_for_discussion_points_and_division`, `test_decisions_lock_with_minutes_but_action_status_stays_open` | — |
| AI-assisted draft minutes in the organisational template | `minutes_draft.generate_minutes_markdown`, `minutes_pdf.py` | `test_generate_minutes_template_fallback`, `test_minutes_pdf_maps_every_word_to_a_page_and_keeps_bangla` | — |
| Authorised review, correction, approval (four-eyes) | `services/minutes.py` workflow | `test_four_eyes_rule`, `test_edit_marks_source_edited_and_lock_blocks_changes` | — |
| Archive audio/video, transcript, draft and approved minutes **as linked records** | `services/meeting_records.py`, `MeetingRecord` (migration `0012_meeting_records`), `GET /api/meetings/{id}/records`, Archive records panel on the meeting page | `test_meeting_records_seal_recording_transcript_and_every_minutes_version` | Every record is write-once with its SHA-256; transcript is archive document `TRN-<id>`, approved minutes `MIN-<id>` |

Speech-to-text evidence so far (`eval/reports/stt-synthetic.json`; Whisper `small`, CPU int8;
eight synthetic two-speaker meetings from `make_synthetic_benchmarks.py --sets stt`):

| Slice | Files | WER | Speaker-attributed WER, perfect turns | Real-time factor |
|---|---|---|---|---|
| English | 2 | 14.8% | 14.8% | 0.14 |
| Bangla (espeak-ng voice) | 2 | 119% | 120% | 2.85 |
| One speaker Bangla, one English | 4 | 80% | 79% | 0.88 |

What this shows and what it does not:

- Speaker attribution adds no error on top of recognition when turns are separated by short
  pauses: with perfect speaker turns, speaker-attributed WER equals plain WER for English.
- espeak-ng's Bangla voice is not intelligible to Whisper (it returns noise and emoji), so the
  Bangla rows measure the synthetic voice, not the product. The mixed rows show that Whisper,
  left to detect the language, picks one language for the whole file; real code-switched board
  meetings must be measured before choosing between forcing `WHISPER_LANGUAGE=bn` and a larger model.
- The diarizer itself (pyannote) is not measured yet: its model is gated on Hugging Face and needs
  the bank's token, or the bank-hosted `DIARIZATION_URL` service.
- Credible numbers need 30–60 minutes of real, consented board-room recordings per language with
  typed speaker turns, in the manifest format of `eval/benchmarks/README.md`. Target: Bangla WER
  ≤ 25% (`docs/evidence/benchmark-report.md`, section 5).

---

## Traceability to other docs

| Doc | Role vs this file |
|---|---|
| [PRD.md](./PRD.md) | Product narrative + requirement → feature mapping |
| [DESIGN.md](./DESIGN.md) | UX for pillars A–E |
| [MEMORY.md](./MEMORY.md) | How institutional memory is stored/retrieved |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Layer mapping R9 → software components |
| [ROADMAP.md](./ROADMAP.md) | R11 phases + codebase Done/Partial/Next |
| [API.md](./API.md) | HTTP surface (implementation) |
| [INTEGRATION.md](./INTEGRATION.md) | External systems for R9/R10 |
| [DEPLOYMENT.md](./DEPLOYMENT.md) | Run/pilot deploy for R11 |
