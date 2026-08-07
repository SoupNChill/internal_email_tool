# Sending email with emaild

Paste this file to a coding assistant, or read it in two minutes. It is the
whole integration.

The API is **wire-compatible with Resend**, so if you or your assistant already
know that shape, you already know this one. Change the base URL and it works.

---

## Send an email

```
POST {BASE_URL}/v1/emails
Authorization: Bearer em_live_...
Content-Type: application/json
```

```json
{
  "from": "Acme <noreply@example.com>",
  "to": "customer@example.net",
  "subject": "Verify your email",
  "html": "<p>Click <a href=\"...\">here</a> to verify.</p>",
  "text": "Click here to verify: ..."
}
```

```json
{ "id": "email_01KZDD5PWYYJNQPPWHN9YYYB96", "status": "queued" }
```

### Fields

| Field | Required | Notes |
|---|---|---|
| `from` | yes | Must be a sender identity your key is scoped to. `Name <addr>` or bare `addr`. |
| `to` | yes | String, or array of strings. |
| `subject` | no | |
| `html` | one of | At least one of `html` or `text`. |
| `text` | one of | Send both when you can — HTML-only scores worse with spam filters. |
| `cc`, `bcc` | no | String or array. Counts toward the 150-recipient limit. |
| `reply_to` | no | |
| `headers` | no | Custom headers. `From`, `To`, `Message-ID`, `Date` are ignored — changing them would break DMARC alignment or bounce attribution. |

Unknown fields are **rejected**, not ignored. A typo'd `htlm` is an error rather
than a silently unsent body.

---

## Don't send twice

```
Idempotency-Key: order-42-receipt
```

Replaying the same key with the same body returns the original `id` and creates
nothing. Reusing it with a *different* body is an error — otherwise you would
believe a message was sent that never existed.

Use something derived from the thing you are emailing about (`order-42-receipt`),
not a random value. A random key retried after a timeout creates a second email.

---

## Examples

### curl

```bash
curl -X POST "$EMAILD_URL/v1/emails" \
  -H "Authorization: Bearer $EMAILD_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: welcome-user-1234" \
  -d '{
    "from": "Acme <noreply@example.com>",
    "to": "customer@example.net",
    "subject": "Welcome",
    "html": "<p>Welcome aboard.</p>",
    "text": "Welcome aboard."
  }'
```

### Python

```python
import os
import httpx

EMAILD_URL = os.environ["EMAILD_URL"]
EMAILD_API_KEY = os.environ["EMAILD_API_KEY"]


def send_email(to, subject, html, text=None, idempotency_key=None):
    headers = {"Authorization": f"Bearer {EMAILD_API_KEY}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    response = httpx.post(
        f"{EMAILD_URL}/v1/emails",
        headers=headers,
        json={
            "from": "Acme <noreply@example.com>",
            "to": to,
            "subject": subject,
            "html": html,
            "text": text,
        },
        timeout=10,
    )
    if response.status_code >= 400:
        # The error body always has this shape.
        error = response.json()["error"]
        raise RuntimeError(f"{error['type']}: {error['message']}")
    return response.json()["id"]


send_email(
    "customer@example.net",
    "Reset your password",
    "<p>Click to reset.</p>",
    idempotency_key="pwreset-user-1234-20260807",
)
```

### JavaScript / TypeScript

```javascript
const EMAILD_URL = process.env.EMAILD_URL;
const EMAILD_API_KEY = process.env.EMAILD_API_KEY;

export async function sendEmail({ to, subject, html, text, idempotencyKey }) {
  const headers = {
    Authorization: `Bearer ${EMAILD_API_KEY}`,
    "Content-Type": "application/json",
  };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

  const response = await fetch(`${EMAILD_URL}/v1/emails`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      from: "Acme <noreply@example.com>",
      to,
      subject,
      html,
      text,
    }),
  });

  const body = await response.json();
  if (!response.ok) {
    throw new Error(`${body.error.type}: ${body.error.message}`);
  }
  return body.id;
}
```

---

## What the statuses mean

This is where emaild deliberately differs from most email APIs.

| Status | Meaning |
|---|---|
| `queued` | Durably stored. A worker will pick it up. **Not sent yet.** |
| `sending` | A worker is delivering it right now. |
| `accepted_by_provider` | The provider took custody and answered `250`. **Terminal.** |
| `temporarily_failed` | Failed for a retryable reason; will be retried with backoff. |
| `permanently_rejected` | Will not be retried. |

**`accepted_by_provider` does not mean delivered.** Bad external recipients are
accepted by the provider and bounce out of band, so this is the honest limit of
what can be proven. Nothing here will ever claim delivery it cannot demonstrate.

---

## Check on a message

```bash
curl -H "Authorization: Bearer $EMAILD_API_KEY" \
  "$EMAILD_URL/v1/emails/email_01KZDD5PWYYJNQPPWHN9YYYB96"
```

Returns the message plus its full timeline:

```
api.accepted        recipients=1 size_bytes=2283
message.queued      message_id_header=<email_01KZ...@example.com>
delivery.attempt    attempt=1 host=chocobo.mxrouting.net
provider.accepted   code=250 response="OK id=1wsDhE-00000008tKT-28PB"
```

The body is never returned — it is purged shortly after delivery, so password
reset links do not become a permanent archive.

---

## Errors

Every error has the same shape:

```json
{ "error": { "type": "authorization_error", "message": "...", "param": "from" } }
```

| Status | `type` | What to do |
|---|---|---|
| 401 | `authentication_error` | Key missing, malformed, or revoked. |
| 403 | `authorization_error` | Key may not send as that `from`. The message lists what it *can* use. |
| 422 | `validation_error` | Bad field. `param` names it. |
| 422 | `domain_not_ready` | The domain's DNS is incomplete. Publish records, don't widen the key. |
| 422 | `suppressed_recipient` | A recipient is on the suppression list. |
| 422 | `idempotency_key_reused` | Same key, different body. Use a new key. |
| 409 | `idempotency_conflict` | An identical request is in flight. Retry shortly. |
| 429 | `rate_limit_exceeded` | Back off. |

---

## Limits

| Limit | Value |
|---|---|
| Recipients per message | 150 (`to` + `cc` + `bcc`) |
| Message size | 50 MB |
| **Sends per hour, per sender identity** | **400** |

That hourly limit is the one to design around. The provider **rejects**
over-limit sends permanently rather than queueing them, so emaild gates ahead of
the ceiling and holds messages back instead of letting them fail. If you expect
to exceed it, say so before you build against it.

---

## Check your key works

```bash
curl -H "Authorization: Bearer $EMAILD_API_KEY" "$EMAILD_URL/v1/me"
```

```json
{
  "project": "billing",
  "key_name": "billing-prod",
  "allowed_senders": ["noreply@example.com"],
  "allowed_domains": ["example.com"]
}
```

## Watch your own volume

```bash
curl -H "Authorization: Bearer $EMAILD_API_KEY" "$EMAILD_URL/v1/metrics"
```

Scoped to your project: totals, failure rate, queue health, provider latency,
and how much of the hourly budget you have left.

---

## Two rules worth following

1. **Send `text` alongside `html`.** It costs one line and measurably improves
   inbox placement.
2. **Use a deterministic `Idempotency-Key`.** Derive it from the event, not from
   a random generator, so a retry after a network timeout does not become a
   second email to your customer.
