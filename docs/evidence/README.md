# Sonali Bank Archive: evidence pack

Response to section 9 of the post-demonstration report ("documents and evidence to be
submitted"). Each item points to the document that answers it and to the artefact behind it
(code, script, measured result). Measured results carry their date, commit and environment.

| # | Section 9 item | Document | Artefacts and evidence |
|---|---|---|---|
| 1 | Detailed Solution Architecture | [architecture.md](architecture.md) §1 | `src/bcp_project/`, `.railway/railway.ts` |
| 2 | Hardware Sizing (Dev, UAT, Prod, DR) | [sizing.md](sizing.md) §1–3 | `docs/CAPACITY.md`, `loadtest/` |
| 3 | AI/ML Architecture | [ai-models.md](ai-models.md) §1–3, §5 | `src/bcp_project/ai/`, `/admin/ai` |
| 4 | Software / License List | [licenses.md](licenses.md) | `scripts/license_inventory.py` (regenerate on each release) |
| 5 | Cybersecurity Architecture | [security.md](security.md) §1, §6, [network.md](network.md), [hardening.md](hardening.md) | `docs/SECURITY_OPERATIONS.md`, CI security gates and image scan, `deploy/onprem/nginx-archive.conf` (TLS 1.2+), `deploy/onprem/docker-compose.hardening.yml` |
| 6 | Deployment Architecture | [architecture.md](architecture.md) §2, [network.md](network.md) (ports, bandwidth), [monitoring.md](monitoring.md) | `Dockerfile`, `docs/RAILWAY.md`, `deploy/onprem/`, `deploy/prometheus/`, `deploy/grafana/` |
| 7 | High Availability & DR | [../DR.md](../DR.md), [drills/](drills/README.md) | `scripts/dr_backup.py`, `dr_restore.py`, `dr_failover_drill.py`; restore drill PASS |
| 8 | Business Workflow Diagrams | [workflows.md](workflows.md) | State machines in `services/minutes.py`, `services/briefing.py`, `services/ingest.py` |
| 9 | Integration Architecture | [architecture.md](architecture.md) §4 | `docs/INTEGRATION.md`, `src/bcp_project/ports/` |
| 10 | API Documentation | [../API.md](../API.md), [architecture.md](architecture.md) §5 | `frontend/openapi.json` (`scripts/export_openapi.py`) |
| 11 | Data Flow Diagram | [architecture.md](architecture.md) §3 | — |
| 12 | User Access / Authorization Model | [security.md](security.md) §2–4 | `policy.py`, `access_control.py`, `search_acl.py`; leak test in CI |
| 13 | AI Accuracy / Benchmark Report | [benchmark-report.md](benchmark-report.md) | [benchmark-results.md](benchmark-results.md), `eval/reports/*.json`, `scripts/bench_accuracy.py` |
| 14 | Bangla Language Capability | [ai-models.md](ai-models.md) §4, [benchmark-report.md](benchmark-report.md) §2 | Bangla OCR line recovery, legacy Bijoy detection, bilingual retrieval set |
| 15 | Storage & Retention Strategy | [storage-retention.md](storage-retention.md) | `intake.py` (write-once, Object Lock), `services/retention.py` and `/admin/retention` (schedule, legal hold, four-eyes disposal) |
| 16 | Implementation Methodology | [delivery.md](delivery.md) §1 | `.github/workflows/ci.yml` |
| 17 | Project Timeline | [delivery.md](delivery.md) §2 | — |
| 18 | Training & Knowledge Transfer Plan | [delivery.md](delivery.md) §3 | Runbooks listed there |
| 19 | Support & Maintenance Model | [delivery.md](delivery.md) §4 | — |
| 20 | Infrastructure Cost Estimate | [sizing.md](sizing.md) §2–3 | — |
| 21 | Software / License Cost | [sizing.md](sizing.md) §4 | [licenses.md](licenses.md) |
| 22 | POC Proposal | [poc-proposal.md](poc-proposal.md) | `eval/benchmarks/README.md` (sample plan) |

Supporting runbook: [local-ai-cutover.md](local-ai-cutover.md) (cloud → bank GPU, per task, reversible).

## What is measured and what is still open

| Item | Status |
|---|---|
| OCR accuracy, Bangla and English | Measured on a 120-page synthetic set; bank samples needed |
| Retrieval and access control | Measured in CI (100 bilingual queries, 0 leaks) with stub embeddings; real embeddings on bank corpus pending |
| STT, metadata, summary, Ask accuracy | Harness ready; needs bank samples and a UAT instance |
| Restore drill | PASS on Dev, 12 Oct 2026 |
| Failover drill | Script ready; needs a standby environment |
| On-prem AI | Compose stack, gateway and check script ready; needs bank GPU hardware |
| Load test at target concurrency | Scripts ready (`loadtest/`); results table in `docs/CAPACITY.md` to be filled on UAT |
| Monitoring and alerting | Config validated in CI (`promtool` rule unit tests, `amtool`); alert routing to bank email, pager and SOC to be set at install |
| Hardened container profile | Tested on Dev, 12 Oct 2026 ([hardening.md](hardening.md) §7); host CIS checks run by the bank's platform team |
| TLS 1.2+ front end | `nginx-archive.conf` tested on Dev (TLS 1.0/1.1 refused); bank certificate and load balancer at install |
| Records retention periods | Mechanism built and tested; periods come from the bank's records policy |

## Producing a single PDF

Bangla needs a renderer with complex-script shaping, so go through HTML and print from Chromium:

```bash
cd docs/evidence
pandoc README.md architecture.md security.md network.md hardening.md monitoring.md ai-models.md \
  workflows.md storage-retention.md sizing.md licenses.md benchmark-report.md benchmark-results.md ../DR.md drills/README.md \
  delivery.md poc-proposal.md local-ai-cutover.md \
  -s --toc --metadata title="Sonali Bank Archive — Evidence Pack" -o /tmp/evidence-pack.html
chromium --headless --print-to-pdf=sonali-archive-evidence-pack.pdf /tmp/evidence-pack.html
```

Mermaid diagrams render as code blocks unless converted first (for example with `mermaid-filter`).
