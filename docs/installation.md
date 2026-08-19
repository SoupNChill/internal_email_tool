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

```bash
sudo mkdir -p /opt/emaild && sudo chown $USER /opt/emaild && cd /opt/emaild
curl -fsSL https://raw.githubusercontent.com/SoupNChill/internal_email_tool/main/deploy/compose.yaml -o compose.yaml
docker compose up -d
```

That is the entire installation. There is no `.env` to write first and no
installer to run.

If `docker compose` says *permission denied* or *cannot connect to the Docker
daemon*, add yourself to the `docker` group once and log back in:

```bash
sudo usermod -aG docker $USER
newgrp docker
```

### Why there is nothing to configure

Everything has a working default, and the two secrets generate themselves the
first time the containers start:

| | Where it comes from |
|---|---|
| Version | Pinned in `compose.yaml`. Change that line to upgrade. |
| Database password | A default. Postgres publishes no port, so it is unreachable from outside the compose network. |
| Mailbox encryption key | Generated on first boot into the `worker_secrets` volume. |
| Dashboard password | Generated on first boot into the `api_secrets` volume. |

This used to be an `install.sh` whose only job was generating those two
secrets. Removing the requirement removed the installer.

**Save the encryption key today.** It is the one thing that cannot be
regenerated — a database backup restored without it leaves every stored mailbox
password unreadable:

```bash
./appctl key
```

### Get `appctl` too

`compose.yaml` runs the service; `appctl` operates it — `doctor`, `backup`,
`key`, `admin`. It is not required to start, but you want it before anything
goes wrong:

```bash
curl -fsSL https://raw.githubusercontent.com/SoupNChill/internal_email_tool/main/deploy/appctl -o appctl
chmod +x appctl
./appctl doctor
```

The `chmod` matters if you transferred the file with a graphical FTP client
(FileZilla, WinSCP, Cyberduck) — none of them preserve the Unix executable bit,
and the resulting error is misleading: `./appctl` says *Permission denied*,
while `sudo ./appctl` says *command not found* for a file that is plainly
there. `bash appctl …` works regardless of the bit.

### Reachable from the LAN by default

The API binds `0.0.0.0`, because a transactional email API that only its own
host can call has no callers. On a trusted home LAN this is the intended shape.

It costs you cleartext: `Authorization: Bearer em_live_…` is readable by
anything else on that network. Two ways to change that — set
`EMAILD_BIND=127.0.0.1` in a `.env` beside `compose.yaml` if the products
calling it run on this same machine, or run the `public` profile for HTTPS.

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
nano compose.yaml                 # change the version on the x-image line
docker compose pull
docker compose up -d
./appctl doctor
```

Migrations run automatically as a separate step before the application starts.
If one fails, the application does not start — restore the pre-upgrade backup.

Upgrading never touches the secrets: they live in their own volumes, which
`docker compose up -d` leaves alone. `docker compose down -v` would destroy
them along with the database, which is why that flag appears nowhere in this
documentation except as a warning.

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
