# Troubleshooting

Symptom first. Every command here has been run against a live installation.

Start with:

```bash
./appctl doctor
```

---

## Mail is queued but nothing is being delivered

**Check queue age first** — it is the one number that distinguishes the causes.

```bash
./appctl status
```

### `oldest pending` is climbing and there are no workers

```
WORKERS
  ! none have ever reported -- is the worker running?
```

The worker is not running or cannot reach the database.

```bash
./appctl logs worker | tail -30
docker compose up -d worker
```

### Workers are `STALE`

The process is alive but its loop has stopped, or it lost the database.

```bash
./appctl logs worker --since 10m
docker compose restart worker
```

### Messages sit in `queued` with `delivery.rate_gated` events

Working as designed. The sender identity is at its hourly ceiling and messages
are being **held rather than allowed to fail** — over-limit at MXRoute is a
permanent rejection, not a deferral.

```bash
./appctl status        # see HOURLY HEADROOM
```

They resume as the rolling hour frees slots. If this is constant, you are
exceeding 400/hour for one identity, and the answer is another sending domain or
a second provider — not more mailboxes, which violates MXRoute's policy.

---

## A send is rejected by the API

The error `type` tells you which.

| `type` | Meaning | Fix |
|---|---|---|
| `authentication_error` | Key missing, malformed, unknown, or revoked | Issue a new key |
| `authorization_error` | Key may not send as that `from` | The message lists what it *can* use |
| `domain_not_ready` | The domain's DNS is incomplete | Publish records — do **not** widen the key |
| `validation_error` | Bad field | `param` names it |
| `suppressed_recipient` | Recipient is on the suppression list | Check why before removing |
| `idempotency_key_reused` | Same key, different body | Use a new key |
| `idempotency_conflict` | Identical request in flight | Retry shortly |

### `domain_not_ready`

```bash
./appctl admin domains verify example.com
```

It names which checks fail. Common causes:

- **DKIM fails but the record "looks right"** — you pasted it with the quotes.
  Most registrars add their own, so a double-quoted key resolves fine and fails
  verification. Re-paste without them.
- **MX missing** — Cloudflare Email Routing is enabled and publishing its own MX
  records. Disable it.
- **SPF fails** — more than one SPF record. RFC 7208 permits exactly one;
  receivers treat two as permerror, which is worse than none.

---

## Messages fail immediately

```bash
./appctl admin status     # FAILURES BY CLASS
```

| Class | Meaning | Action |
|---|---|---|
| `auth_failure` | The stored SMTP password is wrong | `mailboxes rotate <address>` |
| `sender_mismatch` | Envelope did not equal the login | A bug on our side — check the timeline |
| `recipient_rejected` | The address does not exist | Usually auto-suppressed |
| `policy_rejected` | Content or reputation | Review what is being sent |
| `rate_limited` | Hit the ceiling on the wire | The gate should have prevented this — report it |
| `connection` | Network or TLS | Check outbound 465 |
| `unknown` | An unrecognised response | **Flagged for review** — see below |

### Messages flagged `needs_review`

An unrecognised 5xx. Rather than deciding it was fatal, emaild re-queues it and
flags it, because a wrong permanent-failure decision destroys mail silently.

Find them on `/messages?review=1`, read `provider_response`, and if the wording
is a genuine permanent failure, it belongs in the classifier
(`emaild/delivery/classify.py`).

---

## Outbound SMTP is blocked

```bash
timeout 5 bash -c 'cat < /dev/null > /dev/tcp/chocobo.mxrouting.net/465' \
  && echo reachable || echo BLOCKED
```

If blocked, emaild cannot deliver from this host at all. Some providers block
outbound SMTP by default and will open it on request.

---

## Mail is accepted but never arrives

`accepted_by_provider` means MXRoute took custody, **not** that anything was
delivered. Bad external recipients are accepted at RCPT and bounce out of band.

Check in order:

1. **Spam folder.** Then verify SPF, DKIM, and DMARC all pass — in Gmail, open
   the message and use *Show original*.
2. **The recipient address exists.** A typo is accepted at RCPT and bounces
   later, invisibly.
3. **Your DNS is still correct** — `domains verify`. A `ready` domain that
   silently became `misconfigured` means something changed outside emaild.

emaild cannot tell you whether a message reached an inbox. Nothing can, on this
provider — which is why the status vocabulary stops where it does.

---

## The API will not start

```bash
docker compose logs api | tail -30
```

Startup refusals are deliberate and name the fix. The most common:

- *"role=api must not be given MXROUTE_\* credentials"* — the account-root key
  belongs only to the admin path.
- *"refusing to serve an unauthenticated dashboard in production"* — set a
  token, acknowledge proxy auth, or disable the dashboard.
- *"role=worker requires EMAILD_MAILBOX_ENCRYPTION_KEY"* — the worker cannot
  decrypt credentials without it.

```bash
./appctl config-check
```

## `/health/ready` returns 503

```json
{"status":"not_ready","checks":{"database":"ok","schema":"migrations_pending"}}
```

| `schema` | Meaning |
|---|---|
| `no_migrations_applied` | The migrate step never ran |
| `migrations_pending` | Image is newer than the database — run migrations |
| `schema_ahead_of_application` | Database migrated by a **newer** build. Do not downgrade the image; roll forward. |

That last one refuses traffic on purpose: an older application writing against a
newer schema is a data-corruption risk, not a warning.

---

## Restore fails

### `encryption key mismatch`

Working as intended. Restoring would leave every mailbox credential
undecryptable. Supply the `EMAILD_MAILBOX_ENCRYPTION_KEY` that matches the
archive's fingerprint — see [backup-and-restore.md](backup-and-restore.md).

### `refusing to overwrite an active installation`

Also intended. It names how many messages would be destroyed. Use `--force` only
if you mean it.

### `checksum mismatch`

The archive is corrupt or was modified. Use another one. This is the reason to
keep more than one.

---

## Collecting diagnostics

```bash
./appctl doctor > diagnostics.txt 2>&1
./appctl logs --tail 500 >> diagnostics.txt 2>&1
```

Logs are redacted, but read the file before sending it anywhere.
