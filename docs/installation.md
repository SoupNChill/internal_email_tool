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

## Getting the files onto the host

The host needs the whole of `deploy/` — **three files**, and the installer
refuses to start without all of them:

| File | Why it is needed |
|---|---|
| `install.sh` | The installer itself |
| `compose.yaml` | Defines the services; copied into the installation |
| `appctl` | Every operator command from here on — `doctor`, `backup`, `admin` |

Copy the **directory**, not the files you happen to click on. From a machine
with a checkout:

```bash
tar czf emaild-deploy.tar.gz deploy/
scp emaild-deploy.tar.gz you@host:~
```

Then on the host:

```bash
tar xzf emaild-deploy.tar.gz && cd deploy
```

The repository is private, so `git clone` on the target host would need an SSH
key deployed there. Copying the tarball avoids putting repository credentials
on a mail server that does not need them.

## Install

```bash
sudo bash install.sh --version 0.9.0-rc.1 --lan --port 8000
```

### Why `bash install.sh` and not `./install.sh`

Because it works in every case. Graphical FTP clients — FileZilla, WinSCP,
Cyberduck — do **not** preserve the Unix executable bit, so a transferred
`install.sh` arrives unexecutable and `./install.sh` fails with:

```
-bash: ./install.sh: Permission denied
```

Reaching for `sudo` then produces a genuinely misleading error:

```
sudo: ./install.sh: command not found
```

The file is right there. `sudo` reports "command not found" for a file it
cannot execute, which sends you looking for a missing program instead of a
missing permission bit. Invoking `bash` explicitly sidesteps the bit entirely.
(`chmod +x install.sh` also works, if you prefer.)

### Why `sudo`

The default `--dir` is `/opt/emaild`, which needs root to create, and talking
to the Docker daemon needs root unless your user is in the `docker` group.
Running the installer under `sudo` covers both.

One consequence: the installation directory is then owned by root, so operator
commands are `sudo ./appctl …` rather than `./appctl …`. The rest of this
documentation writes `./appctl` for brevity — add `sudo` if you installed this
way.

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
