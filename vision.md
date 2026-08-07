# Vision

## Email infrastructure should disappear

Every new product begins with momentum.

A person has an idea.  
They open their editor.  
They build the first screen.  
They create the first account.  
And then, almost immediately, they hit the same wall:

Email.

SMTP servers. Credentials. Ports. TLS settings. DNS records. SPF. DKIM. DMARC. Password rotation. Delivery failures. Logs scattered across systems that do not speak to one another.

None of this is the product.

None of it creates value for the customer.

And yet, every application needs it.

We believe transactional email should feel like electricity.

You connect it once.  
You trust it.  
And from that moment forward, it is simply there.

That is what we are building.

---

## One simple promise

Our promise is simple:

> Any application should be able to send secure, trustworthy transactional email through a clean API in minutes.

No SMTP credentials inside the application.

No repeated domain configuration.

No wondering whether a message disappeared inside the app, inside the email service, or at the provider.

The application makes one authenticated request.

We take responsibility from there.

---

## Built for builders

This is not an email marketing platform.

It is not a campaign designer.

It is not a template marketplace.

It is not an automation engine.

It does not try to become the product.

It exists for the moments every product must handle:

- Verify an email address
- Reset a password
- Confirm an account
- Send a receipt
- Deliver a notification
- Alert a customer that something important happened

These messages are small, but they carry enormous trust.

When a verification email does not arrive, the user does not blame the mail server.

They blame the product.

Our job is to protect that trust.

---

## The experience

A builder creates a project.

They connect a domain once.

The system verifies that the domain is ready to send.

They create an API key with permission to use that domain.

Then they send an email with a request that is obvious, readable, and difficult to misuse.

```http
POST /v1/emails
Authorization: Bearer em_live_xxxxx
```

```json
{
  "from": "Acme <noreply@example.com>",
  "to": ["customer@example.net"],
  "subject": "Verify your email",
  "html": "<p>...</p>",
  "text": "..."
}
```

The response is immediate:

```json
{
  "id": "email_01J...",
  "status": "queued"
}
```

From that moment, the builder can see exactly what happened.

The request was accepted.

The message was queued.

A worker attempted delivery.

MXRoute accepted it, deferred it, or rejected it.

There is no mystery.

---

## Clarity over theater

Email systems often create the illusion of certainty.

A dashboard says “delivered” when a downstream server merely accepted the message.

A green checkmark hides the distance between submission and the recipient’s inbox.

We will not do that.

We will describe reality precisely.

A message may be:

- Accepted by the API
- Queued
- Sending
- Accepted by the provider
- Temporarily failed
- Permanently rejected
- Unknown after provider acceptance

We will never call a message delivered unless we have evidence that it was delivered.

Trust begins with honest language.

---

## One control plane for every product

The real power of this system is not sending one email.

It is creating one dependable email foundation for every application we build.

Every SaaS product should not have to rediscover:

- How to authenticate with an SMTP server
- Where credentials should live
- How retries should work
- How domains are authorized
- How failures are classified
- How sending volume is measured
- How an incident is investigated

Those decisions should be made once.

They should be made well.

And every future product should inherit them automatically.

The email API becomes a shared capability across the entire portfolio.

A new application does not integrate with MXRoute.

It integrates with us.

---

## Security without friction

Convenience must never require weaker security.

Applications will never receive raw SMTP credentials.

They will receive scoped API keys.

A key can be restricted to a project, a domain, or a sender identity.

A compromised key must not become permission to impersonate every domain in the system.

Every request must answer four questions:

1. Who is making this request?
2. Which domain are they allowed to use?
3. Which sender identities are permitted?
4. Is the requested message within the limits of that authorization?

Keys will be stored using one-way hashes.

They can be named, audited, revoked, and replaced.

The system will record when they were created and when they were last used.

Security should be strong enough for production and simple enough that builders never feel tempted to bypass it.

---

## Reliability is the product

The API is only the front door.

The real product is everything that happens after the request arrives.

A message must be durably recorded before the API claims success.

Delivery must happen asynchronously.

Temporary failures must be retried with controlled backoff.

Permanent failures must stop.

Duplicate requests must not create duplicate emails.

Provider limits must be respected.

A worker crash must not lose a message.

A deployment must not interrupt the queue.

Every message must have a stable identity and an append-only history.

The system must be designed around the assumption that networks fail, providers defer, processes restart, and humans make mistakes.

Reliability is not an enhancement.

It is the reason this system exists.

---

## Observability that answers the real question

When email fails, a builder needs one answer:

> Where did it fail?

The event timeline should make that answer obvious.

```text
16:02:11 API request accepted
16:02:11 Message queued
16:02:12 Worker started
16:02:13 Connected to MXRoute
16:02:14 Provider accepted message: 250 OK
```

Or:

```text
16:02:13 Authentication failed
535 Invalid credentials
```

Or:

```text
16:02:14 Recipient rejected
550 Mailbox does not exist
```

The logs should be light, structured, searchable, and useful.

They should expose enough detail to troubleshoot the system without turning sensitive email content into permanent application data.

By default, we store metadata and delivery events—not full message bodies.

Verification links, password-reset tokens, customer data, and private correspondence should not become an accidental archive.

---

## Metrics that create confidence

The dashboard should not overwhelm.

It should answer the questions that matter:

- How many messages were requested?
- How many were accepted by MXRoute?
- How many failed?
- Which domains are sending?
- Which API keys are active?
- Is the queue healthy?
- Are retries increasing?
- Is one application creating unusual volume?
- Are provider response times changing?

Volume by domain.

Volume by project.

Failure rate over time.

Queue age.

Provider-acceptance latency.

The goal is not analytics for their own sake.

The goal is confidence.

A builder should be able to look at the system and know that email is healthy.

---

## Domains are first-class infrastructure

A domain is not a string in a request.

It is an identity.

Each domain must have a clear lifecycle:

- Added
- Ownership pending
- DNS incomplete
- Verified
- Ready to send
- Suspended
- Misconfigured

The system should show the exact records required for ownership, SPF, DKIM, and DMARC.

It should verify those records periodically.

It should make obvious when something changes outside the application.

And it should refuse to send from domains that are not authorized.

The foundation must support multiple domains from the beginning, even if the first version uses only one.

Because every new product should be able to inherit the same clean experience.

---

## MXRoute is the engine, not the interface

MXRoute provides the delivery infrastructure.

We provide the product experience.

Our applications should not know which SMTP host is used.

They should not know the port.

They should not know the credentials.

They should not know whether the provider changes in the future.

That boundary is strategic.

Today, the system may route every message through MXRoute.

Tomorrow, we may need multiple MXRoute accounts, regional routing, provider failover, or a different delivery provider altogether.

The API must remain stable even when the infrastructure behind it evolves.

The provider is an implementation detail.

Our contract with applications is not.

---

## The foundation

Starting with the end in sight means the first version must establish the right boundaries.

The foundation must include:

### A stable public API

The API should be versioned, predictable, and intentionally small.

It should support idempotency from the beginning.

Its response language should match the actual delivery lifecycle.

### Durable message storage

Every accepted message must have a stable identifier and a durable record before work begins.

### An asynchronous delivery system

API requests and SMTP delivery must be separated.

The system should be able to scale workers without changing the API.

### An append-only event model

Every meaningful transition should create an event.

Events become the basis of logs, audits, metrics, debugging, and future webhooks.

### Provider abstraction

MXRoute-specific behavior should live behind a delivery adapter.

Applications and core domain logic should not depend directly on SMTP implementation details.

### Domain-scoped authorization

Every message must be checked against project, key, domain, and sender permissions.

### Structured failure classification

Errors should be normalized into categories such as authentication, connection, provider deferral, recipient rejection, policy rejection, and internal failure.

### Privacy by default

Message bodies should not be retained indefinitely.

Secrets must never appear in logs.

Sensitive headers and provider responses must be sanitized.

### Operational limits

The system must enforce configurable limits for rate, concurrency, recipients, message size, and daily volume.

### A path to bounce intelligence

Even if full bounce processing is not part of the first release, message identifiers and return-path design should make it possible later.

These are not premature abstractions.

They are the minimum structure required to prevent every future feature from becoming a rewrite.

---

## What we will not become

Focus is a feature.

We will not build a visual email editor.

We will not build marketing campaigns.

We will not build contact lists.

We will not build drip sequences.

We will not build scheduled sends.

We will not build a second copy of the application’s business logic.

We will not promise inbox placement we cannot prove.

We will not collect sensitive content simply because storage is cheap.

We will not turn a simple transactional email service into a sprawling communications platform.

The product should remain small enough to understand and strong enough to trust.

---

## The first perfect mile

The first milestone is not a large feature list.

It is one complete, dependable journey:

1. A builder adds a domain.
2. The system verifies that the domain is authorized and ready.
3. The builder creates a scoped API key.
4. An application submits a transactional email.
5. The API durably accepts it and returns a stable message ID.
6. A worker delivers it through MXRoute.
7. Temporary failures are retried safely.
8. Permanent failures are classified correctly.
9. The dashboard shows an honest event timeline.
10. Metrics reflect what actually happened.

When that journey feels effortless, the foundation is right.

Everything else can follow.

---

## The future

Over time, this system can become more capable without becoming more complicated.

It may add:

- Bounce mailbox processing
- Suppression lists
- Delivery webhooks
- Additional provider adapters
- Multi-account routing
- Usage quotas
- Cost allocation by project
- Alerting for abnormal failure rates
- Command-line tools and SDKs
- Test-mode delivery and local development support

But every future capability must preserve the original promise:

The builder sends one clear API request.

The system handles the infrastructure.

The truth remains visible.

---

## The standard

The standard is not that email usually works.

The standard is that builders stop thinking about it.

They should not wonder whether the SMTP password expired.

They should not search deployment logs for a missing message.

They should not rebuild retry logic in every product.

They should not expose infrastructure credentials to application code.

They should not need to understand the machinery in order to trust the outcome.

The best infrastructure does not ask to be admired.

It creates freedom.

Freedom to build the product.

Freedom to launch the idea.

Freedom to move quickly without creating invisible risk.

That is the vision.

One secure API.

One honest timeline.

One dependable foundation for every product we build.

Email infrastructure that simply disappears.
