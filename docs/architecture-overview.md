# Architecture Overview

## Shape

```
   your product                        ┌──────────────────┐
        │  POST /v1/emails             │    MXRoute       │
        ▼                              │                  │
   ┌─────────┐                         │  REST  (admin)   │
   │   api   │  no SMTP, no MXRoute key│  SMTP  :465      │
   └────┬────┘                         └────▲────────▲────┘
        │ writes                            │        │
        ▼                                   │        │
   ┌──────────┐      claims           ┌──────┴───┐    │
   │ postgres │◄─────────────────────►│  worker  │────┘
   │  state + │                       │ no port  │  outbound only
   │  queue   │                       └──────────┘
   └──────────┘
        ▲
        │  provisioning only, never publicly routed
   ┌────┴────┐
   │  admin  │  holds the MXRoute account-root key
   └─────────┘
```

## Three roles, three sets of secrets

Enforced at startup — a process given a secret it must not hold **refuses to
start**.

| Role | Mailbox key | MXRoute admin key | Listens |
|---|---|---|---|
| `api` | forbidden | forbidden | yes, public |
| `worker` | required | forbidden | no |
| `admin` | required | required | no (CLI) |

So compromising the internet-facing surface yields sending within existing key
scopes — not the ability to delete mailboxes or manipulate reseller users.

## Postgres is the queue

No broker. Workers claim with `SELECT … FOR UPDATE SKIP LOCKED`, and a reaper
returns rows stuck in `sending` past 15 minutes. That is the crash-safety
mechanism: a timestamp, not an acknowledgement protocol, because a timestamp is
what survives `kill -9`.

At a few hundred messages a day, a broker would be a moving part with nothing to
do.

## The request path

```
validate → authorize → limits → suppression → COMMIT → respond
```

The response is sent only after the transaction commits. `queued` means the row
exists — not that anything was sent.

Everything knowably impossible is refused **before** the write. 151 recipients
or a 51 MB body can never succeed at SMTP, so a 422 now beats an asynchronous
550 later in a place the caller has stopped watching.

## Provider constraints that shaped the design

All verified against a live account; see `spec_sheet.md` and `spike_results.md`.

**400 sends/hour per sender identity, and over-limit is a *permanent* rejection
with no provider-side queue.** So the rate limiter is a hard gate, not a
throttle — a message is held back rather than allowed to fail. We enforce 90% of
the ceiling so our backpressure engages before theirs.

**The envelope sender must equal the SMTP login exactly.** A mailbox *is* a
sender identity, one-to-one. Plus-addressing is rejected too, which is why
bounce attribution uses a `Message-ID` we author rather than VERP.

**Bad external recipients return `250 Accepted`** and bounce out of band. This is
why `accepted_by_provider` is terminal and does **not** mean delivered, and why
the suppression list is the only brake that exists.

## Status vocabulary

The one place emaild deliberately differs from other email APIs.

| Status | Means |
|---|---|
| `queued` | Durably stored. Not sent. |
| `sending` | A worker is delivering it now. |
| `accepted_by_provider` | The provider took custody and said `250`. **Terminal.** Not "delivered". |
| `temporarily_failed` | Retryable failure; will retry with backoff. |
| `permanently_rejected` | Will not be retried. |

Nothing here claims delivery it cannot demonstrate.

## Data that cannot be rebuilt

Only three things:

- **Event history** — the audit trail is the product.
- **Suppression list** — accumulated knowledge; rebuilding means re-learning it
  by mailing dead addresses again on shared IPs.
- **The mailbox encryption key** — recoverable only by re-provisioning every
  sender identity.

Everything else regenerates from the MXRoute API and DNS. See
[backup-and-restore.md](backup-and-restore.md).

## Message bodies are temporary

Stored because asynchronous delivery requires it, then purged 72 hours after the
message reaches a terminal state. Metadata and the timeline persist. Password
reset links and verification tokens must not become a permanent archive.
