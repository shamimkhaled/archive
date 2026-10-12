# Security operations

Runbook for the controls added in Phase 8: sign-in through the bank directory, the tamper-evident
audit log, SIEM forwarding, rate limits, secret rotation, and the CI security gates.

## Sign-in

| Mode | Enable | What is trusted |
| --- | --- | --- |
| Local password | default | bcrypt hash in `users`; TOTP step-up for `MFA_REQUIRED_ROLES` |
| LDAP / AD | `LDAP_ENABLED=1`, `LDAP_*` | A bind as the user against the directory, after a service-account search |
| OIDC (AD FS, Entra ID, Keycloak) | `OIDC_ENABLED=1`, `OIDC_*` | An ID token from the IdP token endpoint, verified against the IdP JWKS |

**OIDC flow.** `/auth/oidc/start` creates `state`, `nonce`, and a PKCE verifier, and keeps them in a
signed, HttpOnly, 10-minute cookie scoped to `/auth/oidc`. The callback:

1. checks `state`;
2. exchanges the code with the verifier;
3. verifies the ID token's signature (JWKS, `OIDC_ALLOWED_ALGS`), issuer, audience, expiry (60 s skew), nonce and `at_hash`;
4. reads the username from `OIDC_USERNAME_CLAIM`.

Nothing from the query string selects the account.

**Roles from the directory.** Set `OIDC_ROLE_MAP` / `LDAP_ROLE_MAP` to a JSON object mapping each group to a role.

- When a user is in several mapped groups, the most privileged role wins: `admin`, then `board_secretary`, then `board_member`, then `uploader`.
- While a map is configured (and `IDENTITY_SYNC_ROLES=1`, the default), the directory is authoritative. The role is re-synced at every sign-in, and a user in no mapped group is refused. Removing someone from the archive groups therefore revokes their access at the next sign-in. Sessions already issued last until `ACCESS_TOKEN_EXPIRE_MINUTES` runs out.
- `*_AUTO_PROVISION=1` creates the local user on first sign-in. That user gets a random local password, so they can only sign in through the directory.

**MFA.**
- OIDC users skip the local TOTP step only when `OIDC_TRUST_IDP_MFA=1` *and* the ID token's `amr` claim says MFA was performed.
- LDAP users always go through the local TOTP step-up for privileged roles.

**Break-glass.** With LDAP on, only accounts listed in `LDAP_LOCAL_FALLBACK_USERS` may use their local password when the directory rejects them or is unreachable. Every fallback is audited as `login_local_fallback`. Keep the list to one or two sealed admin accounts.

## Tamper-evident audit log

- `audit_logs` is append-only in Postgres (migration `0010`). Triggers reject UPDATE, DELETE and TRUNCATE. The one exception is a single "seal" update, which sets `prev_hash`/`row_hash` on a row that has none and changes nothing else.
- A scheduler job (`AUDIT_SEAL_INTERVAL_SECONDS`, one replica at a time via a Redis lock) seals rows in id order:
  `row_hash = sha256(prev_hash + "\n" + canonical JSON of the row)`. The chain starts from 64 zeros.
- **Verify:** `POST /api/admin/audit/verify` (admin). It recomputes every hash and walks the chain from the start. It reports the first broken row and the reason: content changed, fork, missing predecessor, or newest rows removed. Each verification is itself audited.
- **Status:** `GET /api/admin/security/status` shows unsealed rows and the oldest unsealed time, the chain tip, the SIEM cursor, and the identity configuration.
- A database owner can still disable triggers. The SIEM copy and the exported archives are the off-host evidence, and they carry `rowHash`/`prevHash`, so a rewritten chain will not match what the SIEM already holds.

## SIEM forwarding

`SIEM_ENABLED=1` forwards sealed audit rows after each sealing pass.

- **Formats:** CEF (default) or JSON.
- **Transports:** syslog over UDP, TCP or TLS (RFC 5424 framing, octet-counted on TCP/TLS), or HTTP NDJSON (Splunk HEC, Elastic).
- **Delivery:** the cursor in `integration_cursors` advances only after the SIEM accepts a batch, so a SIEM outage delays events but loses none.
- **Severity:**
  - 7 for denials, failures, lockouts, rejected SSO sign-ins and audit-chain breaks.
  - 5 for role changes, user provisioning, break-glass sign-ins, approvals, reopenings, downloads and exports.
  - 3 for everything else.

## Rate limits

Limits are fixed one-minute windows in Redis, per signed-in user or per service key. Each one is overridable with `RATE_LIMIT_<BUCKET>_PER_MINUTE`, and `0` disables it.

| Bucket | Default | Routes |
| --- | --- | --- |
| search | 60 | `/api/search` |
| ask | 20 | `/api/v1/ask` |
| upload | 30 | uploads, ingest, new versions |
| ai_job | 10 | briefing generation |
| recording | 6 | meeting recording upload |
| api_v1 | 120 | every `/api/v1` call (per client unless the client row sets its own) |

If Redis is unreachable, each replica enforces the limits with local counters. Limits are never dropped. Over-limit requests get `429` with `Retry-After`.

**Client IP behind proxies.** Login lockouts, rate limits and audit rows use the client address
taken from `X-Forwarded-For`, counted `TRUSTED_PROXY_HOPS` entries from the right (default 1). The
proxy closest to the app appends the address it saw, so entries further left are client-supplied
and ignored. Set it to the number of proxies that append to the header: 1 for Railway or a single
nginx, 2 for a bank load balancer in front of nginx. Too low and every user appears as the proxy's
address (one lockout hits everyone); too high and a client can choose its own address. On-prem, also
set `FORWARDED_ALLOW_IPS` to the proxy's address so uvicorn ignores forwarded headers from anyone else.

## Monitoring

Security alerts go to the SOC through Alertmanager (`severity: security`): more than 30 failed
sign-ins in 10 minutes, any lockout, more than 100 rate-limit rejections in 10 minutes. LDAP
outages page on-call. Details: [`evidence/monitoring.md`](evidence/monitoring.md).

## Changes that need a second person

| Change | Control |
| --- | --- |
| OCR verification, minutes, briefings | Four-eyes by default in production (`OCR_REVIEW_FOUR_EYES`, `MINUTES_FOUR_EYES`, `BRIEFING_FOUR_EYES`) |
| AI model route switch, reset or rollback | `AI_ROUTE_FOUR_EYES=1`: the change waits at `/admin/ai` until another admin approves it |
| Record disposal | Always four-eyes at `/admin/retention`; proposer and approver must differ. Legal hold blocks it |

## Secret rotation

| Secret | How to rotate without downtime |
| --- | --- |
| `JWT_SECRET_KEY` | Set the new value and move the old one to `JWT_SECRET_KEY_PREVIOUS`, then redeploy. Existing sessions stay valid and are re-signed by the sliding-session refresh. After one `ACCESS_TOKEN_EXPIRE_MINUTES` (8 h by default), remove the previous key. |
| `MFA_ENCRYPTION_KEY` | Set the new key and put the old one in `MFA_ENCRYPTION_KEY_PREVIOUS`, then redeploy. Run `scripts/rewrap_mfa_secrets.py --apply`, then remove the previous key. Without `MFA_ENCRYPTION_KEY`, TOTP secrets are encrypted with a key derived from the JWT secret: rotate both together and run the rewrap. |
| Service API keys | Create a second key for the client (`scripts/create_service_client.py`), switch the caller, then deactivate the old one. Verified keys are cached for at most `API_KEY_CACHE_SECONDS` (300). |
| `WEBHOOK_SECRET` | Coordinate with receivers. They should accept both signatures for the switch-over window. |
| `DATABASE_URL`, Redis, Qdrant, S3, AI provider keys | Issue a second credential at the provider, update the Railway variable (each service redeploys), then revoke the old one. |
| `OIDC_CLIENT_SECRET`, `LDAP_BIND_PASSWORD` | Add the new secret at the IdP or directory, update the variable, redeploy, then remove the old one. |

Review once a quarter:
- `.env` is never committed (it is in `.gitignore`).
- Railway variables are limited to the services that need them.
- No secret appears in logs. `scripts/` and the app print only the presence of keys.

## CI gates

- **Unit tests and bilingual retrieval evaluation.**
- **Migrations:** fresh upgrade, re-run, single head, and `scripts/check_audit_append_only.py`. That script checks that edits, deletes, truncates and re-seals of audit rows are refused.
- **Dependency scan:** `pip-audit` over `requirements*.txt`, plus `bandit` at high severity and high confidence. Dependabot opens weekly update PRs for pip, Docker, and Actions.
- **Image scan:** Trivy on each built image; a critical or high vulnerability with a fix available breaks the build. The image also has to start under the hardened profile (uid 10001, read-only root, no capabilities).
- **Monitoring config:** `promtool check config`, alert rule unit tests, `amtool check-config`, dashboard JSON.
- **OWASP ZAP baseline:** a passive scan of the running app. Rules are in `.zap/rules.tsv`. FAIL rules (missing clickjacking or nosniff headers, error disclosure, X-Powered-By) break the build; WARN rules are reported.
