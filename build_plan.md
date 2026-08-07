# Build Plan

**Companion to:** `vision.md` (what we're building and why), `spec_sheet.md` (what MXRoute actually does)
**Date:** 2026-08-05

## Decisions locked

| Decision | Choice | Consequence |
|---|---|---|
| Audience | Internal portfolio only | No signup, billing, sessions, or untrusted input. Projects and keys are admin objects. |
| Stack | Python · FastAPI · Postgres | `aiosmtplib`, `httpx`, SQLAlchemy + Alembic, Pydantic v2. |
| Providers | MXRoute only | Adapter boundary stays honest, but ships with one implementation. |
| Volume | Well under 400/hr per domain | **Postgres is the queue.** No Redis, no RabbitMQ, no Celery. |

### API contract: deliberately Resend-compatible

The stated use case is handing this API to an AI assistant scaffolding a new SaaS. That makes **wire-compatibility with Resend a feature, not an accident** — `vision.md`'s example is already ~90% of Resend's contract, so we close the remaining gap on purpose:

| Element | Shape |
|---|---|
| Endpoint | `POST /v1/emails` |
| Auth | `Authorization: Bearer em_live_…` |
| Body | `from`, `to[]`, `subject`, `html`, `text`, `reply_to`, `cc[]`, `bcc[]` |
| Response | `{"id": "...", "status": "queued"}` |
| Idempotency | `Idempotency-Key` request header |

Three payoffs: any coding assistant already knows this API without being shown docs and gets it right first try; migrating to real Resend or SES later is a base-URL change rather than a rewrite; and the contract is already proven ergonomic, so there's nothing to design.

Deviate only where honesty requires it — our `status` vocabulary reflects the real delivery lifecycle from `vision.md` rather than Resend's, because that difference is the entire point of the project.

### What this explicitly rules out

Not building: self-serve onboarding, billing, user accounts, session auth, webhooks (v1), multi-region, provider failover routing, or a message broker. Every one of these is a real cost with no internal-portfolio payoff. The dashboard sits behind network-level auth or a single admin token — not a login system.

### Runtime shape

Two processes against one Postgres:

```
  ┌──────────────┐         ┌──────────────┐
  │  API         │         │  Worker      │
  │  (uvicorn)   │         │  (asyncio)   │
  └──────┬───────┘         └──────┬───────┘
         │                        │
         └────────┬───────────────┘
                  ▼
            ┌───────────┐        ┌──────────────┐
            │ Postgres  │        │  MXRoute     │
            │ (state +  │        │  SMTP :465   │
            │  queue)   │        │  REST API    │
            └───────────┘        └──────────────┘
```

---

## Phase 0 — SMTP spike

**Nothing else starts until this lands.** Half a day. Account and test domain are ready, so this runs immediately.

A throwaway script — not production code, not in the final tree — that authenticates to `<server>.mxrouting.net:465` and captures verbatim provider responses.

**Resolves** open questions #1, #2, #3, #4, #5 from `spec_sheet.md`.

Deliberately trigger and record raw response lines for:
- Successful send
- Bad password
- Nonexistent recipient on an external domain
- Nonexistent recipient on our own domain
- Malformed sender / unauthorized `From`
- ~~Hourly limit exceeded — send 400+ to a sink address~~ — **cut.** Resolved from docs: permanent 5xx. Blasting 400 unauthenticated messages to prove a known answer would risk the shared IP for nothing.

Then measure: max concurrent connections, max messages per connection, whether the hourly window is rolling or fixed-clock.

**Output:** `spike_results.md` — a response corpus that becomes the failure-classification table in Phase 5.

**Answered ahead of the spike:** over-limit is a **permanent 5xx**. There is no provider-side recovery, so the limiter must guarantee we never reach the ceiling. Phase 5 inherits three hard requirements from this — hard gating rather than throttling, a rate-limit 5xx that re-queues instead of terminating, and fail-safe handling of any unrecognised 5xx. See `spec_sheet.md` §4a.

The spike is still worth running for the response corpus and connection limits — it is just no longer the gate it was.

---

## Phase 1 — Foundation

Project skeleton, config, secrets, and the complete schema. 1–2 days.

Full schema up front — the tables are cheap and retrofitting `events` or `idempotency_keys` later is not.

| Table | Purpose |
|---|---|
| `projects` | Which product is sending |
| `domains` | Sender domains + lifecycle state + cached DNS status |
| `mailboxes` | One per domain (enforced), encrypted SMTP credential, cached MX host |
| `api_keys` | Hashed keys, scoped to project/domain/sender |
| `messages` | Durable record + queue rows + envelope |
| `events` | Append-only transition log |
| `suppressions` | Addresses we refuse to send to |
| `idempotency_keys` | Replay protection |

**Config:** `.env` / Pydantic Settings. Two secrets need real handling — the MXRoute API key (account-root) and the mailbox-password encryption key. Neither is ever logged, and the API process should have no read path to mailbox passwords.

---

## Phase 2 — Control plane

MXRoute REST client and the domain lifecycle. 2–3 days.

- **`httpx` client** with the three auth headers, respecting `X-RateLimit-*`. Reads capped well under 100/min, writes under 20/min.
- **Domain lifecycle** as an explicit state machine matching `vision.md`: `added → ownership_pending → dns_incomplete → verified → ready → suspended/misconfigured`. Onboarding is inherently async (5–15 min DNS propagation), so state survives restarts — no in-request waiting.
- **Verification sweep:** scheduled, jittered, cached. Never on the send path.
- **`GET /domains/{d}/dns`** drives verification and yields the authoritative MX host — persisted per domain, never hardcoded.
- **DKIM absence is blocking.** The field is nullable; no DKIM means not `ready`.
- **Mailbox provisioning:** generate password, `POST` the account, store encrypted. **Enforce one mailbox per *sender identity*** — MXRoute pins the envelope sender to the login address exactly (`spike_results.md`, Finding 1), so an identity and a mailbox are one-to-one. Distinct real addresses are fine; minting mailboxes to multiply throughput for one identity is the policy violation.

---

## Phase 3 — Authorization

Scoped API keys. 1 day.

Key format `em_live_<32 bytes base62>`, shown once. Store `sha256(key)` plus a plaintext prefix for display.

> **Use SHA-256, not bcrypt/argon2.** Those exist to slow brute force against low-entropy human passwords. A 256-bit random key is not brute-forceable, so a slow hash buys nothing and adds latency to every request.

Resolve the vision's four questions on every request: who is calling, which domain, which sender identities, within limits? Track `created_at` / `last_used_at`; support naming and revocation.

---

## Phase 4 — Ingest API

`POST /v1/emails`. 2 days.

Ordering is the whole point: **validate → check suppression → durably write → return.** The response is only sent after the row is committed. No in-memory handoff, ever.

- Idempotency scoped to project; store request hash + response, replay identically for 24h. Unique constraint handles concurrent duplicates.
- Enforce recipient count (≤150), message size (≤50 MB), and sender authorization at the edge — all three are hard SMTP limits, and a queued message that violates one can never succeed. Reject at the API rather than accepting work that is guaranteed to 550.
- **Sender identity must match a provisioned mailbox exactly.** Envelope and `From:` address both, or DMARC alignment fails. Display name is unconstrained.
- Returns `{id, status: "queued"}`.

**Body retention:** bodies are stored because async delivery requires it, then purged by a sweeper shortly after terminal state. Metadata and events persist; content does not. This is what `vision.md` means by not building an accidental archive of password-reset links.

---

## Phase 5 — Delivery worker

The core. 3–4 days.

**Claim:** `SELECT … WHERE status='queued' AND next_attempt_at <= now() ORDER BY next_attempt_at FOR UPDATE SKIP LOCKED LIMIT n`, mark `sending` with worker id and timestamp.

**Crash recovery:** a reaper returns rows stuck in `sending` past a threshold back to `queued`. This is what satisfies "a worker crash must not lose a message" — no ack protocol needed, just a timestamp.

**Rate limiter:** token bucket per mailbox, persisted in Postgres so it is shared across workers and survives restart. Target 90% of 400/hr — our backpressure engages before MXRoute's does. Until spike #2 settles rolling-vs-fixed, assume rolling; it's the stricter reading.

**Send:** `aiosmtplib`, port 465 implicit TLS, username is the full address. Connection pooling bounded by spike #4.

**Classify:** table-driven from the Phase 0 corpus, kept as data so new provider strings don't need a code change. 4xx → retry with exponential backoff and jitter; 5xx → permanent stop.

**Adapters:** `MXRouteAdapter` and `SinkAdapter` behind one interface. The sink records events without connecting — that's test mode, and it costs almost nothing to build now versus burning real quota and reputation on every dev integration.

**Events** are emitted at every transition. They are the product, not a byproduct.

---

## Phase 6 — Suppression and return-path design

**Rescoped down.** ~1 day. At dev-tool volume the original framing overweighted this.

Ships now:
- **The VERP return-path scheme, pinned.** Non-negotiable and cheap — changing it later invalidates the envelope of every message already sent.
- **Suppression table, checked at ingest.** Populated by hand initially. Since bad external addresses come back `250 Accepted` (`spike_results.md`, Finding 2), this is the only brake that exists.

Deferred until volume justifies it: catch-all/forwarder provisioning, automated bounce parsing, and complaint handling. The hooks exist in the API whenever they're wanted.

### Original framing, for the record

**Do not defer the envelope design.**

Pin the VERP return-path scheme now — changing it later invalidates the envelope of every in-flight message. Bounce *processing* can lag; the scheme cannot.

Suppression is checked at ingest from Phase 4 onward, initially populated by hand. Automatic population arrives when bounce collection does, via catch-all/forwarders — both already provisionable through the API.

**This phase is the blast-radius control.** Shared IPs plus a zero-tolerance policy plus no automatic way to stop mailing dead addresses is how one bad integration takes down email for the entire portfolio at once. It is closer to load-bearing than `vision.md` suggests.

---

## Phase 7 — Observability

2 days.

- **Event timeline** per message — the honest one from `vision.md`, showing exactly where a message stopped.
- **Structured logs** with hard redaction of keys, passwords, and bodies.
- **Metrics:** volume by domain and project, failure rate over time, queue age, provider latency, retry rate, hourly-limit headroom.
- Health endpoints for API, worker, DB, and MXRoute reachability — separately, since the two planes fail independently.

---

## Phase 8 — Dashboard

2–3 days. Server-rendered, read-only, deliberately small.

Domain list with lifecycle state and missing DNS records. Message search with timeline. Key management. The metrics above. No editor, no campaigns, no charts for their own sake.

---

## Phase 9 — Production packaging

Governed by `release_rules/first_production_packaging.md`. Not started; listed so
Phases 2–8 build toward it instead of being retrofitted.

That document is explicit (§2, §28) that the audit comes **before** broad
implementation, so Phase 9 opens with five documents grounded in the actual repo,
not with code: `distribution_audit.md`, `production_packaging_plan.md`,
`persistent_data_inventory.md`, `configuration_inventory.md`,
`migration_risk_assessment.md`. Then work orders 2–12.

### Already satisfied in Phase 1

| Requirement | Where |
|---|---|
| §6 `.dockerignore`, multi-stage, non-root, no secrets in image | `Dockerfile`, `.dockerignore` |
| §10 installation identity, created once, survives upgrade | `installation` table |
| §11 Postgres persistence, health check, no public port | `docker-compose.yml` |
| §12 explicit ordered migrations, immutable once released | Alembic; upgrade tested with live data |
| §13 `/health/live`, `/health/ready`, `/version` + commit/build time | `emaild/health.py` |
| §15 stdout logging, no secrets | `emaild/logging_config.py` |
| §9 secrets outside the image, validated at startup | `emaild/config.py` |

### Deliberately deferred

- **§8 production compose from published images.** The current file uses
  `build:`, which is right for development and explicitly wrong for production.
  Phase 9 adds `deploy/compose.yaml` pinned to `ghcr.io/soupnchill/emaild:X.Y.Z`.
- **§16 `appctl`** — start/stop/status/version/health/logs/config-check/backup/
  restore/doctor.
- **§17–18 backup and restore**, with manifest and checksums. §24 requires
  restore onto a *clean* machine; a restore that only works on the original host
  does not count.
- **§21 GitHub Actions.** PR validation plus a separately authorised release
  workflow. Not added yet on purpose: §54 forbids shipping commands that have not
  been tested, and CI cannot be honestly tested from here before there is
  something for it to run.

### Constraints this imposes on earlier phases

1. **Phase 5 worker must shut down gracefully (§14).** Stop claiming new work on
   SIGTERM, finish or safely release in-flight messages, exit within a documented
   timeout. A worker killed mid-send must leave the message claimable by the
   reaper, never silently lost.
2. **Nothing irreplaceable outside the Postgres volume (§5).** Everything must be
   reachable by `pg_dump` plus the encryption key — no state in container layers,
   temp dirs, or the source checkout.
3. **No hard-coded production hostnames (§20).** The base URL is configuration.
4. **One authoritative version string (§22).** `emaild/__init__.py`, referenced
   by the image, `/version`, and the backup manifest.

## Sequence

```
Phase 0  SMTP spike            ✓ DONE  → spike_results.md
Phase 1  Foundation            ▓▓ 1–2d
Phase 2  Control plane         ▓▓▓ 2–3d
Phase 3  Authorization         ▓ 1d
Phase 4  Ingest API            ▓▓ 2d
Phase 5  Delivery worker       ▓▓▓▓ 3–4d
Phase 6  Suppression + VERP    ▓ 1d       (rescoped down)
Phase 7  Observability         ▓▓ 2d
─────────────────────────────────────── first perfect mile
Phase 8  Dashboard + snippets  ▓▓▓ 2–3d
```

Roughly **12–17 working days** to the complete journey in `vision.md` §"The first perfect mile", plus the dashboard.

Phase 8 also ships a one-page README with curl / Python / JS snippets — the artifact actually pasted to an AI assistant when scaffolding a new product, and the highest-leverage documentation in the project.

Phases 3 and 4 can overlap. Phase 7 can start during 5, since events already exist by then.

## Mapping to the vision's ten steps

| Vision step | Phase |
|---|---|
| 1. Builder adds a domain | 2 |
| 2. System verifies it's authorized and ready | 2 |
| 3. Builder creates a scoped API key | 3 |
| 4. Application submits an email | 4 |
| 5. API durably accepts, returns stable ID | 4 |
| 6. Worker delivers through MXRoute | 5 |
| 7. Temporary failures retried safely | 5 |
| 8. Permanent failures classified correctly | 0 → 5 |
| 9. Dashboard shows honest timeline | 7 |
| 10. Metrics reflect what happened | 7 |

## Open risks

1. **Spike #1 unresolved** — retry design is provisional until over-limit behavior is measured. Phase 0 exists to close this.
2. **Concentration risk** — one account, shared IPs, termination-level policy. Phase 6 is the mitigation; treat it as required, not optional.
3. **400/hr is a hard ceiling.** Adding mailboxes to exceed it is prohibited. Growth comes from more domains, or a second adapter behind the Phase 5 boundary.
4. **No delivery confirmation exists.** `accepted_by_provider` is genuinely terminal for most messages. The honest vocabulary in `vision.md` is not pessimism — it is the actual limit of what is knowable.
