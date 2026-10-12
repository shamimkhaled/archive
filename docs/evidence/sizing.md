# Hardware sizing and cost (Dev, UAT, Production, DR)

Section 9 items covered here: Hardware Sizing, Infrastructure Cost Estimate, Software/License Cost.
Figures are planning estimates for procurement, to be confirmed against the POC load test
(`docs/CAPACITY.md`) and the bank's vendor quotes. Currency is USD; hardware prices are
indicative street prices in 2026 and vary with vendor and import duty.

## 1. Sizing basis

| Input | Year 1 | Year 3 |
|---|---|---|
| Documents in archive | 5,000 | 25,000 |
| Average original size | 5 MB (cap 50 MB) | same |
| Versions and image originals | +50% storage | same |
| Vectors per document | ~25 chunks + 1 summary | same |
| Named users | 80 | 200 |
| Concurrent users (meeting day peak) | 30 | 100 |
| Meetings with recordings | 4 / month, 3 h each | 10 / month |
| Ask Sonali questions | 1,000 / month | 5,000 / month |

Derived: originals ≈ 40 GB in year 1, ≈ 190 GB by year 3. Vectors ≈ 130k (year 1) and 650k
(year 3); at 1024 dimensions with HNSW overhead that is ≈ 1 GB and ≈ 5–6 GB of Qdrant RAM
(≈ 1.5× more at 1536 dimensions). Postgres stays under 20 GB including audit rows.

## 2. Railway (current production path, cloud AI)

| Service | Dev | UAT | Production | DR |
|---|---|---|---|---|
| web | 1 × 1 vCPU / 2 GB | 1 × 2 vCPU / 4 GB | 2 × 2 vCPU / 4 GB | 1 × 2 vCPU / 4 GB (warm) |
| outbox-relay | shared with web | 1 × 0.5 vCPU / 512 MB | 1 × 0.5 vCPU / 512 MB | 1 × 0.5 vCPU / 512 MB |
| worker-ingest | 1 × 2 vCPU / 4 GB | 1 × 2 vCPU / 8 GB | 1–3 × 2 vCPU / 8 GB (autoscaled) | 1 × 2 vCPU / 8 GB |
| worker-meeting | shared | 1 × 1 vCPU / 2 GB | 1 × 2 vCPU / 4 GB | scaled to 0 until failover |
| worker-notify | shared | 1 × 0.5 vCPU / 512 MB | 1 × 0.5 vCPU / 512 MB | 1 × 0.5 vCPU / 512 MB |
| Postgres (Neon) | free / 0.25 CU branch | 0.5 CU | 1–2 CU, PITR 7 days | read replica or restored branch in second region |
| Qdrant Cloud | 1 GB | 1 GB | 4 GB | snapshot restore or rebuild from Postgres + objects |
| Redis (persistence on) | 256 MB | 256 MB | 1 GB | separate instance |
| Object storage (S3 / R2) | 10 GB | 50 GB | 200 GB, versioning + Object Lock | cross-region replica |

**Monthly cost (indicative).**

| Item | Dev | UAT | Production | DR |
|---|---|---|---|---|
| Railway compute | $10–20 | $40–70 | $120–220 | $40–80 |
| Neon Postgres | $0–19 | $19 | $69–150 | $20–70 |
| Qdrant Cloud | $0 (free tier) | $25 | $60–120 | $0–60 |
| Redis | $0–10 | $10 | $20–40 | $10–20 |
| Object storage + egress | $1 | $3 | $10–40 | $5–20 |
| Cloud AI (tokens + minutes) | $5 | $10–20 | $40–120 | usage-based |
| Email (Resend) | $0 | $0 | $20 | $0 |
| **Total** | **≈ $20–60** | **≈ $110–150** | **≈ $340–710** | **≈ $75–320** |

Cloud AI estimate (list prices, October 2026): ingest ≈ 20k tokens per document for metadata and
summary on `gpt-4.1-mini` (≈ $0.01 per document), Ask ≈ 10k tokens per question (≈ $0.005),
embeddings negligible, speech to text ≈ $0.006 per minute (≈ $4–11 per 3-hour meeting).

## 3. Bank data centre with on-prem AI (target)

### Application and data tier (VMs on the bank's virtualisation platform)

| Role | Dev | UAT | Production (per node) | DR (per node) |
|---|---|---|---|---|
| web | 1 VM 4 vCPU / 8 GB | 2 VM 4 vCPU / 8 GB | 2–3 VM 4 vCPU / 8 GB | 2 VM 4 vCPU / 8 GB |
| workers (ingest, meeting, notify, relay) | same VM as web | 1 VM 8 vCPU / 16 GB | 2 VM 8 vCPU / 16 GB | 1 VM 8 vCPU / 16 GB |
| PostgreSQL 16+ | 1 VM 2 vCPU / 4 GB | 1 VM 4 vCPU / 8 GB / 100 GB | primary + sync standby, 8 vCPU / 32 GB / 500 GB SSD | async standby, same size |
| Qdrant | container | 1 VM 4 vCPU / 8 GB | 1–3 VM 8 vCPU / 32 GB / 200 GB SSD | 1 VM, same size |
| Redis (AOF) | container | 1 VM 2 vCPU / 4 GB | primary + replica + Sentinel, 2 vCPU / 8 GB | 1 VM |
| Object storage (MinIO, Object Lock) | 100 GB | 500 GB | 4-node erasure-coded, 10 TB raw (≈ 5 TB usable) | 4 nodes, replicated bucket |
| ClamAV | container | 1 VM 2 vCPU / 4 GB | 2 VM 2 vCPU / 4 GB | 1 VM |
| Backup target | — | NAS share | immutable backup repository, 2× usable data, 35-day retention | receives replicated backups |

### AI tier (GPU)

| Item | Dev / UAT | Production | DR |
|---|---|---|---|
| GPU servers | 1 server, 1 × NVIDIA L40S 48 GB (or RTX 6000 Ada) | 1 server, 2 × L40S 48 GB: GPU 0 vLLM Qwen2.5-14B; GPU 1 TEI bge-m3 + reranker + Whisper large-v3 | 1 server, 2 × L40S (same as production) or accept degraded AI during DR |
| Server spec | 32 cores, 256 GB RAM, 2 TB NVMe | 2 × 32 cores, 512 GB RAM, 2 × 3.84 TB NVMe, 2 × 25 GbE | same as production |
| Growth path | — | 4 × L40S or 2 × H100 80 GB for Qwen2.5-32B or more concurrent Ask users | — |

Expected throughput on 1 × L40S for the 14B model in FP16 with prefix caching: ≈ 25–40 tokens/s
per stream and ≈ 300–600 tokens/s aggregate at 16–32 concurrent streams. That covers 100 concurrent
users with Ask p95 under 8 s when answers stay under ~400 tokens. `scripts/local_ai_check.py --load`
measures the real figure on delivered hardware.

### One-time hardware cost (indicative)

| Item | Production | DR | UAT / Dev |
|---|---|---|---|
| GPU server (2 × L40S) | $45,000–60,000 | $45,000–60,000 | $20,000–28,000 (1 GPU) |
| MinIO nodes (4 × 2U, 10 TB raw) | $24,000–32,000 | $24,000–32,000 | VM storage |
| VMs on existing virtualisation (≈ 60 vCPU, 200 GB RAM) | bank capacity | bank capacity | bank capacity |
| Load balancer / WAF, HSM for backup key | existing bank estate | existing bank estate | — |
| **Total new hardware** | **≈ $70,000–92,000** | **≈ $70,000–92,000** | **≈ $20,000–28,000** |

Running cost: power and cooling for each GPU server ≈ 1.5–2 kW under load; annual hardware support
≈ 10–15% of purchase price.

## 4. Software and licence cost

Every runtime component is open source with a permissive or weak-copyleft licence; there is no
per-user, per-core or per-document licence fee. Inventory with licence per package:
[`licenses.md`](licenses.md).

| Component | Licence | Licence cost | Optional paid support |
|---|---|---|---|
| Application (this codebase) | bank-owned | — | — |
| Python, FastAPI, SQLAlchemy, React, Vite | MIT / BSD / PSF | 0 | — |
| PostgreSQL | PostgreSQL | 0 | EDB or similar, if the bank requires vendor support |
| Qdrant | Apache-2.0 | 0 | Qdrant Hybrid Cloud / Enterprise |
| Redis 7.2 (or Valkey) | BSD-3 (Redis ≤ 7.2) / BSD-3 (Valkey) | 0 | — |
| MinIO | AGPL-3.0 (used unmodified as a separate service) | 0 | MinIO commercial licence if bank policy rejects AGPL |
| Tesseract, Poppler | Apache-2.0 / GPL-2.0 (separate binaries) | 0 | — |
| vLLM, TEI, faster-whisper | Apache-2.0 / Apache-2.0 / MIT | 0 | NVIDIA AI Enterprise (optional) |
| Qwen2.5-14B-Instruct, bge-m3, bge-reranker-v2-m3, Whisper large-v3 | Apache-2.0 / MIT / Apache-2.0 / MIT | 0 | — |
| ClamAV | GPL-2.0 (separate service) | 0 | — |
| Operating system | RHEL / Ubuntu LTS | bank standard | bank standard |

Cloud phase subscriptions (Railway, Neon, Qdrant Cloud, Upstash, OpenAI/OpenRouter, Resend) are
usage-billed services, not licences, and are in section 2.
