# Configuration

Every variable emaild reads. Source of truth is `emaild/config.py`; `.env.example`
carries the same list with inline notes.

**Every variable requires a restart.** Settings are read once at process start,
so there is no live-reload to reason about.

```bash
./appctl config-check       # validate .env BEFORE an upgrade, not at startup
```

## Core

| Variable | Required | Default | Notes |
|---|---|---|---|
| `EMAILD_ROLE` | yes | `api` | `api` / `worker` / `admin`. Decides which secrets may load. |
| `EMAILD_ENV` | no | `development` | `production` enables the fail-closed checks. |
| `EMAILD_DATABASE_URL` | **yes** | — | Redacted in every log and diagnostic. |
| `EMAILD_LOG_LEVEL` | no | `INFO` | |
| `EMAILD_ENV_FILE` | no | `.env` | `none` = environment only. **Read at import time** — setting it later has no effect. |

## Secrets

Role-scoped and enforced at startup. A process given a secret it must not hold
refuses to start.

| Variable | Required for | Forbidden for |
|---|---|---|
| `EMAILD_MAILBOX_ENCRYPTION_KEY` | worker, admin | **api** |
| `EMAILD_MXROUTE_SERVER` | admin | api, worker |
| `EMAILD_MXROUTE_USERNAME` | admin | api, worker |
| `EMAILD_MXROUTE_API_KEY` | admin | api, worker |

> **Never regenerate `EMAILD_MAILBOX_ENCRYPTION_KEY` during an upgrade.** Doing
> so makes every stored SMTP credential undecryptable. It is created once and
> preserved for the life of the installation.

The MXRoute key is **account-root**: it can delete mailboxes and manage reseller
users, not merely read. It belongs only to the admin path, which is never
publicly routed.

## Sending limits

Fallbacks only — the worker prefers what the server advertises in its EHLO
banner and uses these when a server advertises nothing.

| Variable | Default | Notes |
|---|---|---|
| `EMAILD_SMTP_PORT` | `465` | Implicit TLS. |
| `EMAILD_SMTP_TIMEOUT_SECONDS` | `30` | |
| `EMAILD_HOURLY_LIMIT` | `400` | MXRoute's ceiling. Not yours to raise. |
| `EMAILD_RATE_LIMIT_SAFETY_MARGIN` | `0.9` | **Validated ≤ 0.95.** |
| `EMAILD_FALLBACK_MAX_MESSAGE_BYTES` | `52428800` | |
| `EMAILD_FALLBACK_MAX_RECIPIENTS` | `150` | |
| `EMAILD_FALLBACK_MAX_MESSAGES_PER_CONNECTION` | `100` | |

The safety margin exists because over-limit is a **permanent rejection with no
provider-side queue**. Aiming at the ceiling destroys mail rather than delaying
it. `1.0` is refused.

## Retention

| Variable | Default | Changes stored data? |
|---|---|---|
| `EMAILD_BODY_RETENTION_HOURS` | `72` | **Yes** — lowering purges sooner, irreversibly. |
| `EMAILD_IDEMPOTENCY_TTL_HOURS` | `24` | Yes, but expendable. |

## Network and dashboard

| Variable | Default | Notes |
|---|---|---|
| `EMAILD_BIND` | `127.0.0.1` | `0.0.0.0` for LAN. Read the cleartext warning in [installation.md](installation.md). |
| `API_HOST_PORT` | `8000` | |
| `EMAILD_TRUSTED_PROXY_HOSTS` | empty | Trust forwarded headers only from the tunnel. Empty = trust nothing. |
| `EMAILD_DASHBOARD_ENABLED` | `true` | |
| `EMAILD_DASHBOARD_TOKEN` | unset | HTTP Basic when set. |
| `EMAILD_DASHBOARD_BEHIND_PROXY_AUTH` | `false` | Set true **only** when Cloudflare Access authenticates the dashboard in front. |

## Deployment

| Variable | Notes |
|---|---|
| `EMAILD_IMAGE` | Registry path. |
| `EMAILD_VERSION` | **Required.** Compose refuses to start without an exact version. |
| `POSTGRES_USER` / `POSTGRES_DB` | Default `emaild`. |
| `POSTGRES_PASSWORD` | **Required.** |
| `CLOUDFLARE_TUNNEL_TOKEN` | Only for the `public` profile. |

## What refuses to start

Seven checks, all deliberate. `./appctl config-check` catches most of them
before you restart rather than after.

1. `api` or `worker` holding any `MXROUTE_*` credential
2. `api` holding the mailbox encryption key
3. `worker` or `admin` **without** the mailbox encryption key
4. `admin` missing any MXRoute value
5. `production` with a placeholder database password
6. `production` with the dashboard on and no auth and no proxy-auth acknowledgement
7. safety margin above 0.95
