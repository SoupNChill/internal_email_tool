"""The dashboard's "what should I do next" logic.

From a first-time user who got everything running and then said: "I don't
really see a clear 'this is what I should do', and I don't understand what a
key or a project is."

These assert the ORDER is right -- each step is only offered once the thing it
depends on exists -- and that steps which cannot be done in the browser say so
with the command to run instead. A guide that suggests an impossible action is
worse than none, and this codebase has produced that exact bug twice.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from emaild.dashboard.setup_state import next_step
from emaild.models import (
    ApiKey,
    Base,
    Domain,
    DomainStatus,
    Mailbox,
    Project,
)

TEST_DSN = os.environ.get("EMAILD_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="EMAILD_TEST_DATABASE_URL not set")

BASE = "http://prod1:8000"


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


async def _domain(session, status: DomainStatus) -> Domain:
    d = Domain(name="example.com", status=status)
    session.add(d)
    await session.flush()
    return d


async def _mailbox(session, domain: Domain) -> Mailbox:
    m = Mailbox(
        domain_id=domain.id,
        address="noreply@example.com",
        password_encrypted="x",
        active=True,
    )
    session.add(m)
    await session.flush()
    return m


async def test_empty_installation_asks_for_a_domain(session):
    step = await next_step(session, BASE)
    assert "domain" in step.title.lower()
    # In the browser now: the dashboard queues the job, the provisioner runs
    # it. It used to hand over a CLI command.
    assert step.href == "/domains"
    assert not step.done


async def test_added_domain_asks_for_dns(session):
    await _domain(session, DomainStatus.ADDED)
    step = await next_step(session, BASE)
    assert "DNS" in step.title
    assert step.href == "/domains"


async def test_verified_domain_asks_for_a_sender(session):
    """VERIFIED means DNS is complete and only a mailbox is missing."""
    await _domain(session, DomainStatus.VERIFIED)
    step = await next_step(session, BASE)
    assert "sender identity" in step.title.lower()
    assert step.command and "mailboxes provision" in step.command


async def test_ready_domain_without_a_project_asks_for_one(session):
    d = await _domain(session, DomainStatus.READY)
    await _mailbox(session, d)
    step = await next_step(session, BASE)
    assert "project" in step.title.lower()
    assert step.href == "/keys"


async def test_project_without_a_key_asks_for_a_key(session):
    d = await _domain(session, DomainStatus.READY)
    await _mailbox(session, d)
    session.add(Project(name="app", active=True))
    await session.flush()
    step = await next_step(session, BASE)
    assert "key" in step.title.lower()
    assert step.href == "/keys"


async def test_a_complete_installation_says_it_is_ready(session):
    d = await _domain(session, DomainStatus.READY)
    await _mailbox(session, d)
    project = Project(name="app", active=True)
    session.add(project)
    await session.flush()
    session.add(ApiKey(project_id=project.id, name="k", key_hash="h", key_prefix="em_live_abc123"))
    await session.flush()

    step = await next_step(session, BASE)
    assert step.done
    assert BASE in step.why
    assert step.href == "/integrate"


async def test_a_revoked_key_does_not_count_as_having_one(session):
    """Otherwise an installation whose only key was revoked is told it is ready
    to send, with nothing that can authenticate."""
    from datetime import UTC, datetime

    d = await _domain(session, DomainStatus.READY)
    await _mailbox(session, d)
    project = Project(name="app", active=True)
    session.add(project)
    await session.flush()
    session.add(
        ApiKey(
            project_id=project.id,
            name="k",
            key_hash="h",
            key_prefix="em_live_abc123",
            active=False,
            revoked_at=datetime.now(UTC),
        )
    )
    await session.flush()

    step = await next_step(session, BASE)
    assert not step.done
    assert "key" in step.title.lower()


@pytest.mark.parametrize(
    "status",
    [DomainStatus.ADDED, DomainStatus.DNS_INCOMPLETE],
)
async def test_domain_steps_are_done_in_the_browser(session, status):
    """Domain work is queued through the dashboard now, so these link rather
    than hand over a command."""
    await _domain(session, status)
    step = await next_step(session, BASE)
    assert step.href == "/domains"


async def test_mailbox_provisioning_still_carries_a_command(session):
    """The one step that genuinely cannot happen here: provisioning needs the
    MXRoute credential AND the encryption key, and it can breach the provider's
    acceptable-use policy, which is a judgement call for a person. Saying "do
    it elsewhere" without saying how is where the friction was."""
    await _domain(session, DomainStatus.VERIFIED)
    step = await next_step(session, BASE)
    assert step.command and step.command.startswith("appctl admin mailboxes provision")
