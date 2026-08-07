# Installation

Installing emaild on a Linux host. The destination needs **Docker and nothing
else** — no Python, no compilers, no copy of this repository.

## Requirements

| | |
|---|---|
| OS | Linux (x86_64 or aarch64) |
| Docker Engine | any version with Compose v2 (`docker compose`, not `docker-compose`) |
| Disk | 2 GB free minimum |
| Ports | one free TCP port for the API (default 8000) |
| Network | outbound HTTPS (registry, MXRoute API) and outbound TCP 465 (SMTP) |

Check the last one before you start — some hosting providers block outbound
SMTP, and emaild cannot deliver anything if 465 is closed:

```bash
timeout 5 bash -c 'cat < /dev/null > /dev/tcp/chocobo.mxrouting.net/465' \
  && echo "465 reachable" || echo "465 BLOCKED — emaild cannot deliver from here"
```

## Install

Copy `deploy/` to the target host, then:

```bash
./install.sh --version 0.9.0-rc.1 --lan --port 8000
```

| Flag | Meaning |
|---|---|
| `--version` | **Required.** Production must pin an exact version, never a moving tag. |
| `--lan` | Bind to `0.0.0.0` so other machines can reach it. Omit for localhost-only. |
| `--port` | Host port. Checked for availability before anything is created. |
| `--dir` | Installation directory. Default `/opt/emaild`. |

The installer checks the host, refuses to proceed if a conflicting database
volume exists, generates all secrets, pulls the pinned image, runs migrations,
starts the services, and waits for health. It stops before doing damage rather
than part-way through.

### What `--lan` costs you

The API is reachable from your whole network over plain HTTP, so
`Authorization: Bearer em_live_…` travels in **cleartext** and is readable by
anything else on that network. On a trusted home LAN that is normally accepted.
The installer generates a dashboard password because of it.

For HTTPS, or reachability beyond the LAN, see
[operations.md](operations.md#exposing-emaild-beyond-the-lan).

## After installing

The installer prints what to do next. In order:

**1. Add the MXRoute credentials.** They are needed only to provision domains
and mailboxes, never to send.

```bash
cd /opt/emaild
./appctl stop
nano .env          # fill in EMAILD_MXROUTE_SERVER / USERNAME / API_KEY
./appctl start
```

**2. Add a sending domain.**

```bash
./appctl admin domains token
```

Publish the TXT record it prints at your DNS provider, wait for propagation
(5–15 minutes), then:

```bash
./appctl admin domains add example.com
./appctl admin domains records example.com
```

Publish every record it prints. Paste values **without** surrounding quotes —
most registrars add their own, and a double-quoted DKIM key resolves fine while
failing verification silently.

**3. Verify.**

```bash
./appctl admin domains verify example.com
```

The domain must reach `ready`. MX, SPF, and DKIM must all pass; DMARC is
reported but does not block.

**4. Create a sender identity.**

```bash
./appctl admin mailboxes provision noreply@example.com
```

**5. Create a project and a key.**

```bash
./appctl admin projects create billing
./appctl admin keys create billing-prod \
    --project billing --mailbox noreply@example.com
```

The key is shown **once**. Only a SHA-256 hash is stored, so it cannot be
recovered — copy it now.

**6. Back up, today.** See [backup-and-restore.md](backup-and-restore.md). Two
things, in two places: the archive and the encryption key.

## Verify the installation

```bash
./appctl doctor
```

Exit 0 means everything checks out. Then send a test message using
[integration.md](integration.md).

## Upgrading

```bash
cd /opt/emaild
./appctl backup                   # never upgrade without one
nano .env                         # change EMAILD_VERSION
./appctl stop && ./appctl start
./appctl doctor
```

Migrations run automatically as a separate step before the application starts.
If one fails, the application does not start — restore the pre-upgrade backup.

Re-running `install.sh` over an existing installation is **refused**: it would
overwrite the encryption key and make every stored credential undecryptable.

## Uninstalling

Preserving data:

```bash
cd /opt/emaild && ./appctl backup --to ~/emaild-final-backup
docker compose down          # containers go, the volume stays
```

Destroying data — irreversible, and message history and the suppression list
cannot be rebuilt from anywhere:

```bash
docker compose down -v
rm -rf /opt/emaild
```
