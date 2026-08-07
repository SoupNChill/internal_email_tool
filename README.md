# emaild

Internal transactional email API. One secure API, one honest timeline, one
dependable foundation for every product in the portfolio.

Sends through MXRoute over SMTP. Applications never see SMTP credentials.

## Status

**Phase 1 of 8 — foundation.** Schema, config, health endpoints, and container
setup. Not yet able to send: the ingest API lands in Phase 4 and the delivery
worker in Phase 5. See `build_plan.md`.

## Documents

| File | What it is |
|---|---|
| `vision.md` | What we're building and why |
| `spec_sheet.md` | What MXRoute actually does — verified, inferred, and unknown |
| `spike_results.md` | Observed SMTP behaviour, and the failure taxonomy it seeds |
| `build_plan.md` | The nine phases |
| `deployment_and_release.md` | Where it runs, and which release rules bind |
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
