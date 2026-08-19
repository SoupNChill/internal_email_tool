# Phase 0 — SMTP Spike Results

> Server responses below are verbatim except that the real test domain and
> mailbox have been replaced with `example.com` / `example.net` throughout.
> Only the identifiers changed; every status code and message is as captured.

**Run:** 2026-08-06 · `chocobo.mxrouting.net:465` · `sender@example.com`
**Server:** Exim 4.99.1 · TLS 1.3 (`TLS_AES_256_GCM_SHA384`, 256-bit)
**Messages actually delivered:** 1. All other cases stopped at `RSET` before `DATA`.

## Headline: the server advertises its own limits

```
250-SIZE 52428800
250-LIMITS MAILMAX=100 RCPTMAX=150
250-AUTH PLAIN LOGIN
250-PIPELINING PIPECONNECT 8BITMIME
```

Four open questions answered by the EHLO banner:

| Question | Answer |
|---|---|
| Max message size | **50 MB** (52,428,800 bytes) |
| Max messages per connection | **100** (`MAILMAX`) |
| Max recipients per message | **150** (`RCPTMAX`) |
| Auth mechanisms | `PLAIN`, `LOGIN` |

**Design consequence:** parse `LIMITS` from EHLO at connect time and adapt, rather than hardcoding. The values arrive free on every connection and stay correct if MXRoute ever changes them. Hardcoding would mean discovering a change as production failures.

`PIPELINING` and `PIPECONNECT` are both offered — worth having later if throughput ever matters, irrelevant at current volume.

## Observed responses — the classifier table

Verbatim, and the basis for Phase 5's failure classification:

| Probe | Code | Response | Class | Retry? |
|---|---|---|---|---|
| Auth, correct password | 235 | `Authentication succeeded` | — | — |
| Auth, wrong password | **535** | `Incorrect authentication data` | `auth_failure` | **No** — alert loudly, credential is wrong |
| Send, valid | 250 | accepted, no refusals | `accepted` | — |
| 2nd txn, same connection | 250 | `OK` | — | connection reuse confirmed |
| RCPT, bad *local* mailbox | **550** | `No such recipient here` | `recipient_rejected` | **No** |
| RCPT, bad *external* domain | **250** | `Accepted` | see below | — |
| MAIL FROM `spoofed@gmail.com` | **550** | `Mail from gmail.com is only accepted from authorized IP ranges` | `sender_unauthorized` | **No** — our bug |
| MAIL FROM other owned domain | **550** | `Envelope sender other@example.net must match your login sender@example.com` | `sender_mismatch` | **No** — our bug |
| 8 concurrent authenticated connections | — | all succeeded | — | probe ceiling, not a server maximum |

## Finding 1 — envelope sender is pinned to the login mailbox

> `550 Envelope sender other@example.net must match your login sender@example.com`

MXRoute enforces that `MAIL FROM` equals the authenticated mailbox **exactly**. Not the same domain — the same address.

**Consequences:**

1. **A mailbox is a sender identity, one-to-one.** Sending as both `noreply@x.com` and `support@x.com` requires two mailboxes. This is not optional and no amount of application design routes around it.
2. **This revises the earlier "one mailbox per domain" rule** in `build_plan.md` Phase 2. The correct invariant is **one mailbox per *sender identity***. That remains policy-compliant — MXRoute prohibits minting mailboxes to multiply *throughput* for one identity, not having distinct addresses that genuinely exist.
3. **The `From:` header must match the envelope**, otherwise DMARC alignment fails even with SPF and DKIM passing. Display names are free (`Acme <noreply@x.com>`); the address is not.
4. **Validate at ingest, not at send.** A sender mismatch is a configuration error, and 550 is unrecoverable — a queued message that can never succeed is worse than a 400 at the API. The vision's "which sender identities are permitted?" check becomes a hard precondition.

MXRoute enforcing this is a gift, not an obstacle: it makes sender spoofing impossible at the protocol level rather than something our authorization layer has to get right alone.

## Finding 1a — VERP is unavailable (tested 2026-08-07)

A follow-up probe, because the whole bounce design depended on it:

| Envelope sender | Result |
|---|---|
| `noreply@domain` (exact login) | **250 Accepted** |
| `noreply+email01JABCDEF@domain` | **550** must match your login |
| `noreply+bounces@domain` | **550** must match your login |
| `bounce@domain` | **550** must match your login |

Plus-addressing is rejected exactly as a different local part is. "Must match
your login" means byte-identical, with no subaddressing exemption.

**Consequence: the VERP return-path scheme planned for Phase 6 cannot be built.**
The envelope sender is forced to equal the mailbox address, so bounces cannot
carry a per-message tag in the return path.

**Replacement: attribute bounces by `Message-ID`.** We author the header as
`<{public_id}@{sending_domain}>`, and because `public_id` is already unique and
indexed, a DSN quoting the original Message-ID resolves to its message with one
lookup. No extra column, and nothing to migrate later.

Bounces will arrive in the sending mailbox itself, since that is now the only
possible return path. Whoever builds bounce processing reads that mailbox over
IMAP and parses the DSN rather than reading a tag off the envelope.

Worth noting this was only discoverable by trying it: the constraint is
documented nowhere, and the plus-addressing case is the sort of thing that is
usually exempt.

## Finding 2 — external recipients are accepted without validation

`nobody@nonexistent-domain-xyz-99823.invalid` returned **250 Accepted** at RCPT TO.

Exim accepts external recipients at submission and resolves deliverability later. So:

- **Bad external addresses are undetectable at send time.** No synchronous signal exists.
- **They bounce asynchronously, out of band**, into a mailbox we are not yet collecting.
- Only *local* recipients (on our own domains) fail fast, with `550 No such recipient here`.

This is the empirical confirmation of the vision's `Unknown after provider acceptance` state. `250 Accepted` genuinely means "MXRoute took custody," nothing more — and being honest about that is the whole thesis of `vision.md`.

It also means bounce collection is the *only* channel through which we ever learn an address is dead. That doesn't make it urgent at low volume, but it does make the VERP return-path decision in Phase 6 the one piece that must not be deferred, since changing it later invalidates the envelope of every message already sent.

## Remaining unknowns

| # | Question | Status |
|---|---|---|
| 1 | Exact response string on hourly-limit 5xx | **Still unknown.** Requires deliberately blowing the limit. Worker fails safe: unrecognised 5xx re-queues and flags rather than dropping. |
| 2 | Hourly window rolling or fixed-clock | Unknown. Assume rolling — the stricter reading. |
| 3 | True concurrent-connection ceiling | ≥8. Untested beyond that; far above any realistic need here. |
| 4 | `sent` counter reset boundary (UTC vs local) | Unknown. Low impact. |

## Config values confirmed for production

```python
SMTP_HOST = "chocobo.mxrouting.net"   # read per-domain from GET /domains/{d}/dns
SMTP_PORT = 465                        # implicit TLS, negotiated TLS 1.3
SMTP_USER = "<full email address>"     # == envelope sender, enforced
MAX_SIZE_BYTES   = 52_428_800          # prefer parsing EHLO SIZE
MAX_RCPT_PER_MSG = 150                 # prefer parsing EHLO RCPTMAX
MAX_MSG_PER_CONN = 100                 # prefer parsing EHLO MAILMAX
```

## Deliverability check — needs a human

One message was delivered to the test inbox. Worth confirming by hand, since it validates the whole DNS chain:

1. Did it arrive — inbox or spam?
2. In Gmail: **Show original** → expect `SPF: PASS`, `DKIM: PASS`, `DMARC: PASS`.

DKIM was verified byte-for-byte against the API before sending, so a DKIM failure here would point at signing rather than publication.
