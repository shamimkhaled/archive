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
