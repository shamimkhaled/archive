# Network, firewall ports, bandwidth, and encryption

Section 9 items covered here: Network & Security Architecture (port matrix and bandwidth), and the
encryption statement for Security Architecture. Addresses and hostnames are placeholders; the bank's
network team assigns the real ones.

## 1. Zones

| Zone | Contents |
|---|---|
| User LAN | Head office and branch workstations, tablets on the corporate Wi-Fi |
| Edge | Bank load balancer or WAF, then the nginx TLS front end ([`deploy/onprem/nginx-archive.conf`](../../deploy/onprem/nginx-archive.conf)) |
| Application | Web replicas, workers (ingest, meeting, notify), outbox relay |
| Data | PostgreSQL primary and standby, Redis with Sentinel, Qdrant, MinIO, ClamAV |
| AI | GPU server: vLLM, TEI (embeddings and reranker), Whisper, behind the AI gateway |
| Management | Bastion or PAM jump host, Prometheus, Alertmanager, Grafana, backup server |
| Bank services | Active Directory, identity provider (OIDC), SIEM, mail relay, NTP, DNS |
| DR site | The same zones at the standby data centre |

Nothing in the Application, Data or AI zones needs inbound access from the internet. In the on-prem
target no zone needs outbound internet access either, except optionally the notify worker for email
(see row 13).

## 2. Firewall port matrix

All flows are TCP unless stated. "TLS" means the connection is encrypted and the server certificate is
verified against the bank CA.

| # | From | To | Port | Purpose | Encryption |
|---|---|---|---|---|---|
| 1 | User LAN | Edge (nginx / LB VIP) | 443 | Archive web app, PWA, API | TLS 1.2 or 1.3 only |
| 2 | User LAN | Edge | 80 | Redirect to 443 only; no content served | none (redirect) |
| 3 | Edge | Web replicas | 8080 | Proxied HTTP | Application VLAN; TLS re-encryption optional (see section 5) |
| 4 | Web, workers, relay | PostgreSQL | 5432 | Application database | TLS (`ssl=verify-full` in `DATABASE_URL`) |
| 5 | Web, workers, relay | Redis | 6379 (or 6380 with TLS) | Job streams, locks, cache, rate limits | TLS with `rediss://` and AUTH |
| 6 | Web, workers | Redis Sentinel | 26379 | Primary discovery (HA layout) | AUTH; TLS if Sentinel TLS enabled |
| 7 | Web, workers | Qdrant | 6333 | Vector search and indexing (REST) | TLS with API key |
| 8 | Web, workers | MinIO (S3 API) | 9000 | Originals and versions (write-once) | TLS |
| 9 | Web | ClamAV (clamd) | 3310 | Upload virus scan (`AV_SCAN=clamav`) | Data VLAN only; clamd has no TLS |
| 10 | Web, workers | AI gateway | 8088 | LLM, embeddings, reranker, speech to text | Bearer token; AI VLAN only, or TLS on the gateway (section 5) |
| 11 | Web | Active Directory | 636 (LDAPS) or 389 with StartTLS | Sign-in (`LDAP_URL`) | TLS (`LDAP_CA_FILE`) |
| 12 | Web, user browsers | Identity provider | 443 | OIDC sign-in, token and JWKS endpoints | TLS |
| 13 | Notify worker | Email API (`api.resend.com`) | 443 | Meeting invitations and reminders | TLS. On-prem without internet: needs an SMTP adapter (not built) or an egress proxy |
| 14 | Notify worker, web | SIEM | 6514 (syslog TLS) or 443 (HTTPS) | Audit log forwarding (`SIEM_TRANSPORT`) | TLS |
| 15 | Notify worker | Webhook receivers | 443 | Event notifications to bank systems | TLS |
| 16 | Prometheus | Web | 8080 (`/metrics`) | Metrics scrape | Bearer token (`METRICS_TOKEN`); nginx also limits `/metrics` to the monitoring subnet |
| 17 | Prometheus | Workers, relay | 9108 | Metrics scrape | Bearer token |
| 18 | Prometheus | All hosts | 9100 | node-exporter | Management VLAN |
| 19 | Prometheus | Exporters | 9187 (Postgres), 9121 (Redis), 9115 (blackbox) | Database and probe metrics | Management VLAN |
| 20 | Prometheus | Qdrant | 6333 (`/metrics`) | Vector store metrics | TLS |
| 21 | Admin workstations | Grafana, Prometheus, Alertmanager | 3000, 9090, 9093 | Dashboards and alerts | TLS via the management reverse proxy |
| 22 | Alertmanager | Mail relay, SIEM, pager | 587, 443 | Alert delivery | TLS |
| 23 | PostgreSQL primary | PostgreSQL standby (DR) | 5432 | Streaming replication | TLS |
| 24 | MinIO (primary) | MinIO (DR) | 9000 | Bucket replication | TLS |
| 25 | Backup job | Backup repository | 443 or 9000 (S3), or 2049 (NFS) | Encrypted backups (`scripts/dr_backup.py`) | Files are AES-256-GCM encrypted before they leave the host |
| 26 | Bastion / PAM | All servers | 22 | Administration | SSH keys, no passwords |
| 27 | All servers | Bank NTP, DNS | 123/udp, 53/udp+tcp | Time and name resolution | — |

**Cloud phase (Railway).** Users reach Railway's edge on 443. The web service reaches Neon (5432,
TLS), Qdrant Cloud (6333, HTTPS), Upstash Redis (6379, TLS), S3 or R2 (443) and the AI providers
(443). Services talk to each other on Railway's private network. If the bank restricts outbound
traffic from its LAN, it needs to allow only the archive's public hostname on 443.

## 3. Bandwidth

Estimated from the sizing basis in [`sizing.md`](sizing.md) (year 3: 25,000 documents, 100
concurrent users at peak, 5 MB average original). These are planning figures; the UAT load test
measures real ones.

| Link | Peak estimate | How it is derived | Recommended |
|---|---|---|---|
| User LAN to edge (all users) | ≈ 35–40 Mbit/s | 100 users each opening a 5 MB document every 2 minutes ≈ 4.2 MB/s; search, Ask and page traffic adds < 2 Mbit/s | ≥ 100 Mbit/s to the archive VIP |
| One branch user | ≈ 1–2 Mbit/s | Viewer streams with HTTP range requests, so the first page shows before the whole file arrives | Existing branch WAN is enough |
| Bulk upload or migration | ≈ 12 Mbit/s | 1,000 documents per hour × 5 MB | Schedule migration batches out of hours |
| Application to object storage | ≈ 70–100 Mbit/s | Viewing plus OCR workers reading originals | 10 GbE application/data network |
| Application to PostgreSQL | < 10 Mbit/s | Metadata, page text, audit rows | 10 GbE |
| Application to Qdrant | < 5 Mbit/s | Queries return about 100 KB of payload | 10 GbE |
| Application to AI gateway | ≈ 20–50 Mbit/s | Text prompts are small (10–20k tokens ≈ 40–80 KB); meeting audio dominates | 10 GbE; GPU server has 2 × 25 GbE |
| Primary to DR (bandwidth) | ≈ 5–20 Mbit/s steady | WAL stream (< 1 GB/day) plus new originals (≈ 0.2 GB/day on average, up to 5 GB on a migration day) | ≥ 100 Mbit/s dedicated; 1 Gbit/s for initial seeding of 5 TB. A 15-minute RPO needs the link up continuously, not burst size |

**Latency expectations.** The response-time targets in `docs/CAPACITY.md` (search p95 under 1.5 s, Ask
p95 under 8 s, first 256 KB of a document p95 under 2 s) assume these round-trip times:

| Path | Round trip | Why it matters |
|---|---|---|
| Branch or head-office user to the archive | < 50 ms | Each document view is a few requests; higher latency adds directly to the time to first page |
| Application to PostgreSQL, Redis, Qdrant | < 1 ms (same data centre, same or adjacent rack) | A search makes several sequential calls to Postgres and Qdrant; a few milliseconds each would use up the latency budget |
| Application to object storage | < 2 ms | OCR workers and the viewer read originals in many range requests |
| Application to AI gateway | < 2 ms | Small compared with model time (seconds), but streaming answers are sent token by token |
| Primary to DR | < 50 ms | WAL streaming is asynchronous, so latency affects RPO lag rather than user response time |

## 4. Encryption

### In transit

| Path | Mechanism | Enforced by |
|---|---|---|
| Browser to archive | TLS 1.2 or 1.3, ECDHE with AES-GCM or ChaCha20; TLS 1.0/1.1 refused; HSTS one year | `deploy/onprem/nginx-archive.conf` (on-prem), Railway edge (cloud). Verified: a TLS 1.0 or 1.1 client gets `protocol_version` alert; TLS 1.2 and 1.3 connect; port 80 returns 301 |
| Certificate expiry and protocol drift | Blackbox probe with `min_version: TLS12`, alert 14 days before expiry | `deploy/prometheus/blackbox.yml`, alert `TLSCertificateExpiring` |
| App to PostgreSQL | TLS; `ssl=verify-full` with the bank CA on-prem, `ssl=require` is added automatically for Neon | `DATABASE_URL` (`config.py` normalises `sslmode`) |
| App to Redis, Qdrant, MinIO | TLS (`rediss://`, `https://` URLs) | Connection URLs in the environment |
| App to AD, SIEM | LDAPS / StartTLS with `LDAP_CA_FILE`; syslog over TLS with `SIEM_CA_FILE` | Identity and SIEM adapters |
| App to AI gateway | Bearer token on the isolated AI VLAN; TLS can be enabled on the gateway | `deploy/onprem/ai-gateway.conf.template` |

### At rest

| Data | Mechanism |
|---|---|
| Originals and versions | MinIO server-side encryption (SSE-KMS through KES with the bank KMS or HSM), or encrypted volumes; S3/R2 encrypt at rest by default in the cloud phase. Object Lock prevents changes |
| PostgreSQL | Encrypted volumes (LUKS or the virtualisation platform's storage encryption) on-prem; Neon encrypts storage at rest |
| Qdrant, Redis | Encrypted volumes; no document content in Redis beyond short-lived job payloads |
| Backups | AES-256-GCM, authenticated per record (`bcp_project/dr/crypto.py`); key held in the bank vault or HSM, never next to the backups |
| MFA secrets | Fernet (AES-128-CBC with HMAC-SHA256), keys rotatable (`MFA_ENCRYPTION_KEY`, `scripts/rewrap_mfa_secrets.py`) |
| Passwords (local and break-glass accounts) | bcrypt |
| Watermarked viewing copies | Never written to disk; held in memory for at most 5 minutes |

Disk and volume encryption is a host setting, not something the application can enforce. The
hardening checklist ([`hardening.md`](hardening.md)) includes it as a pre-go-live check.

## 5. Choices left to the bank

- **TLS between the edge and the web tier.** On a dedicated application VLAN plain HTTP is common.
  If bank policy requires end-to-end TLS, point nginx at `https://` upstreams and run uvicorn with
  `--ssl-keyfile` / `--ssl-certfile`, or add a TLS sidecar.
- **TLS on the AI gateway.** Add `listen 8443 ssl` with a bank certificate to the gateway template
  and set `LOCAL_LLM_BASE_URL` to `https://`.
- **Proxy depth.** `TRUSTED_PROXY_HOPS` must equal the number of proxies that append to
  `X-Forwarded-For` (1 for nginx alone, 2 with a load balancer in front). Audit-log addresses and
  sign-in lockouts depend on it. After go-live, send a request with a made-up `X-Forwarded-For`
  header and confirm the audit log shows the real address.
