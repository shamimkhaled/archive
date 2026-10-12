# Operating system and container hardening

Section 9 item covered here: Security Architecture (platform hardening). Baselines are the CIS
Benchmarks the bank already uses: CIS Ubuntu Linux 22.04/24.04 LTS or CIS RHEL 9, Level 1 Server,
and the CIS Docker Benchmark. This page lists what applies to the archive and how to check it. The
bank's own hardening standard takes precedence where they differ.

## 1. What the product already does

| Control | Where |
|---|---|
| Multi-stage image on `python:3.12-slim-bookworm`; compilers only in the build stage | `Dockerfile` |
| Runs as an unprivileged user (uid 10001). Under the default compose/Railway setup the entrypoint starts as root only to fix volume ownership, then drops to that user; the hardened profile starts as uid 10001 directly | `Dockerfile`, `docker-entrypoint.sh`, `deploy/onprem/docker-compose.hardening.yml` |
| Read-only root filesystem, all capabilities dropped, `no-new-privileges`, PID and memory limits, rotated logs | `deploy/onprem/docker-compose.hardening.yml` (tested, section 4) |
| No secrets in the image or in git; configuration comes from the environment or the vault | `.dockerignore`, `.gitignore`, `.env.example` |
| Dependency and code scanning on every change: `pip-audit`, `npm audit`, `bandit`, OWASP ZAP baseline, image scan (Trivy, critical and high, fixed vulnerabilities only) | `.github/workflows/ci.yml` |
| Security headers (CSP, frame denial, referrer and permissions policy, HSTS), CSRF on every form | `security.py` |
| TLS 1.2+ front end and port matrix | `deploy/onprem/nginx-archive.conf`, [`network.md`](network.md) |
| Write-once originals, append-only hash-chained audit log | [`storage-retention.md`](storage-retention.md), [`security.md`](security.md) |

## 2. Host operating system checklist

| # | Control | Check |
|---|---|---|
| H1 | Minimal server install; no desktop, no compilers on production hosts | `dpkg -l \| grep -E 'gcc\|xserver'` returns nothing |
| H2 | Security patches monthly; critical within 72 hours (bank patch window) | Patch report from the bank's patch tool |
| H3 | SSH: keys only, no root login, access only from the bastion/PAM subnet, idle timeout | `sshd -T \| grep -E 'passwordauthentication no\|permitrootlogin no'` |
| H4 | Host firewall default-deny inbound; allow only the rows of the port matrix for that host's role | `nft list ruleset` or `ufw status verbose` |
| H5 | Full-disk or volume encryption (LUKS, or the virtualisation platform's encrypted datastore) for data, object storage and backup volumes | `lsblk -o NAME,TYPE,FSTYPE` shows `crypto_LUKS`, or the platform's encryption report |
| H6 | SELinux enforcing (RHEL) or AppArmor enabled (Ubuntu); Docker's default profiles active | `getenforce` / `aa-status`; `docker info --format '{{.SecurityOptions}}'` |
| H7 | `auditd` with CIS rules; logs shipped to the SIEM | `auditctl -l`, SIEM receives host events |
| H8 | Time synchronised to the bank NTP (audit timestamps and TOTP depend on it) | `chronyc tracking` offset < 100 ms |
| H9 | Accounts from AD (sssd), `sudo` limited to an ops group and logged | `/etc/sudoers.d/*`, sudo events in the SIEM |
| H10 | File integrity monitoring (AIDE or the bank's EDR) on `/etc`, `/usr/bin`, Docker config | Baseline exists and is checked daily |
| H11 | Separate partitions or volumes for `/var/lib/docker`, data and logs, so a full log cannot stop the database | `df -h` |
| H12 | Bank EDR/antivirus agent installed per policy, with exclusions for database and Qdrant data paths to avoid I/O stalls | EDR console |
| H13 | Kernel: IP forwarding off except on container hosts; no unused modules (USB storage, firewire) | `sysctl net.ipv4.ip_forward`; `lsmod` |

## 3. Container runtime checklist

| # | Control | Check |
|---|---|---|
| C1 | Docker Engine from the vendor repository, patched with the host | `docker version` |
| C2 | `/etc/docker/daemon.json`: `"icc": false`, `"no-new-privileges": true`, `"live-restore": true`, `"userland-proxy": false`, log size limits | `cat /etc/docker/daemon.json` |
| C3 | User namespace remapping (`"userns-remap": "default"`) or rootless Docker where the bank's platform supports it | `docker info \| grep -i userns` |
| C4 | Only the docker group of named operators can reach the Docker socket; the socket is never mounted into containers | `getent group docker`; `docker inspect` shows no `/var/run/docker.sock` mounts |
| C5 | Images pulled from the bank's internal registry, pinned by tag or digest; the GPU stack images are mirrored, not pulled from the internet at run time | Compose files reference the internal registry |
| C6 | Application containers run with the hardened profile (`user`, `read_only`, `cap_drop: [ALL]`, `no-new-privileges`, limits) | `docker inspect <container> --format '{{.Config.User}} {{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}}'` |
| C7 | Each service has only the networks it needs (application, data, AI); databases are not published on host ports | `docker network inspect`; `ss -tlnp` on data hosts |
| C8 | Image scan before promotion to UAT and production; no critical findings with a fix available | CI image scan job, or `trivy image <image>` |

## 4. Hardened profile test

Built the production web image and ran it under the hardened profile:

```
docker build -t sonali-archive-web:hardening-test .
docker run --user 10001:10001 --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges:true -v uploads:/data/uploads ... sonali-archive-web:hardening-test
```

Result: the API started as uid 10001, ran all migrations on an empty database and answered
`/healthz` and `/readyz`. Writes to `/app`, `/usr` and `/etc` failed, even for root inside the
container; only `/tmp` (tmpfs) and the uploads volume were writable. All five capability sets were
empty and `NoNewPrivs` was 1. CI repeats a short form of this on every build (start under the same
flags as uid 10001 and import the application). Details are in section 7.

## 5. Service-specific hardening

| Service | Settings |
|---|---|
| PostgreSQL | `hostssl` only in `pg_hba.conf` with `scram-sha-256`; `ssl_min_protocol_version = 'TLSv1.2'`. **Two roles:** an owner role used only by migrations, and an application role with `SELECT, INSERT, UPDATE, DELETE` on tables and no ownership. The audit log's append-only triggers can be dropped by the table owner, so the running application must not be the owner. `log_connections`, `log_disconnections`, and pgaudit for DDL |
| Redis | `requirepass` or ACL user per service; `protected-mode yes`; bind to the data VLAN; `rename-command` for `FLUSHALL`, `FLUSHDB`, `CONFIG`, `DEBUG`; AOF on; TLS port where supported |
| Qdrant | API key required; not published outside the data VLAN; snapshots directory on an encrypted volume |
| MinIO | Bucket with versioning and Object Lock; no anonymous or public policies; one access key per service with a least-privilege policy (web and workers: read and write objects in the archive bucket; backup: read only); KES with the bank KMS for server-side encryption; console limited to the management subnet |
| ClamAV | Signature updates from an internal mirror; `freshclam` status alerted |
| AI gateway | Bearer token on every request (`LOCAL_LLM_API_KEY`); model servers not reachable except through the gateway; no internet access from the GPU host after models are loaded |
| nginx | `server_tokens off`; TLS profile as in `nginx-archive.conf`; access logs to the SIEM |
| Prometheus, Grafana | Behind the management reverse proxy with TLS; Grafana sign-up and anonymous access off; admin password from the vault |

## 6. Secrets

| Secret | Store | Rotation |
|---|---|---|
| `JWT_SECRET_KEY` | Vault | Yearly or on suspicion; rotating signs everyone out |
| `MFA_ENCRYPTION_KEY` | Vault | Add new key first, re-wrap with `scripts/rewrap_mfa_secrets.py`, then retire the old one |
| `BACKUP_ENCRYPTION_KEY` | Vault or HSM, never on the backup target | New key per year; old keys kept to restore older backups |
| Database, Redis, Qdrant, MinIO credentials | Vault | Every 90 days or per bank policy |
| `LOCAL_LLM_API_KEY`, `METRICS_TOKEN`, `SIEM_HTTP_TOKEN` | Vault | Every 90 days |
| LDAP bind password, OIDC client secret | Vault | Per directory team policy |

## 7. Test record

**12 Oct 2026, Dev workstation (Docker Engine 29.9, Linux 7.2).** Image `sonali-archive-web` built from the
working tree (`SERVICE_ROLE=web`, Debian 12.15 base). Throwaway PostgreSQL 16 and Redis 7 containers
on a private Docker network; no bank or cloud data. `APP_ENV=production`, `STORAGE_BACKEND=local`,
`MIGRATE_ON_START=1`, `WEB_CONCURRENCY=2`, `METRICS_TOKEN` set.

| Check | Command or probe | Result |
|---|---|---|
| Starts under the profile | `--user 10001:10001 --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true` | Healthy; migrations `0001`–`0011` applied |
| Liveness | `GET /healthz` | 200 `{"status":"ok"}` |
| Readiness | `GET /readyz` | 200, `degraded` only because local storage was used for the test (the expected warning) |
| Process identity | `id` | `uid=10001(appuser) gid=10001(appuser)` |
| Capabilities | `/proc/1/status` | `CapInh`, `CapPrm`, `CapEff`, `CapBnd`, `CapAmb` all `0000000000000000`; `NoNewPrivs: 1` |
| Docker view | `docker inspect` | `User=10001:10001 ReadonlyRootfs=true CapDrop=[ALL] SecurityOpt=[no-new-privileges:true]` |
| Root filesystem | `touch /app/x`, `touch /usr/local/bin/x` | `Read-only file system` |
| Root inside the container | `docker exec -u 0 … touch /etc/x`; `chown 0 /data/uploads` | `Read-only file system`; `Operation not permitted` (no `CAP_CHOWN`) |
| Escalation tools | `sudo`, `su root` | No `sudo` in the image; `su` fails authentication |
| Writable paths | `/tmp`, `/data/uploads` | Writable (tmpfs and the volume owned by 10001) |
| Metrics | `GET /metrics` without and with the bearer token | 401 without, 200 with; counters from both worker processes merged |
| Response headers | `GET /login` | CSP, `X-Frame-Options: DENY`, `nosniff`, Referrer-Policy, Permissions-Policy, HSTS |
| Image scan, first build | `trivy image --severity CRITICAL,HIGH --ignore-unfixed` | 8 findings (3 critical, 5 high), all in base-image Debian packages (`perl-base`, `libpcre2-8-0`) that Debian had already fixed; none in Python packages |
| Image scan after the fix | Same, after adding `apt-get upgrade` to the runtime stage of the `Dockerfile` | 0 critical, 0 high; `perl-base 5.36.0-7+deb12u4`, `libpcre2-8-0 10.42-1+deb12u2` |

Not covered by this run: the `pipeline` and `worker` images (the CI `docker` job scans and builds
all three), and the host checklist in section 2, which runs on the bank's servers.
