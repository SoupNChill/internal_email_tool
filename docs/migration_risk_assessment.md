# Migration Risk Assessment

Required by `first_production_packaging.md` §12.

**Date:** 2026-08-07 · **Schema head:** `f53f35ee9839` · **Schema version:** 2

## 1. Current schema-management behaviour

Alembic, with migrations run as a **separate compose unit** (`migrate`) that must
exit successfully before `api` or `worker` start. §37 step 10 — migration is never
an application side effect, so a failure stops the deployment rather than
half-starting the service.

The `migrate` service holds **only the DSN**. It is given no application secrets
at all: `alembic/env.py` reads `EMAILD_DATABASE_URL` from the environment and
never constructs `Settings`. Least privilege applies to schema changes too.

## 2. Migration chain

| Revision | Parent | Description |
|---|---|---|
| `8190aa0c4905` | — | initial schema (10 tables) |
| `533f553dfaec` | `8190aa0c4905` | installation identity |
| `7909a5b29a0c` | `533f553dfaec` | send accounting column + rate-window index |
| `f53f35ee9839` | `7909a5b29a0c` | worker heartbeats + provider latency |

Verified today against a clean database: **4 upgrades apply, 4 downgrades
succeed.** The full round trip works.

## 3. Destructive-operation review (§9, §12)

**Every `upgrade()` in the chain is purely additive.** Checked programmatically —
no `drop_table`, `drop_column`, `drop_constraint`, or `alter_column` appears in
any upgrade path. The `drop_*` calls that exist are confined to `downgrade()`
bodies, which is what they are for.

**Zero non-nullable columns were added to populated tables.** Every added column
is either nullable or carries a server default — the case §7 calls out
explicitly as a way migrations break on real data.

Consequence: **no migration shipped so far can destroy data.** That is a
property of where the project is, not a guarantee about the future.

## 4. First production schema version

`f53f35ee9839`, schema version **2**. `emaild/__init__.py` declares
`MIN_SCHEMA_VERSION = 1`, `MAX_SCHEMA_VERSION = 2`, and `/health/ready` compares
the applied revision against the head this build ships — distinguishing
`migrations_pending` from `schema_ahead_of_application`, and refusing traffic on
either (§11).

That second case is the important one: an older application writing against a
newer schema is a data-corruption risk, not a warning.

## 5. Known unsafe assumptions

**None found in the migrations themselves** — they are additive, so §7's list of
things migrations must not assume (populated fields, unique names, valid dates,
clean encoding) does not yet apply.

The assumptions that *do* exist live in the application:

| Assumption | Where | Risk if violated |
|---|---|---|
| `installation` holds at most one row | check constraint `id = 1` | Enforced by the database. |
| A mailbox address is globally unique | unique constraint | Enforced. |
| Enum values are the lowercase strings | `_pg_enum` `values_callable` | Enforced at DDL. Broke once in Phase 1 and was caught. |
| `alembic_version` matches a shipped revision | readiness check | Refuses traffic rather than guessing. |

## 6. Required test datasets (§12, §26)

Migration testing so far has used ad-hoc rows created during each phase. A real
upgrade fixture does not exist, and §26 is explicit that migrations must not be
tested only against empty databases.

**Required for the first release:** a versioned fixture containing at least —

- messages in every status, including `needs_review`
- events with gaps in `sequence` (from failures and retries)
- suppressions from both `manual` and `bounce` sources
- a revoked key, an inactive project, a `misconfigured` domain
- null values in every nullable column
- unicode in subjects and display names, and a subject at the 998-byte limit
- an expired idempotency key

That fixture becomes the input to the upgrade test in every subsequent release.

## 7. Rollback limitations — stated honestly (§10)

```
Application image rollback : supported (containers are disposable)
Configuration rollback     : supported (.env is a file)
Schema downgrade           : implemented and exercised, but NOT SUPPORTED
Recovery after failure     : restore the pre-update backup — WHICH DOES NOT
                             YET EXIST (see F-02/F-03)
```

Downgrades run cleanly today, but §10 is explicit: **if a database downgrade is
not tested against representative data, it is not supported.** They have only
ever been run against near-empty databases, so they are a development
convenience, not a recovery path.

**The honest current position: there is no tested recovery path from a failed
migration.** That is the single largest gap in this assessment, and it is why
backup and restore are work orders 7 and 8 rather than later.

## 8. Recommendations for the first release

1. **Build the upgrade fixture** (§6 above) before any further schema change.
2. **Do not claim downgrade support.** State it as unsupported in release notes
   until it is tested against the fixture.
3. **Keep migrations additive** while a tested restore path is missing. Prefer
   expand-and-contract (§8): a column that is added and stops being written is
   recoverable; a dropped one is not.
4. **Pre-update backup must gate the upgrade** (§13, §36) — the updater should
   refuse to migrate when it cannot produce one.
