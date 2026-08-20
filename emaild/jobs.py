"""Privileged work requested by an unprivileged process.

The dashboard needed to add and verify domains, and cannot: both call MXRoute
with an account-root credential that can delete every mailbox on the account,
and role=api is never given it. Handing the API that key to make a button work
would trade the entire privilege separation for two clicks.

So the API asks instead. It writes a row here; the provisioner (role=admin, no
listener, no published port) executes it. What keeps this from being equivalent
to just giving the API the credential is that the request is not a command --
it is a JobType, and the enum has two members. A fully compromised API can ask
for a domain to be added. It cannot ask for a mailbox to be deleted, because
there is no way to express that.

The payload is validated HERE, at execution, not trusted from the row. An
attacker who could write directly to this table would otherwise control the
argument to a privileged call.

Claiming reuses the pattern the message queue already proved: FOR UPDATE SKIP
LOCKED plus a stale-claim reaper, so a provisioner killed mid-job leaves work
that another one picks up rather than a row stuck in RUNNING forever.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from emaild.domains import add_domain, refresh_domain
from emaild.models import Domain, JobStatus, JobType, ProvisioningJob
from emaild.providers.mxroute import MXRouteClient, MXRouteError

log = logging.getLogger(__name__)

# A job that has been RUNNING longer than this is presumed abandoned. Domain
# calls are seconds of work; ten minutes is generous enough that a slow
# provider never trips it.
STALE_CLAIM_AFTER = timedelta(minutes=10)

# Deliberately strict, and applied before the value reaches a privileged call.
# Not a general hostname validator: it rejects anything that is not plainly a
# domain, which is the only thing either job type accepts.
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class JobError(Exception):
    """A job that cannot be executed, explained for whoever requested it."""


def clean_domain(raw: str) -> str:
    """Normalise and validate a domain name, or raise.

    Called both when enqueueing and when executing. The duplication is
    deliberate: the first is a courtesy that gives the operator an immediate
    error, the second is the one that actually protects the privileged call.
    """
    name = (raw or "").strip().lower().rstrip(".")
    if not name:
        raise JobError("A domain name is required.")
    if name.startswith("http://") or name.startswith("https://"):
        raise JobError("Enter just the domain, with no https:// and no path.")
    if "@" in name:
        raise JobError("Enter the domain only, not an email address.")
    if not _DOMAIN_RE.match(name):
        raise JobError(f"{name!r} does not look like a domain name.")
    return name


async def enqueue(
    session: AsyncSession,
    job_type: JobType,
    payload: dict,
    *,
    requested_by: str = "dashboard",
) -> ProvisioningJob:
    """Request a privileged action. Returns the queued job."""
    if job_type in (JobType.ADD_DOMAIN, JobType.VERIFY_DOMAIN):
        payload = {**payload, "domain": clean_domain(payload.get("domain", ""))}

    # One outstanding request per domain per type. Without this, an impatient
    # double-click queues the same provider call twice, and `domains add` is
    # not free -- it is a write against the account.
    existing = (
        (
            await session.execute(
                select(ProvisioningJob).where(
                    ProvisioningJob.job_type == job_type,
                    ProvisioningJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                )
            )
        )
        .scalars()
        .all()
    )
    for job in existing:
        if (job.payload or {}).get("domain") == payload.get("domain"):
            return job

    job = ProvisioningJob(
        job_type=job_type,
        payload=payload,
        status=JobStatus.PENDING,
        requested_by=requested_by,
    )
    session.add(job)
    await session.flush()
    log.info("queued %s for %s (by %s)", job_type.value, payload.get("domain"), requested_by)
    return job


async def reap_stale(session: AsyncSession) -> int:
    """Return abandoned RUNNING jobs to PENDING.

    Crash safety by timestamp rather than by an ack protocol, matching the
    message queue: a provisioner that dies mid-job cannot tell anyone, so the
    only evidence available is that the claim stopped moving.
    """
    cutoff = datetime.now(UTC) - STALE_CLAIM_AFTER
    result = await session.execute(
        update(ProvisioningJob)
        .where(ProvisioningJob.status == JobStatus.RUNNING, ProvisioningJob.claimed_at < cutoff)
        .values(status=JobStatus.PENDING, claimed_at=None)
    )
    count = result.rowcount or 0
    if count:
        log.warning("returned %d stale provisioning job(s) to pending", count)
    return count


async def claim_one(session: AsyncSession) -> ProvisioningJob | None:
    """Take the oldest pending job, or None."""
    job = (
        await session.execute(
            select(ProvisioningJob)
            .where(ProvisioningJob.status == JobStatus.PENDING)
            .order_by(ProvisioningJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()

    if job is None:
        return None

    job.status = JobStatus.RUNNING
    job.claimed_at = datetime.now(UTC)
    await session.flush()
    return job


async def execute(session: AsyncSession, client: MXRouteClient, job: ProvisioningJob) -> str:
    """Run one job. Returns a message for the requester. Raises JobError.

    The payload is re-validated here rather than trusted, because this is the
    side of the boundary that holds the credential.
    """
    payload = job.payload or {}

    if job.job_type is JobType.ADD_DOMAIN:
        name = clean_domain(payload.get("domain", ""))
        domain = await add_domain(session, client, name)
        return (
            f"Added {domain.name} (status: {domain.status.value}). "
            "Publish its DNS records, then verify."
        )

    if job.job_type is JobType.VERIFY_DOMAIN:
        name = clean_domain(payload.get("domain", ""))
        exists = (
            await session.execute(select(Domain).where(Domain.name == name))
        ).scalar_one_or_none()
        if exists is None:
            raise JobError(f"{name} is not tracked. Add it first.")

        refresh = await refresh_domain(session, client, name)
        if refresh.changed:
            return f"{name}: {refresh.previous.value} -> {refresh.current.value}"
        detail = refresh.note or "no change"
        return f"{name}: still {refresh.current.value} ({detail})"

    # Unreachable while JobType has two members, and the point is that adding a
    # third must be a deliberate act that fails loudly until it is implemented.
    raise JobError(f"unsupported job type: {job.job_type}")


async def run_one(session: AsyncSession, client: MXRouteClient) -> bool:
    """Claim and execute a single job. True if one was processed."""
    job = await claim_one(session)
    if job is None:
        return False

    try:
        job.result = await execute(session, client, job)
        job.status = JobStatus.SUCCEEDED
        log.info("job %s succeeded: %s", job.id, job.result)
    except (JobError, MXRouteError) as exc:
        # Expected failures: a bad domain, a provider refusal. Recorded on the
        # job so the requester sees why, rather than a row that silently
        # stopped.
        job.result = str(exc)
        job.status = JobStatus.FAILED
        log.warning("job %s failed: %s", job.id, exc)
    except Exception as exc:  # noqa: BLE001
        # Unexpected. Still recorded rather than left RUNNING for the reaper to
        # retry forever -- a bug that fails identically on every attempt is not
        # made better by repeating it.
        job.result = f"unexpected error: {exc}"
        job.status = JobStatus.FAILED
        log.exception("job %s raised", job.id)

    job.completed_at = datetime.now(UTC)
    await session.flush()
    return True


async def recent(session: AsyncSession, limit: int = 10) -> list[ProvisioningJob]:
    return list(
        (
            await session.execute(
                select(ProvisioningJob).order_by(ProvisioningJob.created_at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
