"""Provisioning the first mailbox makes a verified domain sendable.

From a first real install. The documented order is verify, provision, send.
Verifying before any mailbox exists correctly stops at VERIFIED -- the status
means "DNS is complete, only a mailbox is missing" -- but provisioning one did
not recompute it, so the send was refused on a domain whose DNS had been
correct the entire time. Nothing in the sequence told the operator to verify a
second time, and the error named the status rather than the remedy.
"""

from __future__ import annotations

import os

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from emaild.crypto import MailboxCipher
from emaild.models import Base, Domain, DomainStatus
from emaild.providers.mxroute import MXRouteClient, RateLimiter
from emaild.provisioning import provision_mailbox

TEST_DSN = os.environ.get("EMAILD_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="EMAILD_TEST_DATABASE_URL not set")

TEST_KEY = "Bb1kMh0e_pJKtxJm5RXFF8pmvWZ_XYbFxK1hHYnQGTk="


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


def _ok_client() -> MXRouteClient:
    """A provider that accepts every provisioning call."""
    return MXRouteClient(
        "srv",
        "user",
        "key",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"success": True})),
        limiter=RateLimiter(),
    )


async def _provision_onto(session, status: DomainStatus) -> Domain:
    domain = Domain(name="example.com", status=status)
    session.add(domain)
    await session.flush()

    async with _ok_client() as client:
        await provision_mailbox(
            session,
            client,
            MailboxCipher(TEST_KEY),
            address="noreply@example.com",
        )
    return domain


async def test_verified_becomes_ready(session):
    """The whole point: the only missing precondition was a mailbox."""
    domain = await _provision_onto(session, DomainStatus.VERIFIED)
    assert domain.status is DomainStatus.READY


@pytest.mark.parametrize(
    "status",
    [
        DomainStatus.DNS_INCOMPLETE,
        DomainStatus.MISCONFIGURED,
        DomainStatus.SUSPENDED,
    ],
)
async def test_other_statuses_are_not_promoted(session, status):
    """Adding a mailbox proves nothing about DNS, and must never override an
    operator's decision to suspend a domain. Promotion is narrow on purpose."""
    domain = await _provision_onto(session, status)
    assert domain.status is status


async def test_ready_stays_ready(session):
    domain = await _provision_onto(session, DomainStatus.READY)
    assert domain.status is DomainStatus.READY
