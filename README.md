# emaild

Internal transactional email API. One secure API, one honest timeline, one
dependable foundation for every product in the portfolio.

Sends through MXRoute over SMTP. Applications never see SMTP credentials.

## Status

**Phase 8 of 9 — dashboard.** The vision's *first perfect mile* is complete and
has an operator interface: a domain is verified, a scoped key is issued, a
message is durably accepted, delivered, retried or classified honestly, and both
the timeline and the metrics reflect what actually happened.

Remaining: production packaging (9). See `build_plan.md`.

**Integrating a product?** `docs/integration.md` is the whole thing — paste it to
a coding assistant, or read it in two minutes.

The dashboard is at `/` — read-only by design. Every mutation lives in the admin
CLI, where it gets a confirmation prompt and a log line.

| Page | Answers |
|---|---|
| `/` | Is email healthy? Volume, latency, headroom, workers. |
| `/domains` | Which domains can send, and exactly which DNS records are missing. |
| `/messages` | Search by recipient, subject, or id; open one for its timeline. |
| `/keys` | What exists, what it may send as, when it was last used. |
| `/suppressions` | Who we refuse to mail, and why. |

Serving it unauthenticated in production is refused at startup. Either set
`EMAILD_DASHBOARD_TOKEN`, or set `EMAILD_DASHBOARD_BEHIND_PROXY_AUTH=true` to
confirm Cloudflare Access is handling it. Never put Access in front of `/v1/*` —
a machine client cannot complete an SSO challenge.

Is email healthy?

```bash
python -m emaild.admin status     # exit 0 healthy, 1 needs attention
```

```
emaild HEALTHY   (window: last 24h)

QUEUE
  pending          0
  oldest pending   -
  needs review     0

PROVIDER LATENCY
  p50 3206 ms   p95 3206 ms   max 3206 ms   (n=1)

WORKERS
  [alive] 4bab159ce304:1  last seen 2s ago  processed=1

HOURLY HEADROOM (over-limit is a permanent rejection, not a deferral)
  noreply@example.com                     1/360                       0%
```

**Queue age is the health signal.** A heartbeat only proves a loop is turning;
queue age proves work is moving, and catches a dead worker, a stuck rate gate,
a provider outage, and an exhausted send budget with one number.

Per-project metrics are available to any key at `GET /v1/metrics` — scoped to
the caller, because how much mail another product sends is not a question an API
key should be able to answer.

```bash
curl -X POST http://localhost:8000/v1/emails \
  -H "Authorization: Bearer em_live_..." \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: order-42" \
  -d '{
    "from": "Acme <noreply@example.com>",
    "to": "customer@example.net",
    "subject": "Verify your email",
    "html": "<p>...</p>",
    "text": "..."
  }'

{"id":"email_01KZDBG61EAKBX5G64TM8V8T3Y","status":"queued"}
```

`queued` means the row is committed and a worker will pick it up. It does not
mean sent, and it certainly does not mean delivered.

Then read the honest timeline:

```
api.accepted        recipients=1 size_bytes=2283
message.queued      message_id_header=<email_01KZ...@example.com>
delivery.attempt    attempt=1 host=chocobo.mxrouting.net
provider.accepted   code=250 response="OK id=1wsDhE-00000008tKT-28PB"
```

`accepted_by_provider` is terminal and means MXRoute took custody. It does
**not** mean delivered — bad external recipients are accepted at RCPT and bounce
out of band, so that is the honest limit of what we can prove.

Working today, via the admin CLI:

```bash
python -m emaild.admin domains token          # ownership TXT record
python -m emaild.admin domains add example.com
python -m emaild.admin domains records example.com   # exact DNS to publish
python -m emaild.admin domains verify                # re-check and update state
python -m emaild.admin mailboxes provision noreply@example.com
python -m emaild.admin projects create billing
python -m emaild.admin keys create billing-prod --project billing \
    --mailbox noreply@example.com
python -m emaild.admin keys revoke billing-prod    # immediate; auth is not cached
```

The CLI is `role=admin` — the only surface holding the MXRoute account-root
credential, and never routed through the tunnel.

## Documents

**Operators**

| File | What it is |
|---|---|
| [docs/installation.md](docs/installation.md) | Installing on a clean host |
| [docs/operations.md](docs/operations.md) | Day-to-day running |
| [docs/backup-and-restore.md](docs/backup-and-restore.md) | **Read this one** |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Symptom → cause → fix |
| [docs/configuration.md](docs/configuration.md) | Every variable |
| [docs/architecture-overview.md](docs/architecture-overview.md) | How it fits together |

**Integrators**

| File | What it is |
|---|---|
| [docs/integration.md](docs/integration.md) | The whole API. Paste it to a coding assistant. |

**Background**

| File | What it is |
|---|---|
| `vision.md` | What we're building and why |
| `spec_sheet.md` | What MXRoute actually does — verified, inferred, and unknown |
| `spike_results.md` | Observed SMTP behaviour, and the failure taxonomy it seeds |
| `build_plan.md` | The nine phases |
| `deployment_and_release.md` | Where it runs, and which release rules bind |
| `docs/distribution_audit.md` | Production-readiness audit and findings |
| `release_rules/` | The standing release contract |

## Quick start

```bash
cp .env.example .env

# Generate the mailbox encryption key. Back this up before creating any mailbox
# (release_rules §44) -- losing it means re-provisioning every mailbox password.
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Set POSTGRES_PASSWORD and EMAILD_MAILBOX_ENCRYPTION_KEY in .env, then:
docker compose up --build

curl -s localhost:8000/health/ready
curl -s localhost:8000/version
```

Public exposure, once a product outside this machine needs to reach it:

```bash
docker compose --profile public up -d    # requires CLOUDFLARE_TUNNEL_TOKEN
```

## Architecture

```
  ┌──────────┐      ┌──────────┐
  │   api    │      │  worker  │        api    : public. No SMTP, no MXRoute key.
  │ (public) │      │(no port) │        worker : outbound SMTP only. No listener.
  └────┬─────┘      └────┬─────┘        admin  : provisioning. Never routed public.
       └───────┬─────────┘
               ▼
        ┌────────────┐         ┌──────────────────┐
        │  postgres  │         │  MXRoute         │
        │ state +    │         │  SMTP :465       │
        │ queue      │         │  REST (admin)    │
        └────────────┘         └──────────────────┘
```

Postgres is the queue — `FOR UPDATE SKIP LOCKED` plus a reaper for stale claims.
At this volume a broker would be a moving part with nothing to do.

### Secret separation

The three roles hold different secrets, enforced at startup rather than by
convention:

| Role | Mailbox key | MXRoute admin key |
|---|---|---|
| `api` | forbidden | forbidden |
| `worker` | required | forbidden |
| `admin` | required | required |

A compromise of the internet-facing surface therefore yields sending within
existing key scopes — not the ability to delete mailboxes or touch reseller
users. Starting a role with a secret it must not have is a hard failure.

## Suppression

The only brake that exists. Bad external recipients come back `250 Accepted` and
bounce out of band, so nothing else stops us mailing a dead address forever.

Permissions are deliberately asymmetric:

| Direction | Who | Why |
|---|---|---|
| **Add** — `POST /v1/suppressions` or CLI | any API key | Fails closed: worst case we stop mailing someone we could have. |
| **Remove** — CLI only, and `--yes` required | operator | Fails open: resumes mail to an address we had reason to distrust. |

Addresses are also suppressed automatically when the provider rejects a
recipient as nonexistent — the one bounce signal available synchronously. The
bar is deliberately high: policy rejections, reputation blocks, and our own
misconfigurations never suppress a recipient, because a wrong entry silently
stops legitimate mail and nobody notices until they complain.

```bash
python -m emaild.admin suppressions list
python -m emaild.admin suppressions add dead@example.net --reason "hard bounce"
python -m emaild.admin suppressions remove dead@example.net --yes
```

## Provider constraints that shape the design

Verified against a live account; details in `spec_sheet.md`.

- **400 sends/hour per sender identity**, and over-limit is a **permanent 5xx**
  with no provider-side queue. The rate limiter is a hard gate, not a throttle.
- **The envelope sender must equal the SMTP login exactly.** A mailbox *is* a
  sender identity, one-to-one.
- **Bad external recipients return `250 Accepted`** and bounce out of band, so
  `accepted_by_provider` is terminal and does **not** mean delivered. The status
  vocabulary says only what we can actually prove.
- Server advertises `SIZE`, `MAILMAX`, and `RCPTMAX` in EHLO — read at connect
  time rather than hardcoded.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ruff check . && ruff format --check .
mypy emaild
pytest

alembic upgrade head          # requires EMAILD_DATABASE_URL
alembic revision --autogenerate -m "description"
```

Released migrations are immutable (`release_rules` §7). Correct a mistake with a
new migration, never by editing one that has shipped.
