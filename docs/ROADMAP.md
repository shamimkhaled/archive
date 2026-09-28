# Roadmap

**Normative requirements:** [REQUIREMENTS.md](./REQUIREMENTS.md) (from EOI PDF)  
**Pilot plan source:** PDF slide — *Low-Risk Implementation* (90 days / 12 weeks)

This roadmap tracks **PDF delivery phases** first, then annotates **codebase coverage**.

---

## Status legend

| Tag | Meaning |
|---|---|
| **Done** | Present in `src/bcp_project` |
| **Partial** | Usable but missing EOI-complete scope |
| **Next** | Required for pilot acceptance |
| **Later** | Post-pilot / expansion (R13) |

---

## Phase 1 — Discover & Design (Weeks 1–2) · R11.1

| Deliverable (PDF) | Req ID | Status | Notes |
|---|---|---|---|
| Policy / corpus selection for pilot | R11.1 | **Next** | Operational (bank) |
| Taxonomy | R11.1, R4.3 | **Partial** | Suggested doc types exist; Division taxonomy incomplete |
| Security matrix | R10, R2 | **Partial** | 4 roles + access requests; ABAC/division/MFA/SSO not done |
| Scope boundary agreed | R2 | **Done** | Documented |

---

## Phase 2 — Archive Foundation (Weeks 3–5) · R11.2

| Deliverable (PDF) | Req ID | Status | Notes |
|---|---|---|---|
| Pilot ingest / import | R4.1 | **Done** | PDF upload; scan hardware workflow external |
| Mixed-language OCR | R4.2 | **Partial** | eng/ben path when Tessdata present; **OCR confidence** not persisted |
| Metadata + Human QC | R4.3 | **Partial** | LLM summary + free-text type; no formal QC queue |
| Searchable PDF + full-text + vector index | R4.4 | **Done** | Parent/chunk Qdrant + Postgres keyword |
| Original binary unchanged | R4.1 | **Done** | Object store keeps source; viewer stamps a copy |
| Relationship linking (project file example) | R4.5, R6.4 | **Partial** | Mind map / related; not full project-file lineage |

---

## Phase 3 — Search + AI Intelligence (Weeks 6–8) · R11.3

| Deliverable (PDF) | Req ID | Status | Notes |
|---|---|---|---|
| Metadata search | R6.1 | **Done** | `/api/search/metadata` |
| Full-text (OCR) search | R6.2 | **Partial** | Hybrid/chunk + ILIKE; dedicated OCR-phrase UX TBD |
| Semantic search | R6.3 | **Done** | Summary + chunk vectors, RRF |
| Relationship map | R6.4 | **Done** | `/archive/map`, graph APIs |
| Superseded vs latest authoritative | R6.5–R6.6 | **Next** | Not first-class |
| **Ask Sonali** governed Q&A | R7, R3.E | **Partial** | `/ask` + `/api/v1/ask` + MCP `ask_sonali`; No Source/No Answer + citations; persona briefings still Next |
| Persona briefings / compare / multi-doc chronology | R8 | **Next** | Depends on Ask Sonali |

**Pilot KPI gates for this phase:** R12.1 findability, R12.3 100% citations (once Ask Sonali ships).

---

## Phase 4 — Paperless Meeting (Weeks 9–10) · R11.4

One committee lifecycle: **agenda → archive**.

| Deliverable (PDF) | Req ID | Status | Notes |
|---|---|---|---|
| Meeting setup (committee/venue/confidentiality) | R5.1 | **Partial** | Title/location/agenda; committee/confidentiality model thin |
| Agenda & secure pack (view-only, dynamic watermark, controlled release) | R5.2 | **Done** | Pack docs + watermark streams |
| Conduct: attendance + decisions | R5.3 | **Partial** | Attendance + signatures **Done**; structured decision capture **Next** |
| Draft minutes, version control, approval | R5.4 | **Next** | |
| Action tracking (unit, due, evidence) | R5.5 | **Next** | |
| Final AI indexing into archive | R5.6 | **Partial** | Manual upload path; automated meeting→archive **Next** |
| Invites / 48h–24h reminders / ICS | — | **Done** | Supports secretariat ops |
| Live transcription | — | **Partial** | Phase B: live chunk STT + WebSocket captions; Phase C: auto minutes draft |

---

## Phase 5 — Validate & Handover (Weeks 11–12) · R11.5

| Deliverable (PDF) | Req ID | Status | Notes |
|---|---|---|---|
| UAT with pilot users | R11.5 | **Next** | |
| Security review (ACL + pen-test → Zero Exposure) | R12.4, R10 | **Next** | |
| Pilot report | R11.5 | **Next** | |
| Docker / Railway packaging | R9 | **Done** | See DEPLOYMENT |
| Unit tests (access/search/jobs/graph) | — | **Partial** | Expand e2e for UAT |

**KPI acceptance (R12):** findability ↓ time · ≥95% OCR searchable · 100% cited AI · zero exposure.

---

## Post-pilot expansion (R13)

| Item | Status |
|---|---|
| Joint bank + Alawaf operating model; human-in-the-loop | Operating principle |
| Expand to divisions / regional offices | **Later** |
| Legacy migration at scale | **Later** (scripts: S3 migrate, reindex) |
| Retention automation + Legal Hold | **Later** |
| Discovery: volume survey, infrastructure sizing, BOQ | Immediate commercial next step |

---

## Suggested engineering order (to close PDF gaps)

1. **Ask Sonali Bank v0** — permission-filtered retrieval; refuse without sources; Document+Page citations (R7, R12.3) — **shipped (v0)**; deepen UX / briefings next  
2. **Decision registry** — superseded / latest authoritative links (R6.6)  
3. **Minutes + actions** models and UI (R5.4–R5.5)  
4. **OCR confidence + Human QC** queue (R4.2–R4.3)  
5. **Checksum + AI-query audit + print controls** (R10.3–R10.4)  
6. **SSO/AD + MFA-ready + division ABAC** (R10.1)  
7. **Private/local LLM option** packaging (R10.2, R9.5)  
8. **Real ASR** — Phases A–C shipped (record + live captions + minutes draft); diarization / room DSP later  

---

## Related docs

- [REQUIREMENTS.md](./REQUIREMENTS.md) — normative IDs  
- [PRD.md](./PRD.md) — product narrative  
- [ARCHITECTURE.md](./ARCHITECTURE.md) — layer map  
- [DEPLOYMENT.md](./DEPLOYMENT.md) — run pilot stack  
- [DR.md](./DR.md) — readiness, multi-queue, recovery  
- [API.md](./API.md) — `/api/v1` including Ask + HITL
