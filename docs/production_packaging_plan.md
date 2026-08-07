# Production Packaging Plan

Required by `first_production_packaging.md` §2. **Not to be implemented until
reviewed** (§2, §28).

**Date:** 2026-08-07 · **Commit:** `70f3a52`

Companion documents: `distribution_audit.md`, `persistent_data_inventory.md`,
`configuration_inventory.md`, `migration_risk_assessment.md`.

---

## 1. Where this stands

Phases 0–8 built something that works. What is missing is everything that makes
it *survivable on a machine that is not this one*: no backup, no restore, no
published image, no install path.

The audit found **8 required items, 4 recommended, 3 optional, and 4 decisions
that are yours.** The honest summary is one sentence:

> The application is production-*quality* and not production-*packaged*.

The single most important gap is not the missing tooling — it is that
`migration_risk_assessment.md` §7 has to say there is **no tested recovery path
from a failed migration**, because backup and restore do not exist. Everything
else is inconvenience by comparison.

## 2. Scope (§3)

One Linux host, Docker Engine, Docker Compose, one installation,
operator-controlled install and update, persistent local storage, documented
reverse-proxy path, reproducible image.

Explicitly **not**: Kubernetes, fleet control, automatic updates, zero-downtime
upgrades. The smallest operational model that is reliable and understandable.

## 3. Work orders

Ordered so that each is independently testable, and so the dangerous gap closes
early rather than last.

### WO-1 · Audit — **complete** (this document set)

### WO-2 · Installation identity — closes **F-01**
Generate the single `installation` row on first startup, idempotently. Expose it
in `admin status` and reserve it for the backup manifest. Small, but it blocks
WO-7 because a manifest cannot identify what it came from without it.

*Acceptance:* fresh install generates exactly one row; restart does not create a
second; the value survives container replacement.

### WO-3 · Version single-sourcing — closes **F-06**
`emaild/__init__.py` becomes authoritative. `pyproject.toml` reads it
dynamically; the Dockerfile receives it as a build arg from the release workflow.

*Acceptance:* changing one file changes `/version`, the image label, and the
package metadata together.

### WO-4 · Production image hygiene — closes **F-09**
Strip `pip` and `setuptools` from the runtime layer. Add OCI metadata from real
build args.

*Acceptance:* clean build, image starts, `/version` reports the true commit,
neither `pip` nor `setuptools` importable.

### WO-5 · Backup — closes **F-02**
`appctl backup` producing a single archive:

- `pg_dump -Fc` (logical, consistent under concurrent access — §11 forbids
  treating the live volume as portable)
- manifest: backup-format version, app version, schema revision, installation
  id, Postgres version, timestamp, **SHA-256 fingerprint of the encryption key**
- SHA-256 checksums of every member

Whether the key itself is included is **decision D-2 below**. The recommendation
is no, with the fingerprint recorded so restore can verify.

*Acceptance:* backup runs against a database with representative data; manifest
and checksums validate; secrets are handled per the decision.

### WO-6 · Restore — closes **F-03**
`appctl restore <archive>`, and §18's ordering is not negotiable: validate
format → validate checksums → validate app compatibility → validate schema
compatibility → **refuse to overwrite an active installation** → restore →
health-check → report.

*Acceptance (§24):* destroy the installation, create a clean environment,
restore, verify message history, suppression list, keys, and installation
identity, and verify the application is healthy. **A restore that only works on
the original machine does not count.**

### WO-7 · Upgrade fixture — closes the §26 gap
The dataset from `migration_risk_assessment.md` §6: messy, representative, and
versioned. Becomes the input to every future migration test.

*Acceptance:* fixture loads on the previous release's schema, survives upgrade to
head, and every relationship remains intact.

### WO-8 · `appctl` — closes **F-11**
`start · stop · restart · status · version · health · logs · config-check ·
backup · restore · doctor`. Wraps compose. Meaningful exit codes, no destructive
defaults, never prints secrets. `admin status` already provides the health verb.

*Acceptance:* every command runs on a clean host; `config-check` catches each of
the seven fail-closed conditions *before* an upgrade rather than at startup.

### WO-9 · Production compose + install script — closes **F-04**, **F-07**
`deploy/compose.yaml` referencing `ghcr.io/soupnchill/emaild:X.Y.Z` — no
`build:`, no source checkout on the destination. Plus an install script doing the
§19 sequence: check Docker, check architecture, check disk, create the directory,
template the config, generate secrets, generate the installation id, pull the
pinned image, start, health-check, print the URL and the backup instructions.

*Acceptance (§25 criteria 1–3):* a clean server installs from documented
artifacts, without the repository and without a Python toolchain.

### WO-10 · CI and image publishing — closes **F-05**, **F-10**
PR workflow: ruff, mypy, pytest, image build, container start, health check,
secret scan, dependency scan. Release workflow, separately authorised: verify the
version matches source, build once, tag, push to GHCR, record the digest.

§30 is explicit — PR workflows must not hold publishing credentials.

### WO-11 · Documentation — closes **F-08**
`installation.md`, `configuration.md`, `operations.md`, `backup-and-restore.md`,
`troubleshooting.md`, `architecture-overview.md`. §23: **every documented command
must be tested.** The Phase 8 precedent stands — the integration snippets were
each executed verbatim before shipping.

### WO-12 · Release candidate drill — §29
The full lifecycle on a clean host: install from a versioned release → create
realistic data → restart services → replace containers → verify → back up →
destroy → restore → verify the same data → verify health.

Until that sequence runs end to end, the application is packaged but not
production-ready.

## 4. Sequence

```
WO-2  installation identity   ▓ ½d   blocks WO-5
WO-3  version single-source   ▓ ½d
WO-4  image hygiene           ▓ ½d
WO-5  backup                  ▓▓ 1–2d
WO-6  restore                 ▓▓▓ 2d   ← the gap that matters
WO-7  upgrade fixture         ▓ 1d
WO-8  appctl                  ▓▓ 1–2d
WO-9  prod compose + install  ▓▓ 1–2d
WO-10 CI + publishing         ▓▓ 1–2d
WO-11 documentation           ▓▓ 1–2d
WO-12 release drill           ▓ 1d
```

Roughly **11–15 working days.** WO-2 through WO-6 are the ones that change
whether data can be lost; the rest change how pleasant the system is to operate.

## 5. Findings by category (§2)

**Required before first release:** F-01 installation id · F-02 backup ·
F-03 restore · F-04 production compose · F-05 image publishing · F-06 version
single-sourcing · F-07 install path · F-08 documentation

**Strongly recommended:** F-09 image hygiene · F-10 PR CI · F-11 appctl ·
F-12 support bundle

**Optional / future:** F-13 channels and canary (meaningless at one
installation) · F-14 multi-arch · F-15 historical upgrade fixtures

## 6. Decisions I need from you

### D-1 · Where does production actually run?

| Option | Consequence |
|---|---|
| **Stays on this dev box** | WO-9 is much smaller. But every product depending on emaild is then coupled to this machine's uptime. |
| **A LAN box** | Needs the install script to be real. Reachable from your network only. |
| **A VPS** | Needs the Cloudflare Tunnel path finished, and makes emaild reachable from anywhere a product runs. |

**DECIDED 2026-08-07: a LAN machine.**

Consequences accepted:

- **API keys travel in cleartext** on a plain-HTTP LAN. The installer says this
  out loud in `--lan` mode and generates a dashboard password because of it.
- **A product later deployed to a VPS cannot reach a LAN-only emaild.** Same
  silent-failure mode as the dev box, just a larger perimeter. The fix needs no
  rearchitecting: enable the `cloudflared` profile, which gives HTTPS and a
  hostname reachable from anywhere.

### D-2 · Does the backup contain the encryption key? — **DECIDED: no**

Fully argued in `persistent_data_inventory.md` §4.

*Recommendation:* **No.** Store it separately; record only a SHA-256 fingerprint
in the manifest so restore fails fast when given the wrong key. A lost key costs
an afternoon of re-provisioning; a leaked all-in-one backup costs every SMTP
credential at once.

### D-3 · Registry visibility — **DECIDED: private**

GHCR, matching the repo's private visibility, or public?

*Recommendation:* **private.** Nothing in the image is secret, but a public
image invites questions about a service you have not chosen to publish.

### D-4 · Version number at first release — **DECIDED: 0.9.0-rc.1 first**

*Recommendation:* **`0.9.0-rc.1`, then `1.0.0` only after WO-12 passes.** §22
permits pre-release versions precisely for this, and calling something 1.0.0
before a restore has ever been demonstrated on a clean host would be the kind of
claim the rest of this project has avoided making.

---

## 7. What I am NOT doing yet

Per §2 and §28: **no implementation until this is reviewed.** Nothing in Phase 9
has changed application behaviour — this commit adds five documents and nothing
else.

---

## 8. Release candidate drill — RESULT (2026-08-07)

Run against `v0.9.0-rc.1`, published to GHCR by `release.yml` run 31208182096.

| §29 step | Result |
|---|---|
| Install from a versioned release | pass — `inst_54479c2f47688cd20f540cb0` |
| Create realistic data | pass — domain to `ready`, mailbox, project, key, suppression, one real delivered email |
| Restart all services | pass — data intact |
| Replace disposable containers | pass — `api` and `worker` destroyed and recreated, data intact |
| Verify data | pass |
| Back up | pass — manifest carries version, schema, installation id, key fingerprint |
| Destroy the installation | pass — volume and directory removed |
| Restore onto a clean installation | pass |
| Verify the same data | pass — every count matched; canary suppression and full event timeline survived |
| Verify health | pass — **and it sent a real email afterwards** |

Two refusals confirmed along the way: restoring without the original encryption
key was rejected on fingerprint mismatch, and the fresh installation's identity
was replaced by the archive's rather than kept.

### Findings

**F-16 — RESOLVED 2026-08-07.** `admin` commands that do not touch MXRoute — `keys`,
`projects`, `suppressions`, `status` — still require the MXRoute credentials,
because the requirement sits on the role rather than the command. During the
drill this blocked key management on a freshly restored host until `.env` was
fully repopulated. The documented procedure covers it (restore `.env` alongside
the archive), so it was friction rather than a defect, but the requirement was
stricter than it needed to be.

Fixed by moving the check from the role to the command. `role=admin` no longer
demands MXRoute credentials at startup; the commands that actually reach the
provider check at the point of use and say which are missing and what still
works without them. Verified live: `keys list` exits 0 without credentials,
`domains token` exits 2 with actionable guidance.

**F-17 (cosmetic, open).** Worker heartbeats survive a restore, so a restored
installation briefly reports workers that no longer exist. They age out to
`STALE` within two minutes and are then replaced. Clearing the table on restore
would be tidier.

### Acceptance

§25's twenty criteria are met, with the single exception noted below.

**Not yet demonstrated:** pulling the published image from GHCR on this host.
The release workflow's own smoke test pulled and verified it
(`published image reports 0.9.0-rc.1`), but the local drill built from the
release tag instead, because the available token lacks `read:packages`. The
install path itself is unchanged either way — `install.sh` pulls when the image
is absent locally — but the registry pull has not been exercised from an
operator's machine.
