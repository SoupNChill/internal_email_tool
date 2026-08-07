# Distribution Audit

**Phase 9, Work Order 1.** Required by `release_rules/first_production_packaging.md`
§2 before any packaging implementation begins.

**Date:** 2026-08-07 · **Commit:** `70f3a52` · **Version:** `0.1.0`

Every finding below was checked against the repository, not recalled. Commands
used are shown where the answer is not obvious from a file.

---

## 1. The twenty questions from §2

| # | Question | Finding |
|---|---|---|
| 1 | How does it start? | Three entrypoints. `uvicorn emaild.main:app` (Dockerfile CMD), `python -m emaild.worker`, `alembic upgrade head` (compose `migrate` service). Admin CLI is `python -m emaild.admin`. |
| 2 | Service dependencies | Postgres 17.2 only. MXRoute is external (REST + SMTP) and is not required for startup. |
| 3 | Ports | Container exposes **8000** (API). Worker exposes **nothing** — outbound SMTP only. Postgres 5432, published to `127.0.0.1` only. |
| 4 | Configuration loading | `emaild/config.py`, Pydantic Settings, `EMAILD_` prefix. Env file selectable via `EMAILD_ENV_FILE`, resolved at class-definition time. |
| 5 | Where secrets live | `.env` (gitignored, mode 600) and container environment. **None in the image** — verified. |
| 6 | Database location | Named volume `postgres_data`. Never a bind mount, never inside the source tree. |
| 7 | Uploaded files | **None.** The application accepts no uploads. |
| 8 | Generated files | **None.** |
| 9 | Logs | stdout/stderr as JSON, with a redaction filter. No log files. |
| 10 | Survives restart | Everything — all state is in Postgres. |
| 11 | Survives container replacement | Everything, for the same reason. Verified in Phase 1. |
| 12 | Survives server migration | Postgres contents **plus the mailbox encryption key**. See §3. |
| 13 | Migrations exist | Yes. Alembic, 4 revisions, `migrate` runs as a separate unit before the app. |
| 14 | Data inside the source directory | **None.** |
| 15 | Data only inside a container filesystem | **None.** |
| 16 | Assumes `localhost` | **No.** `grep` for `localhost`/`127.0.0.1`/`http://` across `emaild/**.py` and templates returns nothing. Host binding is a compose concern. |
| 17 | Development-only hostnames/paths/ports | None in code. Ports come from `.env`. |
| 18 | Undocumented local files at build | No. Build context is `pyproject.toml`, `README.md`, `emaild/`. |
| 19 | Frontend hard-codes a backend URL | No — the dashboard is server-rendered and uses relative links only. |
| 20 | Clean shutdown | **Worker: yes**, verified (SIGTERM → exit 0, nothing stranded). **API: yes**, FastAPI lifespan disposes the engine. |

## 2. Image audit

```
emaild-api            243 MB
runs as               uid=10001(emaild)   non-root
compilers present     no                  (multi-stage build works)
pytest/ruff/mypy      absent              (dev extras excluded)
pip, setuptools       PRESENT             minor surface, see F-09
/app contents         alembic  alembic.ini  emaild
.env in image         no
```

Meets §6 and §19 on every point except the residual `pip`/`setuptools`.

## 3. The state that actually matters

The unusual and fortunate finding: **nothing writes to disk outside Postgres.** A
`grep` for `open(`, `mkdir`, `shutil`, and path writes across `emaild/` returns
nothing but template loading.

That makes the backup surface exactly two items:

1. The Postgres database.
2. The **mailbox encryption key** — which is *not* in the database, by design.

Everything else (domain state, MX hosts, DNS status) rebuilds from the MXRoute
API and DNS. See `persistent_data_inventory.md`.

---

## Findings

### Required before first production release

**F-01 · The installation ID is never written.**
`emaild/models.py` defines the `installation` table with a single-row constraint,
and §10 requires the identity to exist, persist across upgrade, and appear in the
backup manifest. Nothing in the codebase writes a row —
`grep -rn "Installation" emaild/ --include=*.py` matches only the model
definition. I built the table in Phase 1 with an explicit argument for doing it
early, and then never wired it up. Restore cannot verify which installation a
backup came from until this exists.

**F-02 · No backup implementation.** (§17)
`pg_dump` appears in three documents and zero executable files. The entire backup
story is currently prose.

**F-03 · No restore implementation.** (§18)
More consequential than F-02, because §24 requires restore onto a **clean
machine** and §14 states plainly that an untested backup is unproven. We
currently have neither half.

**F-04 · Production compose builds from source.** (§8)
`docker-compose.yml` uses `build:` for `api`, `worker`, and `migrate`. §8 is
explicit that production must not depend on `build: .` on the destination server,
and must pin an exact image. Needs a separate `deploy/compose.yaml` referencing
`ghcr.io/soupnchill/emaild:X.Y.Z`.

**F-05 · No image publishing.** (§21)
`.github/` does not exist. There is no registry, no digest recording, and no
authorised release workflow.

**F-06 · The version is stated in three places.** (§22)
`pyproject.toml:3`, `emaild/__init__.py:5`, `Dockerfile:43` each carry `0.1.0`
independently. §22 requires one authoritative source. Today they agree; nothing
makes them.

**F-07 · No installation script or documented install path.** (§19, §23)
A clean server cannot currently be brought up from documented artifacts without
cloning the repository — which §25 criterion 2 forbids.

**F-08 · Documentation set does not exist.** (§23)
Only `README.md` and `docs/integration.md` exist. Missing: installation,
configuration reference, operations, backup-and-restore, troubleshooting,
architecture overview.

### Strongly recommended

**F-09 · `pip` and `setuptools` remain in the runtime image.**
Not exploitable on their own, but they are install machinery in a container that
never installs anything. One line in the Dockerfile.

**F-10 · No PR validation.** (§21)
ruff, mypy, and 197 tests all run locally and none run automatically. The most
likely future regression is one nobody runs the suite for.

**F-11 · No `appctl`.** (§16)
The admin CLI covers domains, mailboxes, keys, suppressions, and status. Missing
the operational verbs: `start`, `stop`, `logs`, `backup`, `restore`,
`config-check`, `doctor`.

**F-12 · No support bundle.** (§48)
Diagnostics currently mean reading logs by hand. Lower priority for a
single-operator installation than it would be for a fleet.

### Optional / future

- **F-13 ·** Release channels, canary, and rollout waves (§32–34) are meaningless
  with one installation. They activate on the second.
- **F-14 ·** Multi-architecture images (§20). Only build `arm64` if it will be
  functionally tested there; §20 forbids publishing one merely because it builds.
- **F-15 ·** Upgrade fixtures from prior releases (§26) require prior releases to
  exist.

### Unresolved — needs an operator decision

These are in `production_packaging_plan.md` §6 with recommendations. Summarised:

1. **Where does production actually run?** LAN box, VPS, or stays on this
   machine?
2. **Does the backup contain the encryption key?** This is the consequential one
   — see `persistent_data_inventory.md` §4.
3. **Registry visibility** — GHCR private (matching the repo) or public?
4. **Version at first release** — `1.0.0`, or stay on `0.x` while it is only
   yours?

---

## What is already satisfied

Worth recording so Phase 9 does not redo it:

§6 multi-stage build, no secrets in image, `.dockerignore` · §7 non-root runtime
user · §9 `.env.example` documents every variable · §11 Postgres on a named
volume, health-checked, not publicly exposed · §12 ordered immutable migrations
with schema-version enforcement · §13 `/health/live`, `/health/ready`, `/version`
with commit and build time · §14 graceful shutdown, both processes · §15 stdout
logging with secret redaction · §20 no `localhost` assumptions in code.
