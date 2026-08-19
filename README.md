# emaild

[![CI](https://github.com/SoupNChill/internal_email_tool/actions/workflows/ci.yml/badge.svg)](https://github.com/SoupNChill/internal_email_tool/actions/workflows/ci.yml)
[![Release](https://img.shields.io/badge/release-v0.10.0--rc.1-blue)](https://github.com/SoupNChill/internal_email_tool/releases)
[![Python](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org/)

**A self-hosted transactional email API.** One clean endpoint for every product
you build — and an honest answer about what happened to each message.

Applications send one authenticated request. emaild takes responsibility from
there: durable acceptance, asynchronous delivery, controlled retries, honest
failure classification, and a timeline you can actually read.

```bash
curl -X POST https://mail.example.com/v1/emails \
  -H "Authorization: Bearer em_live_..." \
  -H "Content-Type: application/json" \
  -d '{
    "from": "Acme <noreply@example.com>",
    "to": "customer@example.net",
    "subject": "Verify your email",
    "html": "<p>Click to verify.</p>",
    "text": "Click to verify."
  }'

{"id":"email_01KZDD5PWYYJNQPPWHN9YYYB96","status":"queued"}
```

Wire-compatible with [Resend](https://resend.com), so any coding assistant
already knows how to call it. Point [docs/integration.md](docs/integration.md) at
one and it will get the integration right without further explanation.

---

## Why this exists

Every new product hits the same wall: SMTP credentials, DNS records, retry
logic, and no idea whether a message actually left the building. None of it is
the product.

emaild makes those decisions once. Applications never see SMTP credentials, and
a new product inherits a working email foundation instead of rediscovering one.

### What it will not tell you

Most email APIs report `delivered`. emaild does not, because on this provider it
cannot be proven:

| Status | What it actually means |
|---|---|
| `queued` | Durably stored. **Not sent.** |
| `sending` | A worker is delivering it now. |
| `accepted_by_provider` | The provider took custody and answered `250`. **Terminal — not "delivered".** |
| `temporarily_failed` | Retryable failure; will retry with backoff. |
| `permanently_rejected` | Will not be retried. |

Bad external recipients are accepted at RCPT and bounce out of band, so
`accepted_by_provider` is the honest limit of what can be demonstrated.

---

## Features

- **Resend-compatible API** — `POST /v1/emails`, bearer auth, `Idempotency-Key`
- **Durable acceptance** — the response is sent only after the row commits
- **Postgres as the queue** — `FOR UPDATE SKIP LOCKED` plus a reaper for crashed
  workers; no broker to operate
- **Scoped API keys** — per project and per sender identity, revoked instantly
  because authentication is never cached
- **Domain lifecycle** — DNS verified continuously; only `ready` domains send
- **Honest failure classification** — built from responses captured on a live
  server, not guessed
- **Suppression list** — fed automatically by provider rejections
- **Read-only dashboard** — every mutation lives in the CLI, where it is logged
- **Backup and restore** — proven by destroying an installation and rebuilding it

---

## Quick start

**Sending from an application?** → **[docs/integration.md](docs/integration.md)**
is the whole API in two minutes.

**Running the service?** → **[docs/installation.md](docs/installation.md)**.

```bash
sudo mkdir -p /opt/emaild && sudo chown $USER /opt/emaild && cd /opt/emaild
curl -fsSL https://raw.githubusercontent.com/SoupNChill/internal_email_tool/main/deploy/compose.yaml -o compose.yaml
docker compose up -d
```

No `.env`, no installer: the version is pinned in the compose file and both
secrets generate themselves on first boot. Upgrading is
`docker compose pull && docker compose up -d`.

Then save the encryption key, which is the one thing that cannot be
regenerated — see [installation.md](docs/installation.md).

**Working on emaild itself?** See [Development](#development).

---

## Documentation

### Operators

| | |
|---|---|
| [installation.md](docs/installation.md) | Install, upgrade, uninstall |
| [operations.md](docs/operations.md) | Day-to-day running |
| [backup-and-restore.md](docs/backup-and-restore.md) | **Read this one first** |
| [troubleshooting.md](docs/troubleshooting.md) | Symptom → cause → fix |
| [configuration.md](docs/configuration.md) | Every variable |
| [architecture-overview.md](docs/architecture-overview.md) | How it fits together |

### Integrators

| | |
|---|---|
| [integration.md](docs/integration.md) | The whole API. Paste it to a coding assistant. |

### Design record

| | |
|---|---|
| [vision.md](vision.md) | What this is for, and what it refuses to become |
| [spec_sheet.md](spec_sheet.md) | What MXRoute actually does — verified, inferred, unknown |
| [spike_results.md](spike_results.md) | Observed SMTP behaviour, and the taxonomy it seeded |
| [build_plan.md](build_plan.md) | The nine phases |
| [docs/distribution_audit.md](docs/distribution_audit.md) | Production-readiness audit |

---

## Architecture

```
   your product                        ┌──────────────────┐
        │  POST /v1/emails             │    MXRoute       │
        ▼                              │  REST  (admin)   │
   ┌─────────┐                         │  SMTP  :465      │
   │   api   │  no SMTP, no admin key  └───▲──────────▲───┘
   └────┬────┘                            │          │
        │ writes                          │          │
        ▼                                 │          │
   ┌──────────┐      claims        ┌──────┴───┐      │
   │ postgres │◄──────────────────►│  worker  │──────┘
   │ state +  │                    │ no port  │  outbound only
   │ queue    │                    └──────────┘
   └──────────┘
```

**Three roles, three sets of secrets**, enforced at startup — a process given a
secret it must not hold refuses to start:

| Role | Mailbox key | MXRoute admin key | Listens |
|---|---|---|---|
| `api` | forbidden | forbidden | yes |
| `worker` | required | forbidden | no |
| `admin` | required | per command | no |

Compromising the internet-facing surface therefore yields sending within
existing key scopes — not the ability to delete mailboxes.

---

## Provider constraints worth knowing

Verified against a live account, not assumed:

- **400 sends/hour per sender identity**, and over-limit is a **permanent
  rejection with no provider-side queue.** The rate limiter is a hard gate: it
  holds messages back rather than letting them fail.
- **The envelope sender must equal the SMTP login exactly** — plus-addressing
  included. A mailbox *is* a sender identity, one-to-one, which is why bounce
  attribution uses an authored `Message-ID` rather than VERP.
- **Message bodies are purged** 72 hours after a terminal state. Verification
  links and reset tokens must not become a permanent archive.

---

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env    # set POSTGRES_PASSWORD and EMAILD_MAILBOX_ENCRYPTION_KEY
docker compose up -d

ruff check . && ruff format --check .
mypy emaild
EMAILD_TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5443/emaild pytest -q
./scripts/secret-scan.sh
```

CI runs all of the above, plus a production image build that must start and
become healthy.

### Conventions

- **Released migrations are immutable.** Correct a mistake with a new migration,
  never by editing one that shipped. Formatters are excluded from
  `alembic/versions/` so they cannot rewrite one by accident.
- **`emaild/__init__.py` is the only source of the version.**
- **Run `./scripts/secret-scan.sh` before committing.** It checks for provider
  credentials, API keys, encryption keys, and any live value from your `.env`.

### Releasing

```bash
# 1. bump __version__ in emaild/__init__.py
# 2. commit
git tag -a v1.2.3 -m "..." && git push origin v1.2.3
```

The release workflow verifies the tag matches the source, runs the full suite,
builds the image **once**, publishes it to GHCR, and records the digest. It is
the only workflow holding `packages: write`.

---

## Project status

**v0.10.0-rc.1** — feature complete and packaged. The full lifecycle (install →
data → restart → container replacement → backup → destroy → restore → verify →
send) has been demonstrated end to end on a clean installation.

Deliberately out of scope: marketing campaigns, contact lists, drip sequences,
scheduled sends, a visual editor, and any promise about inbox placement that
cannot be proven.
