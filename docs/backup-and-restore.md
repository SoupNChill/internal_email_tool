# Backup and Restore

The most important document here. Read the first section even if you skip the
rest.

## Two things, in two places

A backup archive is **not sufficient on its own**. You need:

| | What | Where it lives | Backed up by |
|---|---|---|---|
| **a** | The database archive | `backups/emaild-*.tar.gz` | `./appctl backup` |
| **b** | The mailbox encryption key | `EMAILD_MAILBOX_ENCRYPTION_KEY` in `.env` | **you, manually** |

**The archive deliberately does not contain the key.** A backup carrying both
the ciphertext and the key that decrypts it is a backup whose compromise equals
plaintext — the encryption stops protecting anything the moment they travel
together.

So: archives to your backup destination, the key to a password manager. Not the
same place.

The archive records a **SHA-256 fingerprint** of the key, never the key. Restore
checks it and refuses immediately if you supply the wrong one, instead of
succeeding into a database whose credentials cannot be decrypted.

## What is actually irreplaceable

| Data | Recoverable? |
|---|---|
| Message history and events | **No.** The audit trail is the product. |
| Suppression list | **No.** Rebuilding means re-learning by mailing dead addresses again. |
| Encryption key | Painfully — re-provision every sender identity. |
| API key hashes | Not recoverable, but reissuable. |
| Mailbox SMTP passwords | Yes — reset through MXRoute. |
| Domain and DNS state | Yes — rebuilt from the MXRoute API and DNS. |

Losing the key costs an afternoon. Losing the archive loses history permanently.

## Taking a backup

```bash
cd /opt/emaild
./appctl backup
./appctl backup --to /mnt/nas/emaild
```

Produces a single `.tar.gz`, mode 600, containing:

- `database.dump` — `pg_dump -Fc`, a logical and consistent dump. A copy of the
  live volume would not do: three processes write to the database concurrently.
- `manifest.json` — format version, app version, schema revision, installation
  id, Postgres version, timestamp, encryption-key fingerprint
- `checksums.sha256`

**Copy it off this machine.** A backup on the same disk as the database
protects against exactly one failure mode, and not the common one.

### Scheduling it

```
0 3 * * *  cd /opt/emaild && ./appctl backup --to /mnt/nas/emaild >> /var/log/emaild-backup.log 2>&1
```

`appctl backup` exits non-zero on failure, so cron will report it.

## Restoring

```bash
./appctl restore backups/emaild-20260807T161316Z.tar.gz
```

Restore validates in this order, and stops at the first problem:

1. archive format version
2. checksums
3. **encryption key fingerprint** — refuses on mismatch
4. refuses to overwrite an active installation

That fourth check names how many messages would be destroyed:

```
error: refusing to overwrite an active installation.
       current installation : inst_418c0c5b78cd72caa52110d6
       messages that would be destroyed : 10
```

Pass `--force` only when you genuinely mean to discard them.

The installation identity travels with the archive — a restore keeps the
original id rather than minting a new one, so a restored installation is
identifiable as the same one.

## Test your backup

An untested backup is unproven. This has been verified for emaild, and you
should verify it for your own installation at least once:

```bash
./appctl backup
docker compose down -v          # destroys the database volume
docker compose up -d            # clean installation
./appctl restore backups/<latest>.tar.gz --force
./appctl doctor
```

Then confirm the counts match what you had, and send a test message — that last
step proves the mailbox credentials still decrypt, which counting rows does not.

## Recovering from specific failures

### A migration failed during an upgrade

The application does not start when migrations fail, so nothing is running
against a half-migrated schema.

```bash
nano .env                     # set EMAILD_VERSION back
./appctl restore backups/<pre-upgrade>.tar.gz --force
```

Schema **downgrade is not supported** — it works against empty databases but has
never been tested against representative data, and an untested downgrade is not
a recovery path. Restore-from-backup is.

### The encryption key is lost

Message history, events, and suppressions are unaffected — they are not
encrypted with it. Only mailbox SMTP passwords are.

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# put it in .env, then for each sender identity:
./appctl admin mailboxes rotate noreply@example.com
```

`rotate` sets a new password at MXRoute and encrypts it under the new key. It
does not need the old key.

### The whole host is gone

On a new host:

```bash
sudo mkdir -p /opt/emaild && sudo chown $USER /opt/emaild && cd /opt/emaild
curl -fsSL https://raw.githubusercontent.com/SoupNChill/internal_email_tool/main/deploy/compose.yaml -o compose.yaml

# Put the ORIGINAL key back BEFORE the first start. A fresh install generates a
# new one on first boot, and the archive's ciphertext was written under the old
# one -- start first and you restore data you cannot read.
printf 'EMAILD_MAILBOX_ENCRYPTION_KEY=%s\n' "<key from your password manager>" > .env
chmod 600 .env

docker compose up -d
./appctl restore /path/to/archive.tar.gz --force
./appctl doctor
```

An explicit key in `.env` always beats the generated one (`emaild/config.py`),
so the volume never gets a key of its own and nothing has to be undone.

Install the **same version** the archive was taken from — the manifest records
it. A newer version may have migrations the archive's schema has not seen.

## What a backup does not cover

- `.env` itself — back it up separately; it holds two secrets
- Docker, the OS, and anything else on the host
- MXRoute-side state — mailboxes and domains live there and are rebuilt from the
  provider, not from the archive
