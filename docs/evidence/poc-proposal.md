# Proof of concept proposal

Section 9 item covered here: POC Proposal.

## 1. Objective

Show on Sonali Bank's own documents, recordings and network that the archive meets the
post-demonstration requirements: accurate Bangla and English capture, governed AI search and
answers with citations, a complete Board meeting cycle, and bank-grade security and recovery. The
outcome is a measured go / no-go for production, not a demo.

## 2. Scope

| In scope | Out of scope (production phase) |
|---|---|
| UAT deployment inside the bank network (or Railway with bank approval for non-restricted samples) | Full back-file migration |
| SSO through the bank's AD (LDAP or OIDC), MFA for privileged roles | Core banking write integrations |
| SIEM forwarding to the bank's collector | Retention schedule and disposal workflow (`storage-retention.md` section 3) |
| 1,000–2,000 sample documents, including Bijoy-font and poor scans | Mobile device management |
| 10 Board or committee recordings | Handwriting recognition |
| Two real meeting cycles run in parallel with the current process | |
| AI on cloud with `restricted` held local, or on one GPU server if available | |
| Restore drill and failover drill on the UAT pair | |

## 3. Success criteria

Measured with the benchmark harness and recorded in `docs/evidence/` (targets from
`benchmark-report.md` section 5, to be agreed at kick-off):

| Area | Criterion |
|---|---|
| OCR | Bangla CER ≤ 3% on clean and scan pages; English ≤ 1%; ≥ 90% of bad pages reach review |
| Speech | Bangla WER ≤ 25% on board-room audio; secretariat confirms minutes drafting time cut by half or more |
| Metadata | Document type ≥ 90% correct; average fields changed per document ≤ 1.5 |
| Briefings | ≥ 95% of points grounded in the cited page; management rates usefulness ≥ 4 of 5 |
| Ask Sonali | ≥ 90% correct answer-or-refuse; **zero** restricted-document leaks |
| Performance | Search p95 ≤ 1.5 s and Ask p95 ≤ 8 s at the agreed concurrent users |
| Security | No high or critical findings open from the bank's security test; audit chain verifies; SIEM receives events |
| Recovery | Restore drill and failover drill pass within the agreed RTO and RPO (proposed RTO 4 h, RPO 15 min) |
| Adoption | Board secretariat completes two meeting cycles in the system |

## 4. Plan (8 weeks)

| Week | Activities | Bank inputs |
|---|---|---|
| 1–2 | Kick-off, environment, SSO and SIEM connection, sample selection, ground-truth typing begins | Network access, AD group names, SIEM endpoint, sample documents, two staff for ground truth |
| 3–5 | Ingest samples, run benchmarks, tune thresholds (`OCR_BANGLA_PAGE_REVIEW_THRESHOLD`), first meeting cycle | Recordings, board papers with reference briefings, secretariat time |
| 6–7 | Second meeting cycle, user testing with Board office, load test, security test, drills | Test users per role, security tester |
| 8 | POC report: results against criteria, open issues, production plan and costs | Steering committee session |

## 5. Deliverables

1. UAT environment configured for the bank.
2. Benchmark reports on bank samples (cloud, and on-prem if a GPU is available), with comparison.
3. Restore and failover drill records.
4. Security test findings and fixes.
5. Updated sizing and cost for production from measured load.
6. POC report with go / no-go recommendation.

## 6. Team and governance

| Role | From | Commitment |
|---|---|---|
| Project sponsor | Bank | Steering committee, fortnightly |
| Product owner | Bank (Board secretariat) | 2 days / week |
| Records staff for ground truth | Bank | 2 people, 50% for weeks 1–4 |
| IT infrastructure and security | Bank | As needed; one named contact |
| Delivery lead, engineers | Vendor | Full time |

Risks tracked weekly: sample availability, network approvals, GPU lead time, recording quality.

## 7. Data handling during the POC

Samples stay on bank-controlled storage. If the POC runs on Railway before the GPU server arrives,
only documents classified `public` or `internal` go to cloud AI, under `AI_EGRESS=restricted_local_only`;
`confidential` and `restricted` samples are not loaded until the on-prem models are available
(with no local model configured, their AI steps would be blocked by the egress policy). All copies are deleted at the
end of the POC unless the bank decides to keep the UAT environment.
