# Product Requirements Document (PRD)

**Product:** Sonali Bank Intelligent Memory Platform  
**Requirements source:** [REQUIREMENTS.md](./REQUIREMENTS.md) ← extracted from `Sonali_Bank_Intelligent_Memory_Platform.pdf` (EOI, 1 Sep 2026)  
**Solution designer:** Alawaf Technologies Limited  
**Org:** Sonali Bank PLC  
**Codebase product name today:** Sonali Bank Archive System (`brand.py`)

> **Order of truth:** PDF EOI → `REQUIREMENTS.md` → this PRD → implementation docs.  
> Do not treat the current repo as the full requirements set.

---

## 1. Vision (PDF)

Transform Sonali Bank work from **“ফাইল খোঁজা” (file searching)** to **“প্রাতিষ্ঠানিক স্মৃতিকে প্রশ্ন করা” (questioning institutional memory)**.

Daily administration, policy, board meetings, and projects generate huge volumes of data. The challenge is not only storage — it is delivering the **right information and prior decisions to authorized persons at the right time**.

### Headline outcomes

1. Secure Institutional Memory  
2. Source-Backed AI Assistance  
3. Paperless Meetings  
4. Controlled Records  

Bangla product framing: *বুদ্ধিমান ডিজিটাল আর্কাইভ ও প্রাতিষ্ঠানিক জ্ঞান প্ল্যাটফর্ম*.

---

## 2. Requirements summary (MUST)

Full IDs: [REQUIREMENTS.md](./REQUIREMENTS.md). Condensed here for product owners.

### Scope

- **IS:** Knowledge & Archive layer (policies, circulars, board/committee records, project/ICT/audit docs, permission-aware Ask Sonali Bank).  
- **IS NOT:** CBS, lending automation, payments/card/treasury, uncontrolled public AI, autonomous decisions on confidential data.

### Five pillars

| Pillar | PDF intent |
|---|---|
| **A** Intelligent Digital Archive | Paper/digital ingest → OCR (BN+EN + confidence) → metadata/QC → searchable PDF + full-text + vector index |
| **B** Paperless Meeting & Decision Memory | Setup → secure pack (view-only, dynamic watermark) → conduct → minutes/approval → action tracking → AI indexing into archive |
| **C** Knowledge Search | Metadata + OCR full-text + semantic |
| **D** File & Decision Intelligence | Chronology, relationship map, superseded vs **latest authoritative** record |
| **E** Ask Sonali Bank | Governed AI: No Source/No Answer; No Permission/No Retrieval; Human in the Loop; Document+Page citations |

### Security / sovereignty

RBAC/ABAC + MFA readiness; division/committee scopes; local/private AI option; dynamic watermark + download/print control; append-only audit (login → search → AI query → page view); checksum integrity.

### Pilot KPIs

| KPI | Bar |
|---|---|
| Findability | Material time reduction vs baseline |
| OCR searchability | ≥ 95% of good-quality pilot scans |
| Source-backed AI | 100% substantive answers cited (doc + page) |
| Zero exposure | Zero unauthorized leaks in pen-test / ACL tests |

---

## 3. Personas (PDF)

| Persona | Need | Required AI / product outcome |
|---|---|---|
| Senior management | Pending-issue context | One-page briefing before meetings |
| Board/committee secretariat | Secure packs + actions | Pending decisions/actions across recent meetings |
| Division heads | Precedents / prior approvals | Comparative analysis vs new proposal |
| General officers | Long files → notes | Multi-doc chronology + cited summary |

---

## 4. Functional requirements by pillar

### 4.A Archive ingest (R4)

1. Accept scanned and born-digital PDFs; preserve original binary unchanged.  
2. Extract mixed Bangla/English text; record OCR confidence.  
3. Classify metadata (title, date, division) with human QC.  
4. Package searchable PDF; update full-text and vector indexes.  
5. Support relationship linking across multi-document project files.

### 4.B Meetings (R5)

1. Setup: committee, venue, confidentiality.  
2. Secure pack: view-only, dynamic watermark, controlled release.  
3. Conduct: attendance + decision capture.  
4. Minutes: structured review, version control, approval.  
5. Actions: unit, due date, completion evidence.  
6. Final AI indexing of approved decisions into institutional memory.

### 4.C / 4.D Search & decision intelligence (R6)

1. Metadata, OCR full-text, and semantic search.  
2. Relationship visualization (proposal ↔ meeting ↔ decision ↔ circular ↔ revision).  
3. Explicit superseded vs latest-authoritative marking.

### 4.E Ask Sonali Bank (R7)

1. Answers only from authorized sources.  
2. Refuse when evidence insufficient.  
3. Enforce permissions at retrieval time.  
4. Mandatory Document + Page citations.  
5. Humans remain the decision authority.

---

## 5. Non-functional requirements

| Area | Requirement (PDF) |
|---|---|
| Language | Bangla + English OCR and knowledge access |
| Deployment | Scalable, vendor-neutral architecture |
| Identity | SSO/AD path; RBAC/ABAC; MFA-ready |
| AI hosting | Local/private model option for sensitive workloads |
| Audit | Append-only; includes AI queries and page views |
| Integrity | Checksums on stored objects |
| Governance | Bank sponsorship + SI; human-in-the-loop always |

---

## 6. Implementation coverage (codebase vs PDF)

Honest gap analysis for planning — **not** a substitute for REQUIREMENTS.md.

| Requirement area | Codebase today |
|---|---|
| PDF ingest, BN/EN OCR path, LLM summary, vector + hybrid search | Present |
| Watermarked secure viewer; request-gated access; RBAC roles | Present |
| Board meetings, packs, invites, reminders, digital attendance | Present |
| Mind map / related documents | Present (partial vs full decision registry) |
| OCR confidence + formal Human QC workflow | Gap |
| Division metadata / ABAC scopes; SSO/AD; MFA | Gap |
| Minutes versioning, action tracking, completion evidence | Gap |
| Superseded vs latest authoritative registry | Gap |
| Ask Sonali Bank chat + mandatory citations | Gap |
| Local/private LLM packaging | Ports ready; ops packaging gap |
| Checksums; AI-query audit; print controls | Partial / gap |
| Meeting transcription | Dummy demo only |

Detailed phase tags: [ROADMAP.md](./ROADMAP.md).

---

## 7. Related documents

| Doc | Use |
|---|---|
| [REQUIREMENTS.md](./REQUIREMENTS.md) | Normative EOI requirement IDs |
| [DESIGN.md](./DESIGN.md) | UX for pillars |
| [MEMORY.md](./MEMORY.md) | Institutional memory model |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Layer → component map |
| [ROADMAP.md](./ROADMAP.md) | 90-day pilot + gaps |
| [API.md](./API.md) | Current HTTP API |
| [INTEGRATION.md](./INTEGRATION.md) | External systems |
| [DEPLOYMENT.md](./DEPLOYMENT.md) | Run & deploy |
