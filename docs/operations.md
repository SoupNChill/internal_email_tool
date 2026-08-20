# Operations

Day-to-day running. Every command here has been executed against a live
installation.

All commands run from the installation directory.

Provisioning goes through `./appctl admin …`, which starts a one-off container
in the admin role. It cannot run inside the `api` or `worker` containers: those
deliberately do not hold the MXRoute account-root credential, and attempting it
is refused at startup. That refusal is the point — the credential exists in one
place, briefly, and nowhere that listens on a port.

## Is it healthy?

```bash
./appctl status          # containers + the full health overview
./appctl doctor          # everything: host, config, services, health
./appctl health          # just the ready check; exit 1 when unhealthy
```

`doctor` and `health` return meaningful exit codes, so they work from cron:

```
0   fine
1   something needs attention
```

**Queue age is the signal worth watching.** A heartbeat only proves the worker
loop is turning; queue age proves work is *moving*, and it catches a dead
worker, a stuck rate gate, a provider outage, and an exhausted send budget with
one number.

## Starting and stopping

```bash
./appctl start
./appctl stop            # workers finish in-flight sends first
./appctl restart
```

Stopping is graceful: the worker stops claiming, lets in-flight deliveries
finish, and exits. Anything still claimed if it is killed harder is recovered by
the reaper within 15 minutes — never lost.

## Logs

```bash
./appctl logs                       # last 100 lines, all services
./appctl logs -f worker             # follow the worker
./appctl logs --since 1h api
```

Logs are JSON on stdout with hard secret redaction — API keys, passwords,
encryption keys, and DSN credentials never appear.

## Domains

```bash
./appctl admin domains list
./appctl admin domains verify
./appctl admin domains records example.com
```

`domains verify` with no argument re-checks every domain. Run it after changing
DNS. Only `ready` domains may send.

A domain that was `ready` and drops to `misconfigured` means something changed
outside emaild — check the records it names.

## Sender identities

```bash
./appctl admin mailboxes list
./appctl admin mailboxes usage noreply@example.com
./appctl admin mailboxes rotate noreply@example.com
```

`rotate` changes the SMTP password at MXRoute first, then re-encrypts locally —
so a failure leaves the old password working at both ends.

**One mailbox per sender identity.** Provisioning a second on the same domain is
refused unless you pass `--additional-identity`, because minting mailboxes to
multiply the 400/hour budget violates MXRoute's acceptable-use policy and
carries account termination.

## Keys

```bash
./appctl admin keys list
./appctl admin keys create web-app \
    --project billing --mailbox noreply@example.com
./appctl admin keys revoke web-app
```

Revocation takes effect on the **next request** — authentication is never
cached. A key is shown once at creation and is not recoverable.

## Suppressions

```bash
./appctl admin suppressions list
./appctl admin suppressions add dead@example.net --reason "hard bounce"
./appctl admin suppressions remove dead@example.net --yes
```

Adding fails closed; removing fails open, so removal is operator-only and
requires `--yes`. It prints what it is about to undo first.

Addresses are also suppressed automatically when the provider rejects a
recipient as nonexistent.

## The dashboard

`http://<host>:<port>/` — where the day-to-day work happens.

| Page | Answers | Can change |
|---|---|---|
| `/` | Is email healthy? | — |
| `/domains` | Which can send, the exact records to publish, per-record pass/fail | **add, re-check** |
| `/messages` | Search by recipient, subject, or id; open one for its timeline | — |
| `/keys` | What exists, scope, last used | **projects, API keys** |
| `/suppressions` | Who we refuse to mail, and why | **add, remove** |

Log in with any username and the dashboard password:

```bash
./appctl key
```

### How domains work without giving the dashboard the credential

Adding or verifying a domain calls MXRoute with an account-root key that can
delete every mailbox on the account. The API container is never given it.

So the dashboard does not perform those actions — it **queues** them. A row goes
into `provisioning_jobs`, and the `provisioner` service (role=admin, no
listener, no published port) executes it within a few seconds. The result
appears on the domains page.

What makes that meaningfully narrower than just handing over the key is that a
request is not a command. It is a `JobType`, and that enum has two members:
`add_domain` and `verify_domain`. There is no way to express "delete a
mailbox", so a fully compromised API cannot ask for one.

The provisioner is also given less than the admin CLI: it holds the MXRoute
credential but **not** the mailbox encryption key, because domain work never
decrypts a password.

If `EMAILD_MXROUTE_*` is unset, the provisioner idles and says so, leaving
queued jobs pending. Add the credentials, restart, and the work runs.

### Why mailboxes are still CLI-only

Provisioning needs both the MXRoute credential *and* the encryption key, and it
is the one action that can breach MXRoute's acceptable-use policy — minting
extra mailboxes to multiply a sending budget risks account termination. That is
a judgement call, and it belongs to a person at a terminal:

```bash
./appctl admin mailboxes provision noreply@yourdomain.com
```

### Starting a new product, entirely in the browser

1. `/keys` → **New project** → name it after the product
2. **New key** → pick the project, tick the sender identities it may use
3. Copy the key — **it is shown once and is never recoverable**
4. Paste it into the product as `Authorization: Bearer …`

Every change made here is logged with `actor="dashboard"`, so the audit trail
distinguishes it from the CLI.

## Watching a specific message

```bash
curl -H "Authorization: Bearer $KEY" http://localhost:8000/v1/emails/email_01KZ...
```

Or search the dashboard. The timeline shows exactly where it stopped.

## Exposing emaild beyond the LAN

A product deployed to a VPS **cannot reach a LAN-only emaild** — and mail does
not error, it silently stops. To fix that, and to get HTTPS:

1. Create a Cloudflare Tunnel and copy its token.
2. Put it in `.env` as `CLOUDFLARE_TUNNEL_TOKEN`.
3. `docker compose --profile public up -d`

Route `/v1/*` straight through — bearer keys authenticate machines. Put
Cloudflare Access on the dashboard routes **only**: an API client cannot
complete an SSO challenge and would receive an HTML login page where it expects
JSON.

Then set `EMAILD_DASHBOARD_BEHIND_PROXY_AUTH=true`.

## Routine maintenance

The worker handles this automatically every 5 minutes:

- reaps claims from dead workers
- purges message bodies past their retention window

Nothing is scheduled for you to run. The one thing you must do yourself is
[back up](backup-and-restore.md).
