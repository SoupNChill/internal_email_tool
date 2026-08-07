# Deployment & Release Posture

**Companion to:** `build_plan.md`, `release_rules/production_release_rules.md`
**Date:** 2026-08-06

## 1. Why this project deviates from the usual localhost-first path

The normal workflow — build to `localhost:1234`, migrate if it proves value — assumes the thing being built is an *application*. This one is a **dependency of applications**.

That inverts the migration story. Under the usual pattern, a product and its email API would move together. Here they won't: the email service stays put while the products it serves get built, tested, and relocated to other machines and VPSes. A product that migrates to a VPS while its email API sits behind the dev-box firewall doesn't degrade — it silently stops sending.

**Consequence: this service needs a stable, location-independent hostname earlier than a typical project would.** Not on day one — the local dev loop is fine for Phases 1–7 — but before the first real product integrates against it, because the base URL handed to that product should never have to change.

That argues for deployment pattern 2 (Cloudflare Tunnel). It resolves identically from the dev box, a LAN machine, or a remote VPS, with no inbound ports and no certificate management.

## 2. Deployment model: one compose file, two profiles

The deployment mode is a configuration concern, not an architectural one. Same image, same compose file, same code — profiles decide exposure.

```yaml
services:
  postgres:    # never publishes a host port outside local dev
  api:         # binds 127.0.0.1; tunnel reaches it on the internal network
  worker:      # no listener at all; outbound SMTP only
  cloudflared: # profile: public
```

| Command | Result |
|---|---|
| `docker compose up` | Local only. API on `127.0.0.1`. The Phase 1–7 dev loop. |
| `docker compose --profile public up` | Adds the tunnel. `https://mail.<domain>` resolves from anywhere. |

Promotion from private to public is a flag, never a rewrite. This satisfies `release_rules` §46 — a new listening port is release-significant, so exposure changes are explicit and reviewable rather than emergent.

### Cloudflare Access must not cover the API

The single most important detail, and the easiest to get wrong:

| Route | Protection |
|---|---|
| `/v1/*` | **Bearer API keys only. No Cloudflare Access.** |
| `/` , `/admin/*` (dashboard) | **Cloudflare Access** — SSO or one-time PIN |
| `/health/*` , `/version` | Unauthenticated, but must leak nothing (§17, §48) |

Putting Access in front of `/v1/*` would break every integration on the platform. Machine clients cannot complete an interactive SSO challenge — they would receive an HTML login page where they expect JSON, and the failure would present as a baffling parse error rather than an auth error. Access protects humans; bearer keys protect machines.

If tighter API protection is ever wanted, the mechanism is Access **Service Tokens**, not the standard Access policy.

### Trusted proxy configuration

Behind the tunnel, `CF-Connecting-IP` and forwarded headers are only trustworthy when they originate from the tunnel itself. FastAPI must be configured to trust proxy headers **exclusively** from the internal network, never wildcard. Per §46: reverse-proxy trust is explicit.

## 3. Credential separation — the public-exposure mitigation

An internet-reachable service holding an account-root MXRoute key is the main new risk introduced by going public. It is avoidable, because the three roles need different secrets:

| Component | Needs | Does **not** need |
|---|---|---|
| `api` (public) | DB, key-hash comparison | MXRoute admin key, mailbox passwords |
| `worker` (no listener) | DB, mailbox password decryption key | MXRoute admin key |
| provisioning (admin only) | MXRoute admin key | public exposure |

**The MXRoute admin key never enters the publicly-reachable container.** Domain and mailbox provisioning runs as an admin-only path — CLI or a service not routed through the tunnel. A compromise of the public API surface then yields sending within existing scopes, not the ability to delete mailboxes or manipulate reseller users.

This is `release_rules` §17 ("scoped to the minimum necessary permissions") applied concretely, and it costs nothing if designed in at Phase 1. Retrofitting it means moving secrets between running containers.

## 4. Irreplaceable state inventory (§6)

Required before any release work. What actually cannot be recreated:

| State | Recoverable? | Notes |
|---|---|---|
| `messages` + `events` history | **No** | The audit trail *is* the product. Sole irreplaceable data. |
| Mailbox password encryption key | Painfully | Loss ⇒ every mailbox password undecryptable. Recoverable only by `PATCH`-ing new passwords via the MXRoute API and re-provisioning. |
| API key hashes | No, but reissuable | One-way by design. Recovery is issuing new keys and updating consumers. |
| `domains` verification state | Yes | Rebuildable from the MXRoute API plus DNS. |
| `suppressions` | **No** | Hand-curated; rebuilding means re-learning from bounces. |
| `.env` / config | No | Backed up separately from the database, never in the image. |

Two of these — event history and suppressions — have no reconstruction path at all. That is what §13–15 backups exist to protect, and it is why "a Docker volume is not a backup" (§14) applies literally here.

Note the encryption key is *not* fully irrecoverable, purely because MXRoute lets us reset mailbox passwords through the API. Worth recording, because it downgrades the worst-case from catastrophic to merely tedious.

## 5. Which release rules bind now

`production_release_rules.md` is written for a product with a **fleet of installations** — customers, canaries, rollout waves, support lifecycles, release revocation. This project is a **single internal installation with one operator**. Applying all 60 sections literally would be ceremony with no beneficiary.

The honest split:

### Binding from Phase 1 — these protect real, irreplaceable state

| § | Rule | Why it applies here |
|---|---|---|
| 6 | Persistent state survives releases | Event history and suppressions cannot be rebuilt |
| 7 | Migrations committed, released ones immutable | Alembic; matters from the first migration onward |
| 9 | Destructive migrations need backup + approval | Message history is the product |
| 10 | Rollback honesty | Image rollback ≠ schema downgrade. State it explicitly. |
| 13–15 | Backup before upgrade; restore is proven | The `pg_dump` + key backup + tested restore loop |
| 16–17 | Config and secret hygiene | MXRoute root key, mailbox passwords, key hashes |
| 19 | Container hygiene | Pinned bases, non-root, minimal surface |
| 21 | `/health/live`, `/health/ready`, `/version` | Adopting these exact paths verbatim |
| 22–23 | Logging and error handling | Directly shapes the 5xx classifier: never silently drop |
| 24–25 | Test integrity | No weakening tests to pass CI |
| 27–28 | AI change governance | See below — this constrains me |
| 44–46 | Encryption, authz, network exposure | Exactly the risk surface here |

### Deferred until this is more than one installation

§32 release channels · §33 canary · §34 rollout waves · §50 installation inventory · §51 support lifecycle · §52 vulnerability response process · §53 release revocation · §26 upgrade fixtures (needs prior releases to exist)

These activate the moment a second installation exists — a VPS deployment, or handing this to anyone else. Not deleted, dormant.

### Adapted rather than skipped

- **§4 semantic versioning** — applies fully, but §5 supported-upgrade-paths starts trivially (`0.x → 0.x+1`) until there is history.
- **§29 separation of authority** — solo operator, so the doc's own carve-out applies: same person, logically separate steps. Practically: I do not both write a migration and declare it safe. See below.
- **§39 release notes** — kept, scaled to the audience (one reader).
- **§48 support bundles** — the redaction discipline applies to diagnostics generally, even without formal bundles.

## 6. What §27–28 mean for how I work here

The rules name specific categories where an AI agent must not be the sole authority. This project touches nearly all of them: migrations, authentication, authorization, encryption, key management, backup, restore, network exposure, secret handling.

Practically, for each of these I will produce the change plus an explicit data-impact and security-impact statement, and **stop for approval rather than self-certify**. Concretely, that means I will not:

- Run a destructive migration against data that exists
- Rotate or regenerate the mailbox encryption key as part of ordinary work
- Change what is publicly exposed without saying so plainly
- Report a backup or restore as working without having actually restored one
- Treat "tests passed" as evidence that data survived (§25)

The §57 agent report format applies to release work, not to ordinary Phase 1–7 building. It activates at the first tagged release.
