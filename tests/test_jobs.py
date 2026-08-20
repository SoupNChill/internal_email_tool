"""The privileged-work queue, and the boundary it is supposed to be.

The dashboard can now add and verify domains, which both require the MXRoute
account-root credential -- a key that can delete every mailbox on the account.
role=api is never given it. Instead the API writes a row and the provisioner
executes it.

The claim being tested is that this is genuinely narrower than handing over the
credential, and it rests on one thing: a request is a JobType, and that enum
has two members. If someone later adds a destructive member, or starts trusting
the stored payload instead of re-validating it, the queue stops being a
boundary and becomes a slower way of granting the same power.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from emaild.jobs import (
    JobError,
    claim_one,
    clean_domain,
    enqueue,
    execute,
    reap_stale,
    run_one,
)
from emaild.models import (
    Base,
    Domain,
    DomainStatus,
    JobStatus,
    JobType,
    ProvisioningJob,
)
from emaild.providers.mxroute import MXRouteClient, RateLimiter

TEST_DSN = os.environ.get("EMAILD_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="EMAILD_TEST_DATABASE_URL not set")


@pytest.fixture
async def session():
    engine = create_async_engine(TEST_DSN, poolclass=None)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _client(handler) -> MXRouteClient:
    return MXRouteClient(
        "srv", "user", "key", transport=httpx.MockTransport(handler), limiter=RateLimiter()
    )


def _provider_ok(request: httpx.Request) -> httpx.Response:
    """Enough of MXRoute to satisfy add_domain and refresh_domain."""
    path = request.url.path
    if path.endswith("/dns"):
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "mx_records": [{"hostname": "mail.test", "priority": 10}],
                    "spf": {"value": "v=spf1 include:mxroute.com -all"},
                    "dkim": {"name": "x._domainkey", "value": "v=DKIM1; k=rsa; p=AAAA"},
                    "verification": {"name": "_da-verify-abc", "value": "domain-verified"},
                },
            },
        )
    if path == "/domains":
        return httpx.Response(200, json={"success": True, "data": []})
    return httpx.Response(200, json={"success": True})


# --- the security boundary -------------------------------------------------


def test_the_job_type_enum_contains_nothing_destructive():
    """This IS the boundary. A compromised API can only ask for what can be
    named here, so anything added to this enum widens what an attacker who owns
    the internet-facing surface can reach. Deletion, provisioning, and password
    rotation must stay out."""
    assert {t.value for t in JobType} == {"add_domain", "verify_domain"}


async def test_a_stored_payload_is_re_validated_at_execution(session):
    """The queue is written by a less-trusted process, so the argument to a
    privileged call must not be trusted from the row. A tampered payload has to
    fail at the boundary, not reach MXRoute."""
    job = ProvisioningJob(
        job_type=JobType.ADD_DOMAIN,
        payload={"domain": "not a domain at all"},
        status=JobStatus.PENDING,
    )
    session.add(job)
    await session.flush()

    async with _client(_provider_ok) as client:
        with pytest.raises(JobError):
            await execute(session, client, job)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "no-dots",
        "https://example.com",
        "user@example.com",
        "example..com",
        "-example.com",
        "example.com/path",
        "a" * 300 + ".com",
    ],
)
def test_rubbish_is_refused(bad):
    with pytest.raises(JobError):
        clean_domain(bad)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Example.COM", "example.com"),
        ("  example.com  ", "example.com"),
        ("example.com.", "example.com"),
        ("sub.example.co.uk", "sub.example.co.uk"),
    ],
)
def test_valid_domains_are_normalised(raw, expected):
    assert clean_domain(raw) == expected


# --- queue behaviour -------------------------------------------------------


async def test_enqueue_validates_before_storing(session):
    with pytest.raises(JobError):
        await enqueue(session, JobType.ADD_DOMAIN, {"domain": "nonsense"})


async def test_duplicate_requests_collapse(session):
    """An impatient double-click must not queue two writes against the
    provider account."""
    first = await enqueue(session, JobType.ADD_DOMAIN, {"domain": "example.com"})
    second = await enqueue(session, JobType.ADD_DOMAIN, {"domain": "example.com"})
    assert first.id == second.id


async def test_different_domains_do_not_collapse(session):
    a = await enqueue(session, JobType.ADD_DOMAIN, {"domain": "one.com"})
    b = await enqueue(session, JobType.ADD_DOMAIN, {"domain": "two.com"})
    assert a.id != b.id


async def test_a_completed_job_does_not_block_a_new_request(session):
    """Verify is run repeatedly by design -- publish records, re-check, repeat."""
    first = await enqueue(session, JobType.VERIFY_DOMAIN, {"domain": "example.com"})
    first.status = JobStatus.SUCCEEDED
    await session.flush()

    second = await enqueue(session, JobType.VERIFY_DOMAIN, {"domain": "example.com"})
    assert second.id != first.id


async def test_claiming_marks_it_running(session):
    await enqueue(session, JobType.ADD_DOMAIN, {"domain": "example.com"})
    job = await claim_one(session)
    assert job is not None
    assert job.status is JobStatus.RUNNING
    assert job.claimed_at is not None


async def test_claim_returns_none_when_empty(session):
    assert await claim_one(session) is None


async def test_stale_claims_are_returned_to_pending(session):
    """A provisioner killed mid-job cannot tell anyone, so the only evidence is
    that the claim stopped moving."""
    await enqueue(session, JobType.ADD_DOMAIN, {"domain": "example.com"})
    job = await claim_one(session)
    assert job is not None
    job.claimed_at = datetime.now(UTC) - timedelta(hours=1)
    await session.flush()

    assert await reap_stale(session) == 1
    await session.refresh(job)
    assert job.status is JobStatus.PENDING


async def test_a_fresh_claim_is_not_reaped(session):
    await enqueue(session, JobType.ADD_DOMAIN, {"domain": "example.com"})
    await claim_one(session)
    assert await reap_stale(session) == 0


# --- execution -------------------------------------------------------------


async def test_add_domain_runs_and_records_its_result(session):
    await enqueue(session, JobType.ADD_DOMAIN, {"domain": "example.com"})
    async with _client(_provider_ok) as client:
        assert await run_one(session, client) is True

    job = (await session.execute(select(ProvisioningJob))).scalar_one()
    assert job.status is JobStatus.SUCCEEDED
    assert "example.com" in (job.result or "")

    domain = (
        await session.execute(select(Domain).where(Domain.name == "example.com"))
    ).scalar_one()
    # Captured while a provider client was in hand, so the dashboard can show
    # them without one.
    assert domain.required_records


async def test_verifying_an_untracked_domain_fails_with_a_reason(session):
    await enqueue(session, JobType.VERIFY_DOMAIN, {"domain": "unknown.com"})
    async with _client(_provider_ok) as client:
        await run_one(session, client)

    job = (await session.execute(select(ProvisioningJob))).scalar_one()
    assert job.status is JobStatus.FAILED
    assert "not tracked" in (job.result or "")


async def test_a_provider_failure_is_recorded_not_raised(session):
    """The requester is a web page that has already redirected. A job that
    stops without saying why is invisible."""
    session.add(Domain(name="example.com", status=DomainStatus.ADDED))
    await session.flush()
    await enqueue(session, JobType.VERIFY_DOMAIN, {"domain": "example.com"})

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"success": False})

    async with _client(boom) as client:
        assert await run_one(session, client) is True

    job = (await session.execute(select(ProvisioningJob))).scalar_one()
    assert job.completed_at is not None
    assert job.result


async def test_run_one_reports_nothing_to_do(session):
    async with _client(_provider_ok) as client:
        assert await run_one(session, client) is False
