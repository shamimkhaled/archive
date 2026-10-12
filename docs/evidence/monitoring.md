# Monitoring and alerting

Section 9 items covered here: Deployment Architecture (operations) and High Availability (detection).
Everything below is configuration in the repository; CI validates it on every change.

## 1. What is collected

| Source | How | Port | Contents |
|---|---|---|---|
| Web replicas | `GET /metrics` (Prometheus text format), bearer `METRICS_TOKEN` | 8080 | HTTP rate, status and latency per route; search and Ask latency; Ask outcomes (answered, refused, no source); AI calls, latency and errors per task and provider; egress-policy decisions; sign-ins by method, failures, lockouts, directory errors; rate-limit rejections; Redis Streams lag, pending and dead-letter length; unpublished outbox |
| Workers and outbox relay | Same format on `METRICS_PORT` (9108) | 9108 | Job runs and duration per job type and outcome; seconds since the worker last polled; AI calls made by jobs |
| PostgreSQL | `postgres-exporter` | 9187 | Up, connections, replication lag |
| Redis | `redis-exporter` | 9121 | Up, memory |
| Qdrant | Built-in `/metrics` | 6333 | Up, collection and request metrics |
| Hosts | `node-exporter` | 9100 | Disk, memory, CPU |
| Public endpoint | `blackbox-exporter`, HTTPS probe requiring TLS 1.2+ | 9115 | Reachability, certificate days left |

Uvicorn runs several worker processes per replica, so each process writes its counters to
`METRICS_MULTIPROC_DIR` and `/metrics` returns the merged total. In production `/metrics` returns
404 unless `METRICS_TOKEN` is set, and 401 without the right token. Metric names start with `bcp_`;
counters end in `_total` and durations are in seconds.

Prometheus, Alertmanager and Grafana listen only on 127.0.0.1 in the example compose file and are
reached through the management reverse proxy (port matrix rows in [network.md](network.md)).

## 2. Alerts

28 rules in `deploy/prometheus/alerts.yml`, unit-tested with `promtool test rules`
(`deploy/prometheus/alerts.test.yml`). Thresholds are starting points to tune after the UAT load
test.

| Group | Alerts (severity) |
|---|---|
| Availability | Web replica down (critical); worker down (critical); more than 2% 5xx for 10 minutes (critical); search p95 over 3 s (warning); Ask p95 over 20 s (warning) |
| Job pipeline | Redis Streams unreachable (critical); more than 500 undelivered jobs (warning); more than 100 unacknowledged jobs for 30 minutes (warning); new dead-letter jobs (warning); outbox not draining (critical); more than 20% of a job type failing (warning); worker silent for 30 minutes (warning) |
| AI | A provider failing more than 10% of a task's calls (warning, with the hint to switch route at `/admin/ai`); p95 over 30 s (warning); restricted content refused because no local model is configured (warning) |
| Security | More than 30 failed sign-ins in 10 minutes; any account lockout; more than 100 rate-limit rejections in 10 minutes (all `security`); directory (LDAP) unreachable (critical) |
| Infrastructure | PostgreSQL down or replication lag (critical); Redis down (critical) or memory high (warning); Qdrant down (critical); disk filling, memory pressure (warning); TLS certificate expiring within 14 days (warning); public endpoint down (critical) |

**Routing** (`deploy/prometheus/alertmanager.example.yml`): `critical` pages on-call, `warning`
opens a business-hours ticket by email, `security` goes to the SOC and on-call. While a component
is down, alerts that are only its consequences are inhibited (for example, queue backlog while Redis
is down). Critical alerts link to the runbook in `docs/DR.md`. The bank sets the receivers (email
relay, pager or ITSM webhook, SOC address) at install; the live `alertmanager.yml` is not committed.

## 3. Dashboard

`deploy/grafana/dashboards/sonali-archive-overview.json`, provisioned automatically with its
Prometheus data source. Rows: service health (replicas, workers, Redis Streams, 5xx ratio, outbox,
dead letters); HTTP (rate by status, p95 by route, search and Ask latency, Ask outcomes); job
pipeline (lag, pending, runs, duration, outbox, worker heartbeat); AI providers (calls, latency,
errors, egress decisions); security (sign-ins, failures and lockouts, rate limits); infrastructure
(database, Redis, Qdrant, replication lag, disk, certificate days). Grafana anonymous access and
sign-up are off; the admin password comes from the vault.

## 4. Running it

```bash
openssl rand -hex 32 > deploy/prometheus/metrics_token            # not committed
echo "METRICS_TOKEN=$(cat deploy/prometheus/metrics_token)" >> .env
cp deploy/prometheus/alertmanager.example.yml deploy/prometheus/alertmanager.yml  # set receivers
GRAFANA_ADMIN_PASSWORD=... docker compose -f docker-compose.yml \
  -f deploy/observability.compose.yml --profile pipeline up -d
```

On bank hosts, take the token and Grafana password from the vault instead of generating them here.

Edit the `blackbox-https` target in `deploy/prometheus/prometheus.yml` to the archive's public URL.
On Railway, use the platform's metrics plus an external Prometheus that scrapes `/metrics` with the
token.

## 5. Logs and audit

Application logs are JSON with a correlation ID per request (`LOG_FORMAT=json`), shipped by the
bank's log agent. The audit log is separate: hash-chained in PostgreSQL and forwarded to the SIEM
([security.md](security.md)). Monitoring alerts on failed sign-ins and lockouts; the SIEM keeps the
per-user record.

## 6. Validation

| Check | Where | Result |
|---|---|---|
| `promtool check config` and `promtool test rules` | CI job `monitoring-config` | Pass |
| `amtool check-config` on the example routing | CI job `monitoring-config` | Pass |
| Dashboard JSON valid | CI job `monitoring-config` | Pass |
| `/metrics` format, labels, multi-process merge, token handling | `tests/test_phase11_operations.py` | Pass |
| End-to-end scrape against UAT | UAT | To do at install |
