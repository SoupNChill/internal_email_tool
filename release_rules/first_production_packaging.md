# First Production Packaging

## Purpose

This document defines the process for converting a clean, functional local application into its first production-ready, self-hosted Docker Compose release.

The starting point is assumed to be an application that:

- Runs successfully in a local development environment
- May currently be started with commands such as `npm run dev`, `go run`, `python`, or `docker compose up`
- May use development-only configuration
- May store data in unsafe or undocumented locations
- Has not yet been packaged for installation on another server
- Has not yet demonstrated safe upgrades, backup, restore, or container replacement

The target is a reproducible single-host deployment that can be installed on a clean machine without cloning the source repository or installing development toolchains.

The intended initial platform is:

- GitHub for source control and release management
- GitHub Actions for automated validation and builds
- GitHub Container Registry for versioned OCI images
- Docker Compose for deployment
- Persistent volumes or documented bind mounts for state
- Explicit configuration and secrets handling
- Explicit database migrations
- Operator-controlled installation and upgrades

This document concerns the **first production packaging effort**.

Ongoing release and operational requirements are defined separately in `prod_release_rules.md`.

---

# 1. Production Packaging Goal

The production package must allow an operator to:

```text
Install the application on a clean server
Start and stop it predictably
Identify the exact running version
Replace application containers without losing data
Back up all irreplaceable state
Restore onto a clean installation
Upgrade to a newer version safely
Collect useful diagnostics
Understand how to recover from failure
```

The production package is not complete merely because a Docker image builds.

It is complete when the application has a tested operational lifecycle.

---

# 2. Required Agent Behavior

Before modifying the repository, inspect the existing application and produce an implementation plan grounded in the actual codebase.

Do not immediately redesign the application.

Do not apply generic Docker patterns without confirming that they fit the current architecture.

Do not replace working technology merely because another technology is more common.

First identify:

1. How the application currently starts
2. Which services it depends on
3. Which ports it uses
4. How configuration is loaded
5. Where secrets are stored
6. Where the database is stored
7. Where uploaded files are stored
8. Where generated files are stored
9. Where logs are written
10. Which state must survive restart
11. Which state must survive container replacement
12. Which state must survive full server migration
13. Whether schema migrations already exist
14. Whether any data is stored inside the application source directory
15. Whether any data is stored only inside a container filesystem
16. Whether the application assumes `localhost`
17. Whether the application assumes development-only hostnames, paths, or ports
18. Whether builds depend on undocumented local files
19. Whether the frontend contains hard-coded backend URLs
20. Whether the application can shut down cleanly

Before implementation, create:

```text
docs/distribution_audit.md
docs/production_packaging_plan.md
docs/persistent_data_inventory.md
docs/configuration_inventory.md
docs/migration_risk_assessment.md
```

Clearly separate findings into:

- Required before first production release
- Strongly recommended
- Optional future improvement
- Unresolved operator decision

Do not begin broad implementation until the audit and plan are reviewed.

---

# 3. Scope of the First Production Package

The first production package should target:

- One Linux host
- Docker Engine
- Docker Compose
- One independent application installation
- Operator-controlled installation
- Operator-controlled updates
- Persistent local storage
- A documented reverse-proxy path
- A reproducible release image
- A simple support and recovery model

The first milestone does not require:

- Kubernetes
- Service meshes
- Multi-region orchestration
- Automatic fleet-wide updates
- A custom container orchestrator
- Microservice decomposition
- Mandatory telemetry
- Complex multi-host failover
- A centralized fleet-control platform
- Zero-downtime upgrades unless the application specifically requires them

Prefer the smallest operational model that is reliable and understandable.

---

# 4. Separate Application, Configuration, and State

The production design must treat these as separate concerns.

## Application

The application consists of replaceable versioned artifacts:

- Backend executable or runtime
- Frontend assets
- Runtime libraries
- Container image
- Static application code

Application containers must be disposable.

## Configuration

Configuration includes:

- Environment variables
- Configuration files
- Port assignments
- Hostnames
- Feature flags
- Storage locations
- External-service endpoints

Configuration must remain outside the container image.

## Secrets

Secrets include:

- Database passwords
- Session secrets
- Encryption keys
- API credentials
- Private keys
- Signing keys
- Administrative bootstrap credentials

Secrets must not be committed to Git, embedded in images, printed in logs, or included in support bundles.

## Persistent State

Persistent state includes, as applicable:

- Database contents
- Uploaded files
- Generated files
- User-created documents
- Encryption keys
- Certificates
- Installation metadata
- Pending jobs
- Application-managed configuration
- License or entitlement state
- Audit history

Deleting and recreating application containers must not delete persistent state.

---

# 5. Persistent Data Inventory

Create `docs/persistent_data_inventory.md`.

For every persistent item, document:

| Data | Current Location | Production Location | Backup Method | Restore Method | Regenerable |
|---|---|---|---|---|---|
| Database | | | | | |
| Uploads | | | | | |
| Generated files | | | | | |
| Encryption keys | | | | | |
| Certificates | | | | | |
| Installation metadata | | | | | |

For each persistent path, identify:

- Required ownership
- Required permissions
- Responsible service
- Expected growth
- Whether concurrent access occurs
- Whether atomic backup is required
- Whether the data format changes between releases
- Whether the data contains secrets or personal information

No irreplaceable data may remain only inside:

- A writable container layer
- A temporary directory
- A source-code checkout
- A developer home directory
- An undocumented host path

---

# 6. Production Dockerfile

Create a production Dockerfile appropriate to the existing stack.

The Dockerfile must:

- Build successfully on a clean machine
- Use deterministic dependency installation
- Use lockfiles where supported
- Use a multi-stage build when it reduces the final image or removes build tools
- Exclude development-only dependencies from the final image
- Exclude source-control metadata
- Exclude `.env` files and credentials
- Run as a non-root user where practical
- Use an explicit working directory
- expose only required ports
- Handle termination signals correctly
- Include or support a health check
- Write logs to standard output and standard error
- Write persistent state only to documented persistent paths
- Avoid downloading mutable dependencies at container startup
- Include application version and source commit metadata
- Support the declared CPU architecture
- Produce the same application artifact regardless of the developer workstation

Create a `.dockerignore` that excludes at least:

```text
.git
.github
.env
.env.*
node_modules
venv
__pycache__
dist
build
coverage
test-output
backups
local-data
IDE settings
temporary files
developer credentials
```

Adjust the list to the actual project.

Do not copy the entire repository into the final image without reviewing what is included.

---

# 7. Runtime User and Filesystem Permissions

The production container should not run as root unless there is a documented technical requirement.

Define:

- Runtime user
- Runtime group
- Writable paths
- Read-only paths
- Volume ownership
- Host bind-mount permissions
- Upgrade behavior when UID or GID values change

The container must fail clearly when it cannot write to a required persistent path.

Do not solve permission problems by applying unrestricted permissions such as `chmod 777` without a documented reason.

---

# 8. Docker Compose Production Suite

Create a production deployment directory such as:

```text
deploy/
├── compose.yaml
├── .env.example
├── README.md
└── optional-overrides/
```

The production Compose file must:

- Reference published images rather than building from source
- Pin application images to an explicit version
- Define persistent volumes or documented bind mounts
- Define restart policies
- Define service health checks
- Expose only required public ports
- Keep databases and internal services off public interfaces
- Use service names for internal communication
- Support configuration through documented variables
- Avoid embedding secrets
- Declare service dependencies appropriately
- Define predictable container and volume behavior
- Allow application containers to be recreated safely
- Include logging behavior appropriate to the environment
- Avoid development-only mounts and hot reload
- Avoid mounting the source repository into production containers

Production must not depend on:

```yaml
build: .
```

on the destination server.

Production should use:

```yaml
image: ghcr.io/organization/application:1.0.0
```

Do not use `latest` as the installed production version.

---

# 9. Configuration Design

Create `.env.example` containing every supported configuration variable with safe placeholders.

For every variable, document:

- Purpose
- Required or optional
- Default behavior
- Secret or non-secret
- Valid values
- Whether it may change after installation
- Whether changing it requires restart
- Whether changing it affects stored data
- Whether changing it requires migration

Classify configuration into:

## Installation-Time Configuration

Examples:

- Public hostname
- Database engine
- Persistent storage location
- Installation mode
- Initial administrative identity

## Runtime Configuration

Examples:

- Log level
- Feature flags
- Worker count
- Request limits

## Secrets

Examples:

- Database password
- Session signing key
- Encryption key
- External API token

## Generated Immutable Values

Examples:

- Installation ID
- Internal encryption seed
- Application-specific node identity

Generated secrets and identifiers must be created once and preserved across upgrades.

An upgrade must never silently regenerate keys that would invalidate sessions or make stored data unreadable.

---

# 10. Installation Identity

Generate a random immutable installation ID during first installation.

The identifier must:

- Persist outside application containers
- Persist across upgrades
- Persist across backup and restore
- Avoid customer names, IP addresses, hostnames, or personal information
- Be available through diagnostic tooling
- Be included in backup metadata

Record or expose:

```text
Installation ID
Application version
Source commit
Image digest
Schema version
Installation date
Last successful upgrade
Deployment mode
```

---

# 11. Database Packaging and Persistence

Determine whether the application uses:

- SQLite
- PostgreSQL
- MySQL or MariaDB
- Another database
- Local files acting as an informal database

Preserve the current database choice unless changing it is necessary and approved.

## SQLite

For SQLite:

- Place the database in a persistent mounted path
- Ensure only supported processes access the database
- Include associated WAL and shared-memory behavior in backup design
- Use the database backup API or a tested consistent backup method
- Do not copy an active database file casually
- Test restart and container-replacement behavior

## PostgreSQL or Similar Service

For a database service:

- Use a persistent volume
- Do not publish its port publicly by default
- Use a health check
- Store credentials outside Compose source
- Pin a supported database major version
- Document database upgrades separately from application upgrades
- Use logical or otherwise consistent backups
- Do not treat the live database volume as a portable backup

---

# 12. Migration Framework

Implement an explicit ordered migration framework if one does not already exist.

Requirements:

- Every migration has a permanent unique identifier
- Applied migrations are tracked
- Released migration files are immutable
- Migration failure stops the release process
- The application detects incompatible schema versions
- A fresh database can be created entirely from committed migrations or an equivalent controlled schema process
- An older supported database can be upgraded to the new version
- Representative existing data survives migration
- Destructive operations are explicitly identified
- Migration output is visible to the operator
- Migration execution is separate from ordinary request handling where practical

Prefer forward-compatible expand-and-contract changes.

Do not perform destructive schema changes merely to simplify the new code.

Do not drop existing columns, tables, or records without explicit review.

Create:

```text
docs/migration_risk_assessment.md
```

Document:

- Current schema-management behavior
- First production schema version
- Known unsafe assumptions
- Data-cleaning requirements
- Potentially destructive transformations
- Rollback limitations
- Required test datasets

---

# 13. Health Endpoints

Provide:

```text
GET /health/live
GET /health/ready
GET /version
```

## Liveness

Liveness confirms that the application process is running.

It should not fail merely because an optional external service is temporarily unavailable.

## Readiness

Readiness confirms that the application can serve normal traffic.

It should check required dependencies such as:

- Database connectivity
- Required storage paths
- Required internal services
- Schema compatibility
- Critical initialization completion

## Version

Version output should include only non-secret build information:

```json
{
  "version": "1.0.0",
  "commit": "abc1234",
  "buildTime": "2026-08-03T20:00:00Z",
  "schemaVersion": 12
}
```

The exact format may differ.

---

# 14. Graceful Startup and Shutdown

The application must:

- Receive container termination signals
- Stop accepting new work when shutting down
- Finish or safely abandon in-progress operations
- Close database connections
- Flush required writes
- Stop background workers
- Exit within a documented timeout
- Return a non-zero exit code on startup failure

Do not hide failed initialization by starting in a partially broken state unless a deliberate degraded mode is documented.

---

# 15. Logging

Production logs should normally be written to standard output and standard error.

Logs should include:

- Timestamp
- Severity
- Service or component
- Event message
- Request or correlation identifier where useful
- Error details
- Application version where practical

Logs must not include:

- Passwords
- Session tokens
- Authorization headers
- API keys
- Private keys
- Complete database connection strings containing credentials
- Sensitive uploaded content
- Unnecessary personal information

Document how to retrieve logs:

```bash
docker compose logs
docker compose logs -f app
```

Or provide an operator wrapper.

---

# 16. Operator Management Command

Create an operator-facing command named `appctl`, or an equivalent appropriate to the project.

For the first production release, it should support at least:

```text
appctl start
appctl stop
appctl restart
appctl status
appctl version
appctl health
appctl logs
appctl config-check
appctl backup
appctl restore
appctl doctor
```

The command may call Docker Compose internally.

It must:

- Use meaningful exit codes
- Validate required files
- Detect missing Docker or Compose
- Avoid destructive defaults
- Avoid printing secrets
- Explain failures clearly
- Operate from a documented installation directory
- Avoid depending on the current shell directory where practical

Future update and rollback behavior may be added after backup and restore are proven.

---

# 17. Backup Packaging

Implement a backup command covering all irreplaceable state.

A backup should include, as applicable:

- Consistent database backup
- Uploaded files
- Generated user-owned files
- Required encryption keys
- Certificates required for recovery
- Installation metadata
- Sanitized configuration
- Backup manifest
- Checksums

A backup must be stored outside the live application data volume or copied to a clearly documented destination.

A backup manifest should include:

```json
{
  "backupFormatVersion": 1,
  "applicationVersion": "1.0.0",
  "schemaVersion": 12,
  "installationId": "inst_example",
  "databaseEngine": "postgresql",
  "databaseVersion": "17",
  "createdAt": "2026-08-03T20:30:00Z"
}
```

Do not claim that named volumes are backups.

---

# 18. Restore Packaging

Implement restoration onto a clean installation.

The restore process must:

1. Validate the backup format
2. Validate checksums
3. Validate application compatibility
4. Validate database compatibility
5. Refuse accidental overwrite of an active installation
6. Restore required database contents
7. Restore persistent files
8. Restore required keys
9. Restore installation metadata
10. Start the application
11. Run health checks
12. Report success or failure clearly

The restore test must begin from a clean environment.

A restore that only works on the original machine is insufficient.

---

# 19. Initial Install Script

Create an installation script or guided process that:

1. Checks supported operating system assumptions
2. Checks Docker availability
3. Checks Docker Compose availability
4. Checks CPU architecture
5. Checks disk space
6. Creates the installation directory
7. Creates configuration from safe templates
8. Generates required secrets
9. Generates the installation ID
10. Pulls the exact release image
11. Creates persistent storage
12. Initializes the database
13. Starts services
14. Runs health checks
15. Displays the resulting URL
16. Displays backup instructions
17. Displays the installed version

The script must be safe to re-run or clearly refuse unsafe repetition.

Do not overwrite existing configuration or data silently.

---

# 20. Reverse Proxy and TLS

The application should not require a specific reverse proxy unless necessary.

Document a supported path for:

- Direct LAN access
- Reverse proxy access
- HTTPS termination
- WebSocket forwarding if needed
- Forwarded headers
- Trusted proxy configuration
- Upload-size limits
- Request timeouts

Do not hard-code production domains into the application image.

Do not expose the database or internal services through the reverse proxy.

---

# 21. GitHub Actions for the First Production Build

Create pull-request CI that runs applicable checks:

- Formatting
- Linting
- Static analysis
- Type checking
- Unit tests
- Integration tests
- Production container build
- Container startup
- Health checks
- Secret scanning
- Dependency scanning

Create a protected release workflow that:

1. Accepts or derives an explicit version
2. Verifies the version matches the source
3. Runs required tests
4. Builds the production image
5. Adds version and commit metadata
6. Pushes the image to GitHub Container Registry
7. Records the image digest
8. Publishes release information
9. Uses least-privilege permissions
10. Requires explicit authorization for release publication

Pull-request workflows must not have access to production publishing credentials.

---

# 22. Versioning

Use semantic versioning:

```text
MAJOR.MINOR.PATCH
```

Pre-production releases may use:

```text
0.1.0
0.5.0-alpha.1
0.8.0-beta.1
0.9.0-rc.1
1.0.0
```

Choose one authoritative source for the application version.

Avoid manually maintaining unrelated version strings in many files.

The first production deployment must pin an exact image version.

Example:

```yaml
image: ghcr.io/example/application:1.0.0
```

Do not install production using:

```yaml
image: ghcr.io/example/application:latest
```

---

# 23. Documentation

Create:

```text
docs/
├── installation.md
├── configuration.md
├── operations.md
├── backup-and-restore.md
├── troubleshooting.md
├── architecture-overview.md
├── persistent_data_inventory.md
├── migration_risk_assessment.md
└── production_packaging_plan.md
```

Documentation must cover:

- System requirements
- Supported operating systems
- Supported architecture
- Required ports
- Installation
- Initial configuration
- Startup and shutdown
- Logs
- Health checking
- Backup
- Restore
- Data locations
- Complete uninstall
- Data-preserving uninstall
- Known limitations
- Recovery from common failures
- How to report a useful support issue

Every documented command must be tested.

Do not document commands that have not been implemented.

---

# 24. Required Production-Package Tests

## Clean Build

- Build the image without developer-specific files
- Verify no secrets are present
- Verify production dependencies only
- Verify the image starts successfully

## Fresh Installation

- Begin with a clean Docker environment
- Install from documented artifacts
- Do not clone the source repository
- Do not install compilers or application build tools
- Verify application health
- Create representative data

## Restart Persistence

- Create representative data
- Restart every service
- Verify the data remains intact

## Container Replacement

- Create representative data
- Remove the application container
- Recreate it from the same image
- Verify all state remains intact

## Host Reboot

- Reboot or simulate restart of the Docker host
- Verify services return
- Verify health
- Verify data remains intact

## Backup

- Create representative data
- Produce a backup
- Validate the manifest
- Validate checksums
- Verify secrets are handled correctly

## Restore

- Destroy the test installation
- Recreate a clean environment
- Restore the backup
- Verify database contents
- Verify uploaded files
- Verify keys and installation identity
- Verify application health

## Invalid Configuration

- Remove a required value
- Supply an invalid value
- Confirm configuration validation fails clearly
- Confirm secrets are not printed

## Permission Failure

- Make a required storage path unwritable
- Confirm the application fails clearly
- Confirm the failure identifies the affected path

## Health Failure

- Make the database unavailable
- Confirm readiness fails
- Confirm liveness behavior remains appropriate
- Restore the database
- Confirm readiness recovers

---

# 25. First Production Acceptance Criteria

The first production packaging effort is complete only when:

1. A clean supported server can install the application from documented release artifacts.
2. The destination server does not need the source repository.
3. The destination server does not need application compilers or development toolchains.
4. The application image is tied to an exact source commit and version.
5. The application runs without development mounts or hot reload.
6. Configuration is external to the image.
7. Secrets are not committed or embedded in images.
8. All persistent data locations are documented.
9. Application containers can be deleted and recreated without data loss.
10. All services survive a host restart.
11. Database schema state is explicit.
12. A backup can be created successfully.
13. A backup can restore onto a clean installation.
14. Health checks detect critical dependency failures.
15. Logs can be collected without entering containers.
16. The running application version can be identified.
17. Installation instructions have been tested.
18. Restore instructions have been tested.
19. Known limitations are documented honestly.
20. CI can reproducibly build and validate the production image.

---

# 26. Implementation Work Orders

Perform the production packaging in small, independently testable work orders.

Recommended order:

## Work Order 1: Repository and State Audit

Deliver:

- Architecture summary
- Persistent-data inventory
- Configuration inventory
- Secret inventory
- Deployment risks
- Proposed file changes

Do not implement broad changes.

## Work Order 2: Persistent State Separation

Move or configure persistent state so that application containers are disposable.

Acceptance test:

```text
Create data
Delete application container
Recreate application container
Verify data
```

## Work Order 3: Migration Foundation

Implement or validate schema tracking and migration execution.

Acceptance test:

```text
Create old schema data
Run migration
Verify schema
Verify existing data
```

## Work Order 4: Production Image

Create the production Dockerfile and `.dockerignore`.

Acceptance test:

```text
Build clean image
Run image
Verify health
Verify version metadata
Verify no development dependencies
```

## Work Order 5: Production Compose Suite

Create the production Compose deployment.

Acceptance test:

```text
Start from published images
Verify services
Restart services
Verify persistence
```

## Work Order 6: Configuration and Secrets

Create templates, validation, and secret generation.

Acceptance test:

```text
Install with valid configuration
Reject invalid configuration
Verify secrets are not logged
```

## Work Order 7: Backup

Implement consistent full backup.

Acceptance test:

```text
Create representative data
Run backup
Validate manifest and checksums
```

## Work Order 8: Restore

Implement clean-machine restoration.

Acceptance test:

```text
Destroy installation
Create clean installation
Restore backup
Verify all representative data
```

## Work Order 9: Operator Tooling

Implement `appctl` commands, diagnostics, and health checks.

## Work Order 10: CI and Image Publishing

Implement pull-request validation and explicitly authorized image publishing.

## Work Order 11: Documentation

Test every installation, operation, backup, restore, and troubleshooting command.

## Work Order 12: Release Candidate Drill

Perform the complete first-production lifecycle:

```text
Build release
Publish exact image
Install on clean host
Create representative data
Restart host
Replace containers
Back up
Destroy installation
Restore
Verify data
Verify health
```

---

# 27. Required Work-Order Report

After each work order, provide:

```text
Work order:
Status:

Files created:
Files modified:
Files deleted:

Behavior changed:

Commands executed:

Tests run:
- Test:
- Result:

Acceptance criteria:
- Criterion:
- Evidence:

Persistent-data impact:

Database impact:

Configuration impact:

Security impact:

Known limitations:

Manual verification required:

Rollback procedure:

Recommended next work order:
```

Do not describe a work order as complete without test evidence.

---

# 28. Initial Agent Instruction

Use the following instruction to begin:

```text
Read `first_prod_packaging.md` and inspect the complete repository.

Do not implement broad changes yet.

Produce the repository audit, production packaging plan, persistent-data
inventory, configuration inventory, and migration risk assessment required by
the document.

Ground every conclusion in the actual repository.

Identify anything that would be lost if the application directory were
deleted, if application containers were recreated, or if a different Git
version were checked out.

Separate findings into:

1. Required before first production release
2. Strongly recommended
3. Optional future work
4. Unresolved operator decisions

Divide the implementation into small work orders. Each work order must identify
scope, files affected, acceptance criteria, tests, risks, persistent-data
impact, and rollback procedure.

Do not silently redesign the application.
```

---

# 29. Completion Principle

The first production package is successful when the following lifecycle works:

```text
Install from a versioned release
→ create realistic data
→ restart all services
→ replace disposable containers
→ verify data
→ create a backup
→ destroy the test installation
→ restore onto a clean installation
→ verify the same data
→ verify application health
```

Until that sequence is demonstrated, the application is packaged but not yet production-ready.