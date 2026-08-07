# Persistent Data Inventory

Required by `first_production_packaging.md` §5.

**Date:** 2026-08-07 · **Commit:** `70f3a52`

## 1. The short version

Nothing in this application writes to disk outside Postgres. Verified by grep for
`open(`, `mkdir`, `shutil`, and path writes across `emaild/` — the only file
access is Jinja2 loading templates from the image.

So the entire backup surface is **two items**:

1. The Postgres database.
2. The mailbox encryption key, which lives in `.env` and deliberately *not* in
   the database.

That is a much smaller problem than most applications have, and it is worth
saying plainly because it makes the backup design short rather than clever.

## 2. Inventory

| Data | Current location | Production location | Backup method | Restore method | Regenerable? |
|---|---|---|---|---|---|
| Message history + events | Postgres `messages`, `events` | same | `pg_dump` | `pg_restore` | **No** |
| Suppression list | Postgres `suppressions` | same | `pg_dump` | `pg_restore` | **No** |
| Mailbox encryption key | `.env` | operator secret store | manual, separate | supplied at restore | **No** |
| API key hashes | Postgres `api_keys` | same | `pg_dump` | `pg_restore` | No — but reissuable |
| Mailbox SMTP credentials | Postgres, Fernet-encrypted | same | `pg_dump` | `pg_restore` + key | Yes, via provider reset |
| Installation identity | Postgres `installation` | same | `pg_dump` | `pg_restore` | No — **and not yet written (F-01)** |
| Domain + DNS state | Postgres `domains` | same | `pg_dump` | `pg_restore` | **Yes** — from MXRoute API + DNS |
| Projects, mailbox records | Postgres | same | `pg_dump` | `pg_restore` | Partly — provider is authoritative |
| Idempotency keys | Postgres | same | `pg_dump` | `pg_restore` | Yes — 24h TTL, expendable |
| Worker heartbeats | Postgres | same | `pg_dump` | `pg_restore` | Yes — regenerates in seconds |
| Message bodies | Postgres, transient | same | `pg_dump` | `pg_restore` | N/A — purged by design |
| Configuration | `.env` | `.env` on host, mode 600 | manual, separate | manual | No |
| Logs | stdout → Docker | stdout → collector | not backed up | n/a | n/a |

### Truly irreplaceable

Only three rows above have **no** reconstruction path:

- **Event history.** The audit trail *is* the product. Nothing can rebuild it.
- **Suppression list.** Accumulated knowledge. Rebuilding means re-learning every
  entry the expensive way — by mailing dead addresses again on shared IPs.
- **The encryption key.** See §4; its loss is survivable but expensive.

Everything else is either regenerable from MXRoute and DNS, or reissuable.

## 3. Path, ownership, and permission requirements

| Path | Owner | Mode | Service | Notes |
|---|---|---|---|---|
| `postgres_data` volume | postgres (uid 999) | container-managed | postgres | Named volume. **Not a backup** (§14). |
| `.env` | operator | **600** | read by compose | Contains two secrets. |
| `/app` in image | `emaild` (10001) | read-only in practice | api, worker | Nothing writes here. |

Growth: dominated by `messages` and `events`. At a few hundred messages a day,
the order of magnitude is tens of MB per year — bodies are purged after 72 hours,
so the durable rows are metadata only. Concurrent access is normal (API + worker
+ admin CLI), which is why the backup must be a *logical* dump rather than a file
copy of the volume (§11).

The application currently has **no** required writable host path. If that ever
changes, §7 requires it to fail clearly when unwritable.

## 4. The decision that shapes the backup design

**Does the backup contain the mailbox encryption key?**

The tension is real and §17 does not resolve it. §17 says a backup should include
"required encryption keys". But mailbox passwords are stored Fernet-encrypted in
the database, so a backup containing both the ciphertext and its key is a backup
whose compromise is equivalent to plaintext — the encryption stops protecting
anything the moment they travel together.

| Option | Restore | Risk |
|---|---|---|
| **A.** Key inside the backup | Single artifact, works unattended | The backup is as sensitive as the plaintext credentials. One leaked file is total. |
| **B.** Key stored separately | Operator must supply it | Restore fails if the key was never backed up — and that failure surfaces at the worst moment. |

**Recommendation: B, with the failure mode engineered out.** Store the key
separately, and record a **fingerprint** of it in the backup manifest — a SHA-256
of the key, never the key itself. Restore then verifies up front that the
supplied key is the right one, and fails immediately with a clear message rather
than succeeding into a database whose credentials cannot be decrypted.

Two things make B tolerable that would not otherwise:

- Loss of the key is **survivable**. MXRoute lets us `PATCH` mailbox passwords,
  so recovery is re-provisioning every sender identity — tedious, not
  catastrophic. This was demonstrated during Phase 2, when the key was rotated
  and every mailbox re-encrypted.
- Nothing else in the database is encrypted with it. Message history, events, and
  the suppression list — the genuinely irreplaceable data — restore fine without
  the key.

So under B, a lost key costs an afternoon of re-provisioning. Under A, a leaked
backup costs every SMTP credential at once.

**This needs your confirmation before the backup work order starts.**

## 5. What §5 forbids, checked

> No irreplaceable data may remain only inside a writable container layer, a
> temporary directory, a source-code checkout, a developer home directory, or an
> undocumented host path.

All five: **satisfied.** Every durable byte is in the `postgres_data` named
volume, except the encryption key, which is in `.env` on the host and documented
here.
