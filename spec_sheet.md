# MXRoute Integration Spec Sheet

**Status:** Pre-implementation. Compiled from MXRoute's public documentation and the bundled `mxroute_api.yaml`.
**Date compiled:** 2026-08-05
**Purpose:** The "wire it up" reference MXRoute does not publish — the concrete values, limits, and policy constraints needed to build against them, plus an explicit list of what still has to be measured.

## Confidence legend

Every claim below carries one of these. Do not let them blur together.

| Mark | Meaning |
|---|---|
| **[DOC]** | Stated in MXRoute's official documentation. |
| **[SPEC]** | Stated in the bundled `mxroute_api.yaml`. |
| **[INFER]** | Derived from two or more sources agreeing. Sound, but not stated anywhere directly. |
| **[OPEN]** | Unknown. Must be measured against a live account before it can be relied on. |

---

## 1. The system spans two planes

MXRoute exposes two entirely separate integration surfaces. They share a brand and nothing else — different hostnames, different credentials, different protocols, different failure modes.

| | Control plane | Data plane |
|---|---|---|
| **Protocol** | HTTPS REST | SMTP |
| **Endpoint** | `https://api.mxroute.com` **[SPEC]** | `<server>.mxrouting.net` **[DOC]** |
| **Credential** | `X-Server` / `X-Username` / `X-API-Key` **[SPEC]** | Mailbox address + password **[DOC]** |
| **Scope** | Account-wide, unscoped | Per mailbox |
| **Used for** | Domains, mailboxes, DNS records, quota | Sending every message |
| **Documented?** | Yes — `mxroute_api.yaml` | Only as mail-client setup instructions |

**Design consequence:** the provider adapter named in `vision.md` has two halves that fail independently. A control-plane outage blocks provisioning and verification but must not block sending. Health-check them separately.

---

## 2. Control plane (REST API)

### Authentication
Three headers on every request **[SPEC]**:

```http
X-Server:   eagle.mxlogin.com      # DirectAdmin panel hostname
X-Username: johndoe                # DirectAdmin username
X-API-Key:  Mx8d989005f0cded83...  # from panel.mxroute.com/api-keys.php
```

### Rate limits **[SPEC]**
| Operation | Limit |
|---|---|
| Read (GET) | 100 / minute |
| Write (POST, PATCH, DELETE) | 20 / minute |

Every response carries `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`.

### Endpoints that matter to us
| Endpoint | Use |
|---|---|
| `GET /verification-key` | Domain ownership TXT record, format `_da-verify-{32 hex}` |
| `GET /domains` · `POST /domains` | Domain lifecycle |
| `GET /domains/{d}/dns` | **SPF, DKIM, MX records — the source of truth for verification** |
| `POST /domains/{d}/email-accounts` | Provision a sender mailbox; we choose the password |
| `GET /domains/{d}/email-accounts/{u}` | Live `sent` / `limit` counters |
| `PATCH /domains/{d}/email-accounts/{u}` | Rotate password, adjust quota/limit |
| `GET /domains/{d}/catch-all` · `/forwarders` | Bounce-collection plumbing (future) |

### Domain onboarding order is fixed **[DOC]**
The verification TXT record must exist **before** `POST /domains` will succeed:

1. `GET /verification-key`
2. User adds TXT `{key}.theirdomain.com` = `domain-verified`
3. Wait for DNS propagation — MXRoute says 5–15 minutes
4. `POST /domains`
5. `GET /domains/{d}/dns` → surface SPF/DKIM/MX for the user to add
6. Poll until all records resolve → state becomes `Ready to send`

This maps cleanly onto the domain lifecycle in `vision.md`. Note that step 3 makes onboarding inherently asynchronous — the state machine needs a `Ownership pending` state that survives process restarts, not an in-request wait.

---

## 3. Data plane (SMTP)

### Hostname — read it from the API, never hardcode

The API auth header uses a **`.mxlogin.com`** hostname. The SMTP server is a **`.mxrouting.net`** hostname, documented as identical to the domain's primary MX record. **[DOC]**

These are different names. Do not assume one can be derived from the other by string substitution.

**Authoritative resolution:** `GET /domains/{domain}/dns` returns `mx_records[]`. That is the SMTP host, straight from the API, per domain. **[INFER]**

**Confirmed against the live account (2026-08-05):** `X-Server` and the primary MX are the *same* host — `chocobo.mxrouting.net` — with `chocobo-relay.mxrouting.net` as backup MX. So on this account the two planes share a hostname. Still read it from the API per domain rather than assuming: the account is on `chocobo`, but nothing guarantees that for a future account or a migrated domain, and the cost of reading it is one cached call.

This is worth calling out as a win: the single biggest undocumented value is in fact discoverable programmatically. Provisioning should read it and persist it per domain rather than carrying it in config.

### Ports **[DOC]**
| Port | Encryption | Notes |
|---|---|---|
| **465** | Implicit SSL/TLS | **Recommended.** MXRoute's own client docs specify this. |
| 587 | STARTTLS | Standard submission port. |
| 2525 | STARTTLS | Fallback where 587 is blocked. |
| 25 | STARTTLS | Avoid — widely blocked outbound by hosting providers. |

**Default to 465 implicit TLS.** It matches MXRoute's own guidance and removes the STARTTLS-stripping failure mode entirely.

### Credentials **[DOC]**
- **Username:** the full email address, not the local part.
- **Password:** whatever we set at `POST /domains/{d}/email-accounts`. Must be 8+ chars with upper, lower, and a digit **[SPEC]**.
- **Auth is mandatory** on all submission ports.

There is no credential-discovery problem. We generate the password, store it encrypted, and hand it to the worker. Rotation is a `PATCH` away.

---

## 4. Sending limits — the binding constraint

### The number

**400 outbound emails per hour, per email address.** **[DOC]**

This is corroborated by the API spec independently: the mailbox `limit` field defaults to `9600` with `maximum: 9600` **[SPEC]**, and 400 × 24 = 9,600 exactly. Two sources, two units, one consistent ceiling. **[INFER]**

> One search result cited 300/hour. It is contradicted by MXRoute's own limits page, their core FAQ, and the API's own maximum. Treating 400 as correct, flagged here only so nobody rediscovers the discrepancy and assumes the doc is stale.

### What it means in practice

| Window | Ceiling per sender identity |
|---|---|
| Per minute | ~6.6 |
| Per hour | 400 |
| Per day | 9,600 |

**The hourly limit governs burst; the daily limit governs sustained volume. Both need enforcing.** A worker that respects only the daily cap will trip the hourly one inside four minutes of a backlog drain.

The worker's rate limiter should be built around 400/hour from the start, with a configurable safety margin (suggest 90%) so that our own throttle engages before MXRoute's does. We control the shape of our own backpressure; we do not control theirs.

## 4a. Over-limit behaviour — resolved, and it is the strict case

**Hitting the 400/hour limit produces a permanent 5xx rejection. The message is not queued, not deferred, and not retried by MXRoute.** **[DOC]**

This is the worse of the two possibilities and it promotes our rate limiter from an optimisation to a safety-critical component:

1. **The limiter is a hard gate, not a throttle.** A message must never be *attempted* while at the ceiling. Once MXRoute answers 5xx, the send is gone — there is no provider-side recovery. Hold in our own queue and release as headroom returns.
2. **This is the one case where a 5xx must be treated as retryable.** Naively classifying "5xx ⇒ permanent ⇒ stop" would silently discard legitimate mail during a burst. The classifier needs to distinguish a rate-limit 5xx from a genuine recipient rejection — the former re-queues, the latter is terminal.
3. **We need the exact response string to make that distinction**, and we cannot get it without deliberately blowing the limit. Until it is captured, the worker should fail *safe*: any 5xx that is not positively matched against a known-permanent pattern gets re-queued with backoff and flagged for review, rather than dropped.
4. **The 90% safety margin is mandatory**, not prudence. Our backpressure must engage before MXRoute's does.

### Observability
`GET /domains/{d}/email-accounts/{user}` returns live `sent` and `limit` fields **[SPEC]**. This enables genuine pre-flight checking rather than discovering the ceiling as SMTP rejections — but it is a *daily* counter, so it cannot detect hourly-limit proximity. Track hourly consumption locally.

---

## 5. Policy constraints — read this before designing capacity

MXRoute's acceptable-use position is unusually blunt, and it directly constrains the architecture.

### Transactional email is explicitly permitted **[DOC]**
Their core FAQ names "order confirmations, password resets, notifications" and "automated business processes" as supported use. The intended use case for this project is squarely inside their policy. Good.

### Marketing email is prohibited with zero tolerance **[DOC]**
"Zero tolerance for marketing emails, unsolicited outreach, or spam." Violation results in account termination, not a warning. The `What we will not become` section of `vision.md` already rules this out — that constraint is now externally enforced too, with the entire account as collateral.

### ⚠️ Correction to earlier advice: mailbox-per-project is a policy violation

In my initial review I suggested provisioning a mailbox per project to isolate send quota. **MXRoute explicitly prohibits this.** Their documentation states that creating additional addresses to circumvent the hourly limit is not permitted and results in account termination. **[DOC]**

The distinction that matters:

| Pattern | Standing |
|---|---|
| One mailbox per legitimate sender identity (`noreply@customer-domain.com`) | Fine — each domain genuinely needs its own sender. |
| Multiple mailboxes on one domain to multiply the 400/hr budget | **Prohibited. Account termination.** |

**Therefore: 400/hour per sender identity is a hard ceiling, not an engineering problem to route around.** Capacity scales by adding legitimate sending domains, not by sharding mailboxes. If a single product needs more than 400/hour from one domain, MXRoute is the wrong provider for that product and the answer is the provider-abstraction boundary already in `vision.md` — add a second adapter, not a second mailbox.

This should be written into the system as an enforced invariant, not a guideline: **one sending mailbox per domain**, with provisioning refusing to create a second.

### Shared IP reputation **[DOC]**
MXRoute states they own their IP ranges and "militantly monitor" outbound IPs. Sending is from shared IPs. This validates the vision's refusal to promise inbox placement: our reputation is partly a function of other tenants' behavior, and no amount of correct engineering on our side fully controls it.

---

## 6. Open questions — resolve by spike, not by reading

None of these are documented anywhere. All of them answer themselves in a single session against a live account with a scratch domain.

| # | Question | How to measure |
|---|---|---|
| ~~1~~ | ~~Exact response when the hourly limit is hit?~~ | **RESOLVED [DOC] — permanent 5xx rejection, not a 4xx deferral.** See §4a below. Exact response string still unknown. |
| 2 | Is the hourly window rolling or a fixed clock-hour bucket? | Send 400 at :50, retry at :01. Success ⇒ fixed bucket. |
| 3 | Verbatim strings for auth failure, bad recipient, deferral | Deliberately trigger each; record raw response lines. Seeds the failure classifier. |
| 4 | Max concurrent SMTP connections per mailbox | Ramp parallel connections until refusal. Sets worker concurrency. |
| 5 | Max messages per connection before forced reconnect | Hold one connection, send until refusal. |
| 6 | Max message size | Binary-search with padded bodies. |
| 7 | Max recipients per message (RCPT TO) | Add recipients until rejection. |
| 8 | Is 9,600/day enforced separately, or purely 400×24? | Only observable across a full day. Low priority. |
| 9 | Minimum TLS version accepted on 465 | Negotiate down; confirm 1.2 floor. |
| 10 | Does `sent` reset at midnight UTC or server-local? | Poll the counter across a midnight boundary. |

**Spike sequence:** #3 and #1 first — they define the failure taxonomy that everything downstream depends on. #4 and #5 next, since they set worker concurrency. The rest are refinements.

---

## 7. Design implications for `vision.md`

1. **Failure classification must be derived, not invented.** Build the taxonomy from the strings captured in spike #3, and keep it a data table that extends without a code change — providers alter response wording without notice.

2. **Over-limit handling depends entirely on open question #1.** If MXRoute defers (4xx), the queue absorbs it naturally. If it rejects (5xx), our own limiter must guarantee we never reach the ceiling, because there is no recovery path. Do not design the retry policy before measuring this.

3. **Domain verification must be cached and swept, never checked inline.** With 100 reads/minute shared across the whole account, per-send verification does not scale. Sweep on a schedule with jitter; cache aggressively.

4. **DKIM is `nullable` in the DNS schema** **[SPEC]** — a domain can legitimately return no DKIM record. `Ready to send` must treat its absence as blocking.

5. **Bounce collection is provisionable now.** `catch-all` and `forwarders` both exist in the API **[SPEC]**. Even deferring bounce *processing*, pin the return-path/VERP scheme in v1 — changing it later invalidates the envelope of every in-flight message.

6. **The control-plane credential is root.** It has no scoping: the key that reads DNS can also delete mailboxes and suspend reseller users. It warrants isolation from every other secret in the system, and it should never be reachable from the send path.

7. **Test mode should move earlier than "future."** With a hard 400/hour ceiling and termination-level policy enforcement, letting developer integrations consume real quota against real reputation is an unnecessary risk. A sink adapter that records events without connecting to SMTP is cheap now and awkward to retrofit.

---

## 8. Sources

- [Service Limits — MXroute Documentation](https://docs.mxroute.com/docs/presales/limits.html)
- [Pre-Sales FAQ Core — MXroute Documentation](https://docs.mxroute.com/docs/presales/faq-core.html)
- [Quick Setup Guide — MXroute Documentation](https://docs.mxroute.com/docs/quick-setup.html)
- [Configuring Microsoft Outlook — MXroute Documentation](https://docs.mxroute.com/docs/general/outlook.html)
- `mxroute_api.yaml` (bundled, OpenAPI 3.0.3, v1.0.0)

## 9. Live-account findings (2026-08-05)

Verified by direct API call against the working account:

- **Account host:** `chocobo.mxrouting.net`. Three domains present — the shared-reputation concern in §5 is live, not hypothetical.
- **DKIM is pre-generated**, selector `x`, RSA-2048. It exists server-side before it is published in DNS, so `dkim: null` in the schema likely means "not generated for this domain" rather than "unsupported". Do not treat a populated DKIM field as proof the record is live in DNS — those are independent facts, and only a DNS lookup settles the second.
- **DMARC is not supplied by MXRoute.** The `DnsInfo` schema has no DMARC field and the live response has none. It is on us to author. Verification logic must not wait for a DMARC record from the provider that will never arrive.
- **Spec divergence:** `GET /domains/{d}/mail-status` is documented in `mxroute_api.yaml` but returns `405 METHOD_NOT_ALLOWED` live. The spec is not a reliable contract — treat every endpoint as unverified until called, and let the client degrade rather than assume availability.

## 10. Sources

**Not retrieved:** `community.mxroute.com` refused connections during compilation, and `mxroute.com/policy.html` returned 404. Two threads there are worth a manual read — *Outgoing Email Limits* (`/t/outgoing-email-limits/281`) and *Clarification on Policy for Transactional emails* (`/t/clarification-on-policy-for-transactional-emails/1431`) — as they may resolve open question #1 without a spike.
