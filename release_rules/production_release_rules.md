# Production Release Rules

## Purpose

This document defines the permanent release, upgrade, data-safety, security, testing, and operational rules for production versions of this application.

These rules apply after the application has completed its initial production packaging.

They govern:

- Every production release
- Every database change
- Every configuration change
- Every container image
- Every upgrade path
- Every backup-format change
- Every release workflow
- Every AI-generated change that affects production behavior
- Every supported production installation

These rules are not a one-time checklist.

They are an ongoing release contract.

---

# 1. Core Release Principles

Every production release must be:

- Versioned
- Reproducible
- Traceable to source
- Tested
- Recoverable
- Observable
- Documented
- Explicitly authorized
- Safe for existing data
- Honest about compatibility and rollback limitations

A release is not complete merely because:

- The code compiles
- Tests written for the new feature pass
- The container starts
- A development installation works
- The AI agent reports success
- The latest branch appears stable

A release must prove that existing supported installations can move forward safely.

---

# 2. Application Artifacts Are Immutable

Every production release must produce a versioned immutable container image.

A release artifact must be traceable to:

- Application version
- Git commit
- Build workflow
- Build timestamp
- Container image digest
- Dependency lock state
- Migration set

Once published, a released version must not be silently replaced with different contents.

Do not rebuild and overwrite:

```text
1.4.2
```

with a different image.

If the artifact changes, create a new version.

Production deployments must pin exact versions or exact image digests.

Do not use mutable tags such as `latest` as the installed version.

Mutable aliases such as `stable` may exist for discovery or promotion, but the installation must record the resolved exact version and digest.

---

# 3. One Build, Multiple Promotions

Build the production artifact once.

Promote the same image digest through:

```text
development validation
→ release candidate
→ internal persistent installation
→ canary
→ stable
```

Do not separately rebuild nominally identical production artifacts for different stages.

A rebuilt artifact is a different artifact even when it uses the same source commit.

---

# 4. Semantic Versioning

Use:

```text
MAJOR.MINOR.PATCH
```

Interpretation:

- `MAJOR`: incompatible application, API, configuration, data, or operational change
- `MINOR`: backward-compatible feature or meaningful capability
- `PATCH`: backward-compatible fix

Use prerelease versions when appropriate:

```text
1.5.0-alpha.1
1.5.0-beta.1
1.5.0-rc.1
```

Do not disguise breaking changes as patch releases.

Version decisions must consider more than user-facing behavior.

A change may be breaking if it alters:

- Database compatibility
- Backup compatibility
- Configuration requirements
- Authentication behavior
- Network interfaces
- Public API behavior
- File formats
- Plugin interfaces
- Storage layout
- Minimum supported host environment
- Reverse-proxy assumptions

---

# 5. Supported Upgrade Paths

Every release must define its supported upgrade sources.

Example:

```text
Target version: 1.5.0

Supported direct upgrades:
- 1.4.0 through 1.4.9

Requires intermediate upgrade:
- 1.2.x → 1.3.8 → 1.5.0

Unsupported:
- Versions older than 1.2.0
```

The updater must refuse unsupported version jumps.

Do not rely on documentation alone when the application can enforce compatibility.

Record:

- Current application version
- Current schema version
- Target application version
- Target schema version
- Minimum supported source version
- Required intermediate releases

---

# 6. Persistent State Must Survive Releases

Application containers remain disposable.

Every release must preserve:

- Database contents
- Uploaded files
- Generated user-owned files
- Encryption keys
- Certificates required for stored data
- Installation identity
- Required application configuration
- Audit history
- Supported queued work
- Upgrade history

A release must not begin storing new irreplaceable state inside a container layer or temporary path.

Any new persistent state must be added to:

- Persistent-data inventory
- Backup process
- Restore process
- Permission model
- Upgrade tests
- Support diagnostics where appropriate

---

# 7. Database Migration Rules

Database changes are among the highest-risk release activities.

Every production database change must use a committed migration or equivalent version-controlled schema mechanism.

## Released Migrations Are Immutable

After a migration has been included in a production release:

- Do not edit it
- Do not rename it
- Do not reorder it
- Do not change its checksum
- Do not replace it with a different migration using the same identifier

Correct mistakes with a new migration.

## Unique Identity

Each migration must have a unique permanent identifier.

## Schema Tracking

The application must track which migrations have been applied.

## Failure Behavior

Migration failure must:

- Stop the upgrade
- Return a non-zero result
- Leave clear diagnostic output
- Avoid starting an incompatible application version
- Preserve the pre-update backup
- Avoid falsely marking the release as successful

## Data Assumptions

Migrations must not assume:

- Every field is populated
- Names are unique
- Dates are valid
- Character encoding is clean
- Legacy records match current validation rules
- Foreign-key relationships are complete
- Files referenced by the database still exist
- No duplicate records exist
- Only modern application versions created the data

Production data is messy by default.

---

# 8. Prefer Expand-and-Contract Migrations

Prefer multi-release compatible changes.

Example: replacing `customer_name` with `display_name`.

## Expand

- Add `display_name`
- Preserve `customer_name`
- Write to both where needed
- Support reading old records

## Migrate

- Populate `display_name`
- Verify migration completeness
- Switch normal reads

## Contract

- Remove `customer_name` only in a later release
- Remove it only after supported rollback and upgrade windows no longer require it

Avoid combining these into one irreversible release unless necessary.

---

# 9. Destructive Migration Rules

The following are destructive or potentially destructive:

- Dropping a table
- Dropping a column
- Deleting records
- Replacing values without preserving originals
- Rewriting file formats
- Renaming stored files
- Changing encryption formats
- Converting time zones
- Changing identifier formats
- Merging records
- Splitting records
- Truncating text
- Reducing numeric precision
- Changing case sensitivity
- Tightening validation on legacy data
- Adding non-null constraints to existing populated tables
- Replacing storage backends
- Removing configuration support

Destructive changes require:

1. Explicit identification in the release plan
2. Review of affected data
3. A pre-update backup
4. Representative migration tests
5. Documented recovery procedure
6. Release notes
7. Operator approval
8. Verification after migration

Do not hide destructive behavior inside generic cleanup work.

---

# 10. Database Rollback Honesty

Changing the container image back does not necessarily reverse a database migration.

Every release must state separately whether it supports:

- Application image rollback
- Configuration rollback
- Database downgrade
- Full restore from pre-update backup

Do not use the word “rollback” without clarifying which one is meant.

Example:

```text
Application rollback: supported to 1.4.2
Schema downgrade: not supported
Recovery after migration failure: restore pre-update backup
```

If database downgrade is not tested, it is not supported.

---

# 11. Database Version Compatibility

The application must know which schema versions it supports.

Example:

```text
Application 1.5.x supports schema 14 through 16.
```

The application must refuse to start when:

- The schema is too old to use safely
- The schema is newer than the application understands
- Required migrations are incomplete
- Migration checksums are invalid
- A prior migration is marked failed

Failing clearly is safer than attempting permissive compatibility.

---

# 12. Migration Testing

Every release containing database changes must test:

## Fresh Schema

Create a clean database entirely through the supported schema process.

## Upgrade from Previous Release

- Install the previous supported release
- Create representative data
- Upgrade to the candidate release
- Verify schema
- Verify records
- Verify relationships
- Verify files
- Verify application behavior

## Messy Legacy Data

Include representative:

- Null values
- Duplicate values
- Old formats
- Unusual Unicode
- Long text
- Empty files
- Missing optional relationships
- Previously valid but now-invalid values

## Migration Re-entry

Confirm that interrupted or repeated migration execution does not silently corrupt data.

## Failure Simulation

Simulate migration failure and verify:

- Upgrade stops
- Old application is not incorrectly restarted against an incompatible schema
- Backup remains available
- Diagnostics identify the failure
- Recovery instructions are accurate

---

# 13. Backup Before Upgrade

Every production upgrade must verify that a usable backup exists.

For upgrades containing schema or storage changes, create a pre-update backup immediately before modification.

The pre-update backup must identify:

- Application version
- Schema version
- Installation ID
- Database engine and version
- Backup format version
- Creation timestamp
- File checksums

An old scheduled backup may not be sufficient for a high-risk migration.

---

# 14. Backup Rules

A valid backup must:

- Be separate from live state
- Include all irreplaceable data
- Include required keys
- Include restoration metadata
- Be checksummed
- Be readable by the documented restore tooling
- Be protected appropriately
- Have a retention policy
- Be tested through restoration

A Docker volume is not a backup.

A filesystem snapshot is not automatically a consistent database backup.

A backup that has never been restored is unproven.

---

# 15. Restore Rules

Restore tooling must be maintained alongside application changes.

Every change to:

- Storage layout
- Database engine
- File format
- Encryption
- Key storage
- Backup manifest
- Compression
- Archive structure

must consider restore compatibility.

At least one automated or regularly executed restore test must confirm:

```text
Create data
→ back up
→ destroy installation
→ create clean installation
→ restore
→ verify data
→ verify identity
→ verify health
```

Do not deprecate an old backup format without a migration or supported restoration path.

---

# 16. Configuration Hygiene

Every configuration change must declare whether it is:

- New
- Optional
- Required
- Deprecated
- Removed
- Secret
- Safe to change live
- Requires restart
- Requires migration
- Immutable after installation

New required variables should have either:

- A safe default
- An automatic migration
- A clear preflight failure before upgrade

Do not release an application that starts in a partially misconfigured state without clearly identifying it.

Do not silently reinterpret old configuration values.

---

# 17. Secret Hygiene

Secrets must never appear in:

- Git history
- Dockerfiles
- Image layers
- Published release archives
- Default configuration
- Application logs
- CI logs
- Error pages
- Health endpoints
- Version endpoints
- Support bundles
- Screenshots committed to the repository
- Test fixtures copied from production
- AI prompts when avoidable

Secrets must be:

- Generated with adequate randomness
- Stored outside images
- Preserved across upgrades
- Rotatable
- Redacted from diagnostics
- Scoped to the minimum necessary permissions

Do not regenerate encryption or signing keys during routine upgrades.

---

# 18. Dependency Hygiene

Every production dependency change must be deliberate.

Use lockfiles or equivalent version pinning.

Review:

- Newly introduced dependencies
- Removed dependencies
- Major-version upgrades
- Runtime downloads
- Native system libraries
- License implications
- Security advisories
- Abandoned packages
- Typosquatting risk
- Transitive dependency growth

Avoid adding a dependency for trivial functionality when the cost exceeds the benefit.

Do not allow AI agents to invent package names and install them without verification.

---

# 19. Container Hygiene

Production containers must:

- Use explicit base-image versions
- Avoid unnecessary packages
- Avoid development tools in final images
- Avoid embedded credentials
- Run as non-root where practical
- Define writable paths explicitly
- Handle signals correctly
- Expose only required ports
- Include meaningful metadata
- Produce logs through standard output/error
- Pass vulnerability scanning according to project policy

Base-image updates must be treated as release changes.

Do not silently rebuild an old application version against a new base image under the same version tag.

---

# 20. Host and Architecture Compatibility

Every release must define:

- Supported operating systems
- Supported Docker versions
- Supported Docker Compose versions
- Supported CPU architectures
- Minimum memory
- Minimum disk space
- Required kernel features
- Required filesystem behavior
- Required external services

Do not publish an `arm64` image merely because it builds.

An architecture is supported only after functional tests pass on that architecture.

---

# 21. Health and Readiness Rules

Every release must preserve meaningful:

```text
/health/live
/health/ready
/version
```

Readiness must check required dependencies.

Do not change readiness semantics casually, because deployment tooling may depend on them.

A release must not be marked successful until:

- Containers are running
- Required migrations completed
- Readiness passes
- Version matches the target
- Critical background workers are healthy
- Required storage is writable

---

# 22. Logging Rules

Logs must remain useful across releases.

Each release must ensure logs:

- Include severity
- Include timestamps
- Identify the responsible component
- Preserve error context
- Avoid secrets
- Avoid unnecessary personal information
- Are retrievable without entering containers
- Do not grow without operational limits

Schema or format changes to structured logs must be documented when external tooling depends on them.

Do not convert serious errors into warnings merely to make health checks pass.

---

# 23. Error-Handling Rules

Production code must fail clearly.

Avoid:

- Silent fallback to unsafe behavior
- Catch-all exception suppression
- Returning success after partial failure
- Automatically creating replacement data when expected data is missing
- Continuing after schema incompatibility
- Automatically resetting configuration
- Automatically deleting corrupt records
- Ignoring failed backups
- Treating unavailable dependencies as healthy

Fallback behavior must be deliberate, documented, and tested.

---

# 24. Release Testing Rules

Every release must run applicable deterministic checks.

Minimum expectations:

- Formatting
- Linting
- Static analysis
- Type checking
- Unit tests
- Integration tests
- Production image build
- Container startup test
- Health test
- Configuration validation
- Secret scanning
- Dependency scanning
- Container vulnerability scanning

Releases affecting state must additionally test:

- Upgrade from previous supported release
- Persistent-data preservation
- Database migrations
- Backup
- Restore
- Failed upgrade behavior
- Compatibility enforcement

Tests must exercise the production artifact, not only the development runtime.

---

# 25. Test Integrity Rules

Agents and developers must not:

- Delete a failing test solely to make CI pass
- Weaken assertions without justification
- Replace meaningful integration tests with mocks merely for convenience
- Change expected behavior without updating requirements
- Mark flaky tests as ignored indefinitely
- Skip migration tests for database-changing releases
- Skip restore tests for backup-format changes
- Hard-code test results
- Hide failures behind unconditional retries
- Treat “command exited zero” as proof of correct data behavior

A test must prove the requirement it claims to cover.

---

# 26. Representative Upgrade Data

Maintain one or more versioned upgrade fixtures representing previous production installations.

Fixtures should include:

- Existing users
- Existing configuration
- Uploaded files
- Generated files
- Legacy records
- Null and optional values
- Unusual but valid values
- Previously deprecated fields
- Pending or completed jobs where relevant
- Installation metadata

Do not test migrations only against empty databases.

Do not use real customer data unless it has been properly sanitized and authorized.

---

# 27. AI-Generated Code Rules

AI coding agents may:

- Propose changes
- Implement scoped work orders
- Write tests
- Generate documentation
- Analyze failures
- Suggest migrations
- Draft release notes

AI coding agents must not be the sole authority for:

- Requirements
- Security decisions
- Authentication changes
- Authorization changes
- Database destruction
- Migration safety
- Backup correctness
- Restore correctness
- Release approval
- Production credential access
- Fleet-wide deployment

An agent-generated change is held to the same or higher standard as human-generated code.

“Generated by AI” is neither an excuse nor evidence of correctness.

---

# 28. High-Risk AI Change Categories

The following AI-generated changes require heightened review:

- Database migrations
- Authentication
- Authorization
- Encryption
- Key management
- Backup
- Restore
- Installer scripts
- Update scripts
- Rollback logic
- CI permissions
- Release workflows
- Production networking
- Reverse-proxy trust
- File deletion
- Data transformation
- Dependency replacement
- Secret handling
- Remote command execution
- Support-bundle collection
- Telemetry

For these categories, require:

1. Explicit scope
2. Threat or failure analysis
3. Test evidence
4. Data-impact statement
5. Security-impact statement
6. Recovery procedure
7. Operator approval

---

# 29. Separation of Authority

The same automated actor should not independently:

1. Define the requirement
2. Implement the change
3. Define the only tests
4. Decide the tests are sufficient
5. Merge the change
6. Publish the release
7. Deploy to all production installations

Use separation between:

- Builder
- Reviewer
- Deterministic CI
- Release authority
- Deployment authority

For a solo operator, these may be the same person at different steps, but the controls should remain logically separate.

---

# 30. CI Permission Rules

GitHub Actions or equivalent workflows must use least privilege.

Pull-request workflows must not have access to:

- Production SSH keys
- Registry administrative credentials
- Code-signing private keys
- Customer credentials
- Backup encryption keys
- DNS administrative tokens
- Deployment-control credentials

Publishing permissions should exist only in explicitly authorized release workflows.

Third-party actions should be pinned according to project security policy.

Do not run untrusted pull-request code with high-privilege secrets.

---

# 31. Release Authorization

A production release requires explicit authorization.

Merging into the default branch must not automatically deploy to every production installation.

The release process should require an intentional action such as:

- Approved version tag
- Protected release environment approval
- Signed release action
- Manual promotion of a tested image digest

Emergency releases may use an abbreviated process, but must still remain traceable and recoverable.

---

# 32. Release Channels

Supported channels may include:

```text
development
canary
beta
stable
long-term-support
```

Channel aliases may point to image digests, but installed systems must record the exact resolved version.

New releases should progress through appropriate channels.

At minimum:

```text
internal persistent installation
→ canary
→ stable
```

Do not release first to every customer simultaneously.

---

# 33. Canary Rules

Canary installations should:

- Contain realistic persistent data
- Operate for meaningful normal use
- Receive candidate releases first
- Exercise backup and update paths
- Report version and health
- Be recoverable without special developer-only procedures

A temporary empty test container is not a sufficient canary.

---

# 34. Rollout Rules

As the installation fleet grows, release in waves.

Example:

```text
Internal
→ selected canaries
→ 10 percent
→ 25 percent
→ 50 percent
→ 100 percent
```

Pause rollout when:

- Upgrade failures exceed threshold
- Readiness failures increase
- Migration failures appear
- Data-integrity concerns arise
- Support incidents spike
- Backup verification fails
- New critical vulnerabilities are discovered

Do not continue rollout merely because a schedule says to proceed.

---

# 35. Automatic Update Rules

Initial production installations should use operator-controlled updates.

Do not introduce unattended automatic updates until:

- Backups are reliable
- Restore is proven
- Compatibility checks are enforced
- Failure reporting exists
- Canary promotion exists
- Rollout pausing exists
- Release revocation exists
- The operational consequences are understood

Security updates may justify stronger prompts, but should not bypass data-safety mechanisms.

---

# 36. Pre-Update Checks

Before modifying a production installation, verify:

- Current version is known
- Target version is valid
- Upgrade path is supported
- Current schema is compatible
- Required configuration exists
- Required secrets exist
- Sufficient disk space exists
- Persistent paths are writable
- Database is reachable
- No failed migration is pending
- Backup destination is writable
- Required backup completes
- Target image is available
- Target image digest matches release metadata

Fail before making changes when preconditions are not met.

---

# 37. Update Sequence

The standard update sequence is:

1. Identify current application and schema versions
2. Validate target release
3. Validate supported upgrade path
4. Run preflight checks
5. Create or verify pre-update backup
6. Pull the exact image
7. Verify image digest
8. Enter maintenance mode if required
9. Stop affected workers or application services
10. Run migrations explicitly
11. Start the target application
12. Verify target version
13. Verify schema version
14. Verify readiness
15. Verify representative application behavior
16. Record successful upgrade
17. Retain backup and previous image
18. Exit maintenance mode

Do not mark an update complete merely because containers started.

---

# 38. Failed Update Rules

If an update fails:

- Stop further rollout
- Preserve logs
- Preserve the failed state for diagnosis when safe
- Do not repeatedly rerun destructive migrations automatically
- Determine whether the schema changed
- Determine whether the previous application remains compatible
- Use image rollback only when compatibility is known
- Otherwise restore the pre-update backup
- Record the failure
- Communicate the affected version and recovery path

Do not improvise destructive repairs directly against customer data without a backup.

---

# 39. Release Notes

Every production release must include:

- Version
- Release date
- Summary
- User-visible changes
- Operational changes
- Configuration changes
- Database changes
- Storage changes
- Security changes
- Dependency changes
- Upgrade prerequisites
- Supported source versions
- Expected downtime
- Known risks
- Known limitations
- Backup requirement
- Rollback support
- Restore procedure
- Image digest
- Source commit

Avoid release notes that say only:

```text
Bug fixes and improvements
```

when operationally meaningful changes occurred.

---

# 40. Changelog Hygiene

Maintain a changelog for supported releases.

Do not include every internal commit.

Include changes relevant to:

- Users
- Operators
- Administrators
- Integrators
- Security reviewers
- Upgrade planning

Clearly label:

- Added
- Changed
- Fixed
- Deprecated
- Removed
- Security
- Migration required
- Configuration required

---

# 41. Deprecation Rules

Before removing a supported feature, configuration variable, API, file format, or upgrade path:

1. Mark it deprecated
2. Document the replacement
3. Warn operators
4. Preserve compatibility for a defined period where practical
5. Add migration tooling where needed
6. Announce the removal version
7. Test installations using the deprecated behavior

Do not silently remove production behavior because it appears unused in the current codebase.

---

# 42. API and Integration Compatibility

For public or customer-used APIs:

- Version breaking interfaces
- Preserve documented behavior within a supported major version
- Test authentication and authorization
- Document changed response fields
- Avoid changing identifier semantics
- Avoid removing fields without deprecation
- Avoid changing error formats casually
- Record rate-limit or timeout changes

AI agents must not assume an endpoint is unused merely because no internal caller is found.

---

# 43. File-Format Compatibility

Changes to user-owned or generated file formats must define:

- Reader compatibility
- Writer compatibility
- Migration behavior
- Downgrade behavior
- Backup implications
- Restore implications
- Whether conversion is reversible

Do not overwrite the only copy of an old-format file during conversion without a safe recovery path.

---

# 44. Encryption and Key Rules

Changes involving encryption require special caution.

Never:

- Rotate encryption keys implicitly during ordinary upgrade
- Delete old keys before all data is re-encrypted and verified
- Change encoding or cipher metadata without migration tests
- Store keys only inside the application image
- Include private keys in support bundles
- Claim encrypted backups without verifying all included files

Key rotation must have a documented, resumable, and recoverable process.

---

# 45. Authentication and Authorization Rules

Changes to login, sessions, roles, permissions, or account recovery require:

- Explicit security review
- Upgrade compatibility analysis
- Session impact analysis
- Administrative recovery path
- Tests for unauthorized access
- Tests for privilege escalation
- Tests for existing accounts and roles
- Audit-log consideration

Do not weaken authorization checks to resolve functionality failures.

Do not create hidden default administrative credentials.

---

# 46. Network Exposure Rules

Every release must review exposed ports and network listeners.

Rules:

- Databases remain internal by default
- Administrative interfaces require protection
- Debug endpoints remain disabled in production
- Metrics endpoints are protected when they expose sensitive information
- Reverse-proxy trust is explicit
- Forwarded headers are accepted only from trusted proxies
- CORS settings are deliberate
- WebSocket and upload behavior are tested where applicable

A new listening port is a release-significant change.

---

# 47. Telemetry and Privacy Rules

Do not add external telemetry silently.

Any telemetry must be:

- Explicitly documented
- Minimal
- Appropriate to the product
- Securely transmitted
- Governed by a retention policy
- Free of unnecessary sensitive data
- Opt-in where project policy requires
- Disableable
- Visible to the operator

Do not send installation data, customer data, logs, prompts, files, or identifiers to third parties without an explicit product decision.

---

# 48. Support Bundle Rules

Support bundles must remain sanitized across releases.

They may include:

- Application version
- Schema version
- Installation ID
- Container status
- Host OS
- CPU architecture
- Docker version
- Compose version
- Disk space
- Health results
- Migration status
- Recent sanitized logs
- Sanitized configuration shape

They must exclude:

- Passwords
- Tokens
- Cookies
- Session identifiers
- Private keys
- Database contents
- Uploaded customer files
- Full environment dumps
- Personal information
- Sensitive request bodies

Changes to support-bundle contents require redaction tests.

---

# 49. Observability Rules

Every supported installation should make it possible to determine:

- What version is running
- Whether the application is alive
- Whether it is ready
- Whether the database is reachable
- Whether migrations completed
- Whether storage is writable
- Whether backups are succeeding
- Whether disk space is low
- Whether background workers are functioning
- Whether recent upgrades succeeded

Do not require invasive manual inspection for ordinary health questions.

---

# 50. Installation Inventory

As installations grow, maintain an inventory containing at least:

```text
Installation ID
Application version
Schema version
Release channel
Last successful upgrade
Last successful backup
Health status
Deployment mode
CPU architecture
```

Customer identity and sensitive infrastructure details should be handled according to privacy and security policy.

Do not rely on memory or an informal spreadsheet indefinitely as the fleet grows.

---

# 51. Support Lifecycle

Define how long releases are supported.

Possible model:

```text
Current stable minor release
Previous stable minor release
Designated long-term-support release
```

Document:

- Security-fix policy
- Bug-fix policy
- Upgrade expectations
- End-of-support dates
- Minimum supported version
- Required intermediate upgrades

Do not claim indefinite support for every historical version unless that commitment is intentional.

---

# 52. Vulnerability Response

For a critical vulnerability:

1. Confirm affected versions
2. Identify exposed configurations
3. Prepare and test a fixed release
4. Preserve release traceability
5. Publish actionable guidance
6. Identify required configuration changes
7. Promote through an accelerated canary
8. Notify affected operators
9. Revoke compromised artifacts or credentials where necessary
10. Document follow-up actions

Do not overwrite the vulnerable release artifact with a fixed image using the same tag.

---

# 53. Release Revocation

Maintain a process to mark a release as unsafe.

A revoked release should identify:

- Affected version
- Affected image digest
- Reason
- Severity
- Whether installations should stop upgrading
- Whether running installations must downgrade or restore
- Replacement version
- Data-integrity implications

The update mechanism should be able to refuse installation of a known-revoked release.

---

# 54. Documentation Rules

Documentation must change with the software.

Every release must review:

- Installation instructions
- Configuration reference
- Upgrade instructions
- Backup instructions
- Restore instructions
- Troubleshooting
- Compatibility matrix
- Release notes
- Security guidance
- Support-bundle guidance

Do not merge commands that have not been tested.

Do not leave old instructions that can destroy current data.

---

# 55. Required Release Evidence

A production release candidate must provide:

```text
Version:
Source commit:
Image digest:
Schema version:
Supported upgrade sources:
Migration list:
Configuration changes:
Persistent-storage changes:
Backup result:
Restore result:
Upgrade-test result:
Health-test result:
Security-scan result:
Known limitations:
Rollback support:
Recovery procedure:
Release approver:
```

Claims should reference logs, CI results, test output, or other deterministic evidence.

---

# 56. Production Release Checklist

Before promotion to stable, confirm:

## Source and Artifact

- Version is final
- Source commit is known
- Working tree was clean
- Image was built by the authorized workflow
- Image digest is recorded
- Artifact was not rebuilt between validation and promotion

## Tests

- Unit tests passed
- Integration tests passed
- Production image started
- Health checks passed
- Configuration validation passed
- Secret scan passed
- Dependency scan passed
- Container scan passed

## Data Safety

- Persistent-data changes are documented
- Migration changes are documented
- Upgrade from previous supported release passed
- Representative existing data survived
- Pre-update backup completed
- Restore test passed
- Rollback limitations are documented

## Operations

- Logs are usable
- Version endpoint is correct
- Support bundle is sanitized
- Documentation matches behavior
- Disk-space requirements are known
- Expected downtime is known

## Release Control

- Release notes are complete
- Canary installation passed
- Release is explicitly approved
- Publishing credentials were appropriately scoped
- Rollout plan exists
- Revocation path exists

---

# 57. Required Agent Report for Release Work

Any AI agent performing release-related work must report:

```text
Release work item:
Status:

Files created:
Files modified:
Files deleted:

Application behavior changed:

Database behavior changed:

Persistent-data impact:

Configuration impact:

Security impact:

Compatibility impact:

Commands executed:

Tests executed:
- Test:
- Result:
- Evidence:

Upgrade path tested:

Backup tested:

Restore tested:

Rollback limitations:

Known risks:

Manual review required:

Recovery procedure:

Recommendation:
```

“Done” is not an acceptable release report.

---

# 58. Agent Instruction for Ongoing Releases

Use the following instruction when preparing a release:

```text
Read `prod_release_rules.md` before making release-related changes.

Inspect the changes since the last production release and identify:

- application behavior changes
- database and migration changes
- persistent-storage changes
- configuration changes
- secret-handling changes
- authentication or authorization changes
- network exposure changes
- dependency changes
- backup or restore changes
- compatibility changes

Do not begin publishing.

First produce a release risk assessment and map every applicable requirement
from `prod_release_rules.md` to evidence or required work.

Identify the exact supported source versions, target schema version, required
migrations, backup requirements, rollback limitations, and recovery procedure.

Do not weaken tests, rewrite released migrations, use mutable production tags,
or assume that container rollback reverses database changes.

Treat existing production data as irreplaceable.
```

---

# 59. Permanent Release Standard

Every production release must be able to answer:

```text
What changed?
Why did it change?
Which existing installations are supported?
What data will be modified?
What configuration will be modified?
What must be backed up?
How was the upgrade tested?
How was restoration tested?
What happens if migration fails?
Can the previous image still run?
What is the exact recovery procedure?
Which artifact was approved?
Who authorized the release?
```

If these questions cannot be answered clearly, the release is not ready.

---

# 60. Final Rule

The primary measure of release quality is not whether the new version works on a clean installation.

It is whether an existing supported installation can move to the new version while preserving its data, identity, configuration, security, and recoverability.

The governing release sequence is:

```text
Inspect existing installation
→ validate compatibility
→ create backup
→ apply exact version
→ run controlled migrations
→ verify data
→ verify health
→ retain recovery path
→ promote gradually
```

Production data must never become the experiment.