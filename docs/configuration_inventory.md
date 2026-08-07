# Configuration Inventory

Required by `first_production_packaging.md` §9.

**Date:** 2026-08-07 · **Source of truth:** `emaild/config.py`, `.env.example`

Classification per §9: installation-time, runtime, secret, or generated-immutable.

## Core

| Variable | Class | Required | Default | Secret | Restart? | Notes |
|---|---|---|---|---|---|---|
| `EMAILD_ROLE` | install | yes | `api` | no | yes | `api` / `worker` / `admin`. Determines which secrets may load. |
| `EMAILD_ENV` | install | no | `development` | no | yes | `production` enables fail-closed checks. |
| `EMAILD_DATABASE_URL` | install | **yes** | — | contains secret | yes | Redacted in all diagnostics. |
| `EMAILD_ENV_FILE` | install | no | `.env` | no | yes | `none` = env only. **Read at import time**; setting it later has no effect. |
| `EMAILD_LOG_LEVEL` | runtime | no | `INFO` | no | yes | |

## Secrets

Role-scoped and enforced at startup — a process given a secret it must not hold
refuses to start (verified: exit 3).

| Variable | Required for | Forbidden for | Rotatable? |
|---|---|---|---|
| `EMAILD_MAILBOX_ENCRYPTION_KEY` | worker, admin | **api** | Yes, but never implicitly (§44). Rotating requires re-encrypting every mailbox password — demonstrated in Phase 2. |
| `EMAILD_MXROUTE_SERVER` | admin | api, worker | n/a |
| `EMAILD_MXROUTE_USERNAME` | admin | api, worker | n/a |
| `EMAILD_MXROUTE_API_KEY` | admin | api, worker | Yes — one click in the MXRoute panel. Account-root. |
| `EMAILD_DASHBOARD_TOKEN` | optional | — | Yes, freely. |
| `POSTGRES_PASSWORD` | compose | — | Requires a database user change. |

**`EMAILD_MAILBOX_ENCRYPTION_KEY` is the one that must never be regenerated on
upgrade.** §9 and §44 both forbid it, and doing so silently makes every stored
SMTP credential undecryptable.

## Provider limits

Fallbacks only — the worker prefers what the server advertises in its EHLO banner
(`SIZE`, `MAILMAX`, `RCPTMAX`), so these apply only to a server that advertises
nothing.

| Variable | Class | Default | Notes |
|---|---|---|---|
| `EMAILD_SMTP_PORT` | install | `465` | Implicit TLS. |
| `EMAILD_SMTP_TIMEOUT_SECONDS` | runtime | `30` | |
| `EMAILD_FALLBACK_MAX_MESSAGE_BYTES` | runtime | `52428800` | |
| `EMAILD_FALLBACK_MAX_RECIPIENTS` | runtime | `150` | |
| `EMAILD_FALLBACK_MAX_MESSAGES_PER_CONNECTION` | runtime | `100` | |
| `EMAILD_HOURLY_LIMIT` | runtime | `400` | MXRoute's ceiling. Not ours to raise. |
| `EMAILD_RATE_LIMIT_SAFETY_MARGIN` | runtime | `0.9` | **Validated ≤ 0.95.** Over-limit is a permanent rejection, so aiming at the ceiling destroys mail. |

## Retention

| Variable | Class | Default | Affects stored data? |
|---|---|---|---|
| `EMAILD_BODY_RETENTION_HOURS` | runtime | `72` | **Yes** — lowering it purges sooner. Irreversible. |
| `EMAILD_IDEMPOTENCY_TTL_HOURS` | runtime | `24` | Yes, but expendable data. |

## Network and dashboard

| Variable | Class | Default | Notes |
|---|---|---|---|
| `EMAILD_TRUSTED_PROXY_HOSTS` | install | empty | §46: trust forwarded headers only from the tunnel. Empty = trust nothing. |
| `EMAILD_DASHBOARD_ENABLED` | runtime | `true` | |
| `EMAILD_DASHBOARD_TOKEN` | secret | unset | HTTP Basic when set. |
| `EMAILD_DASHBOARD_BEHIND_PROXY_AUTH` | install | `false` | Explicit statement that Cloudflare Access is in front. |
| `API_HOST_PORT` | install | `8000` | Compose only. Currently `8018` locally — 8000 was taken. |
| `POSTGRES_HOST_PORT` | install | `5433` | Compose only. Currently `5443` locally. |

## Generated immutable values (§9)

| Value | Where | Status |
|---|---|---|
| Installation ID | `installation` table | **NOT YET GENERATED — see F-01.** |
| Mailbox encryption key | operator-generated | Created once, preserved across upgrades. |
| API keys | `api_keys` table | Hashed; shown once, never recoverable. |

## Fail-closed checks already enforced

1. `role=api` or `worker` with any `MXROUTE_*` → refuses to start.
2. `role=api` with `MAILBOX_ENCRYPTION_KEY` → refuses.
3. `role=worker`/`admin` without `MAILBOX_ENCRYPTION_KEY` → refuses.
4. `role=admin` missing any MXRoute value → refuses.
5. `env=production` with `CHANGEME` in the DSN → refuses.
6. `env=production`, dashboard on, no token and no proxy-auth acknowledgement → refuses.
7. `rate_limit_safety_margin > 0.95` → refuses.

## Gaps

- **No `config-check` command** (§16). Every check above happens at startup;
  none can be run ahead of an upgrade as a preflight (§36).
- **No documented "safe to change live" matrix.** In practice every variable
  needs a restart, because Settings is read once at process start. Worth stating
  in `docs/configuration.md` rather than leaving implicit.
